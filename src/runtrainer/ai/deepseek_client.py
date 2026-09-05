"""DeepSeek 客户端（OpenAI 兼容协议）与 Mock 客户端。

接口约定：chat_json(system, user, data) → dict
- data 为机器可读的课表快照（真实 AI 从 user 文本读同一份信息；Mock 直接用它构造回应）。
- 返回 dict 需能通过 contracts.CoachOutput 校验。
"""
from __future__ import annotations

import json
import logging

from openai import OpenAI

log = logging.getLogger(__name__)

BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
FALLBACK_MODEL = "deepseek-v4-flash"
MAX_TOKENS = 4096
TEMPERATURE = 0.3
TIMEOUT_S = 120


class DeepSeekClient:
    def __init__(self, api_key: str, model: str | None = None):
        self.model = model or DEFAULT_MODEL
        self._client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=TIMEOUT_S)

    def chat_json(self, system: str, user: str, data: dict | None = None) -> dict:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)


class MockClient:
    """按场景返回预设建议：normal / low_hrv / overload / add_extra。

    从 data 快照中取真实课表 id/日期，保证输出能通过护栏与落库流程。
    """

    def __init__(self, scenario: str = "normal"):
        self.scenario = scenario
        self.calls: list[dict] = []

    def chat_json(self, system: str, user: str, data: dict | None = None) -> dict:
        self.calls.append({"system": system, "user": user, "data": data})
        data = data or {}
        workouts = data.get("workouts") or []
        by_date = {w["date"]: w for w in workouts}
        dates = sorted(by_date)
        today_w = by_date.get(data.get("today"), {})
        today = data.get("today", dates[0] if dates else "")
        builder = _ScenarioBuilder(today, by_date, dates)
        return {
            "normal": builder.normal,
            "low_hrv": builder.low_hrv,
            "overload": builder.overload,
            "add_extra": builder.add_extra,
        }[self.scenario]()


class _ScenarioBuilder:
    def __init__(self, today: str, by_date: dict, dates: list[str]):
        self.today = today
        self.by_date = by_date
        self.dates = dates

    def _ref(self, date: str) -> dict:
        w = self.by_date[date]
        return {"date": date, "planned_workout_id": w.get("id")}

    def normal(self) -> dict:
        adj = []
        if self.today in self.by_date:
            adj.append({**self._ref(self.today), "action": "keep",
                        "reason": "睡眠与 HRV 正常，按计划执行即可"})
        return {"summary": "今日状态良好，按计划执行。", "readiness": "good",
                "key_signals": ["HRV 处于正常区间", "睡眠充足"], "adjustments": adj,
                "weekly_notes": "保持节奏，注意恢复。"}

    def low_hrv(self) -> dict:
        adj = []
        if self.today in self.by_date:
            w = self.by_date[self.today]
            if w.get("kind") in ("T", "I", "R", "TUNEUP") or \
                    (w.get("kind") == "LR" and w.get("pace_zone") == "M"):
                adj.append({**self._ref(self.today), "action": "modify",
                            "changes": {"kind": "E", "pace_zone": "E",
                                        "duration_min": 40,
                                        "distance_km": round(40 * 6.5 / 60, 1)},
                            "reason": "HRV 连续偏低，改轻松跑促进恢复"})
            else:
                adj.append({**self._ref(self.today), "action": "keep",
                            "reason": "今日本就轻松，HRV 偏低照常完成"})
        return {"summary": "HRV 偏低，今天降强度。", "readiness": "low",
                "key_signals": ["HRV 连续 3 天低于基线", "睡眠时长不足"],
                "adjustments": adj,
                "weekly_notes": "本周减少一次强度课，观察恢复。"}

    def overload(self) -> dict:
        # 找到未来 7 天内最近的强度课，改休息/降量
        target = None
        for d in self.dates:
            w = self.by_date[d]
            if w.get("kind") in ("T", "I", "R", "TUNEUP") or \
                    (w.get("kind") == "LR" and w.get("pace_zone") == "M"):
                target = w
                break
        adj = []
        if target:
            adj.append({"date": target["date"], "planned_workout_id": target.get("id"),
                        "action": "rest",
                        "reason": "ACWR 超限且单调性偏高，休息一日降低受伤风险"})
        if self.today in self.by_date:
            adj.append({**self._ref(self.today), "action": "keep",
                        "reason": "今日课保持不变"})
        return {"summary": "负荷偏高，需要主动减量。", "readiness": "low",
                "key_signals": ["ACWR > 1.3", "近 7 天训练单调性高"],
                "adjustments": adj,
                "weekly_notes": "本周总跑量建议下调。"}

    def add_extra(self) -> dict:
        from datetime import date as _date, timedelta
        slot = None
        if self.today:
            d = _date.fromisoformat(self.today)
            hi = _date.fromisoformat(max(self.dates))
            while d <= hi:
                if d.isoformat() not in self.by_date:
                    slot = d.isoformat()
                    break
                d += timedelta(days=1)
        adj = []
        if self.today in self.by_date:
            adj.append({**self._ref(self.today), "action": "keep",
                        "reason": "按计划执行"})
        if slot:
            adj.append({"date": slot, "planned_workout_id": None, "action": "add_easy",
                        "reason": "用户请求加练，安排轻松有氧跑"})
        return {"summary": "状态允许加练一次轻松跑。", "readiness": "good",
                "key_signals": ["恢复良好，可以加练"],
                "adjustments": adj,
                "add_extra_advice": {
                    "allowed": True,
                    "suggestion": {"kind": "E", "duration_min": 30,
                                   "max_duration_min": 45, "pace_zone": "E",
                                   "reason": "以轻松配速促进有氧耐力，不影响后续强度课"},
                },
                "weekly_notes": "加练后注意补足睡眠。"}
