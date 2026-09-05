"""JS ↔ Python 桥：暴露给 window.pywebview.api 的唯一入口。

约定：所有方法返回统一信封 {"ok": bool, "data": ..., "error": str|None}，
参数与返回值均为 JSON 可序列化类型（dict/list/str/int/float/bool/None）。
"""
from __future__ import annotations

import functools
import logging

from ..db.repos import goal_repo, kv_repo, plan_repo, profile_repo, sync_repo
from ..services import settings_service

log = logging.getLogger(__name__)


def _sync_states_with_meta() -> list[dict]:
    """同步状态 + 解码 meta_json（含 last_stats 供 UI 展示真实同步结果）。"""
    from ..utils import jsonutil
    states = sync_repo.list_sync_states()
    for s in states:
        raw = s.pop("meta_json", None)
        s["meta"] = jsonutil.loads(raw) if raw else {}
    return states


def _with_workout_classification(a: dict, segments: list | None = None) -> dict:
    """给活动附加课程分类 {kind, label, work, rest, seg_kinds}（不落库，按需计算）。

    分段结构来自 structure_json（segments 已解析时直接用），max_hr 取
    档案手动值，缺失时按出生年份经验估计。分类本身是纯函数，代价极小。
    """
    from ..domain.workout_analysis import classify_workout, estimate_max_hr
    from ..utils import jsonutil
    if segments is None:
        segments = jsonutil.loads(a.get("structure_json")) or []
    prof = profile_repo.get_profile() or {}
    max_hr = prof.get("max_hr") or estimate_max_hr(prof.get("birth_year"))
    a["workout"] = classify_workout(
        segments, a.get("duration_s"), a.get("distance_m"), a.get("avg_hr"),
        max_hr, prof.get("rest_hr"))
    a.pop("structure_json", None)
    a.pop("laps_json", None)
    return a


def envelope(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": fn(*args, **kwargs)}
        except Exception as e:
            log.exception("bridge 调用失败: %s", fn.__name__)
            return {"ok": False, "error": str(e)}

    return wrapper


class Api:
    # ---- 设置 ----
    @envelope
    def get_settings(self):
        from ..ai.deepseek_client import PROVIDERS
        profile = profile_repo.get_profile() or {}
        garmin_user, garmin_pass = settings_service.get_garmin_credentials()
        provider = settings_service.get_ai_provider()
        return {
            "profile": profile,
            "garmin_username": garmin_user or "",
            "has_garmin_password": bool(garmin_pass),
            "has_deepseek_key": bool(settings_service.get_ai_key("deepseek")),
            "ai_provider": provider,
            "ai_providers": [{"key": k, **v} for k, v in PROVIDERS.items()],
            "ai_keys": {k: bool(settings_service.get_ai_key(k)) for k in PROVIDERS},
            "ai_model": settings_service.get_ai_model(),
            "theme": settings_service.get_theme(),
            "mock_mode": settings_service.is_mock_mode(),
            "garmin_cn": settings_service.is_garmin_cn(),
            "sync_states": _sync_states_with_meta(),
        }

    @envelope
    def save_profile(self, fields: dict):
        profile_repo.upsert_profile(fields)
        return {"saved": True}

    @envelope
    def save_garmin_credentials(self, username: str, password: str):
        if not username or not password:
            raise ValueError("账号和密码不能为空")
        settings_service.set_garmin_credentials(username, password)
        return {"saved": True}

    @envelope
    def clear_garmin_credentials(self):
        settings_service.clear_garmin_credentials()
        return {"cleared": True}

    @envelope
    def save_ai_key(self, provider: str, api_key: str):
        if provider not in settings_service.PROVIDERS:
            raise ValueError(f"未知 AI 服务商 {provider}")
        if not api_key:
            raise ValueError("API Key 不能为空")
        settings_service.set_ai_key(provider, api_key)
        return {"saved": True}

    @envelope
    def clear_ai_key(self, provider: str):
        if provider not in settings_service.PROVIDERS:
            raise ValueError(f"未知 AI 服务商 {provider}")
        settings_service.clear_ai_key(provider)
        return {"cleared": True}

    @envelope
    def save_deepseek_key(self, api_key: str):
        return self.save_ai_key("deepseek", api_key)

    @envelope
    def clear_deepseek_key(self):
        return self.clear_ai_key("deepseek")

    @envelope
    def set_setting(self, key: str, value: str):
        if key == "ai_model":
            settings_service.set_ai_model(value)
        elif key == "ai_provider":
            settings_service.set_ai_provider(value)
        elif key == "theme":
            settings_service.set_theme(value)
        elif key == "mock_mode":
            settings_service.set_mock_mode(value == "1")
        elif key == "garmin_cn":
            settings_service.set_garmin_cn(value == "1")
        else:
            kv_repo.set_setting(key, value)
        return {"saved": True}

    # ---- 目标 ----
    @envelope
    def get_active_goal(self):
        return goal_repo.get_active_goal()

    @envelope
    def list_goals(self):
        return goal_repo.list_goals()

    # ---- 计划 ----
    @envelope
    def get_active_plan(self):
        plan = plan_repo.get_active_plan()
        if not plan:
            return None
        plan["phase_weeks"] = plan_repo.get_phase_weeks(plan)
        # 配速表（按计划 VDOT 换算）：日历小窗各段落目标配速用
        from ..domain import vdot as vd
        try:
            plan["paces"] = vd.pace_table(float(plan["vdot"]))
        except (TypeError, ValueError):
            plan["paces"] = None
        goal = goal_repo.get_active_goal()
        if goal and plan["paces"] and goal.get("target_seconds") and goal.get("distance_m"):
            plan["paces"]["race"] = round(goal["target_seconds"] / (goal["distance_m"] / 1000.0), 1)
        return plan

    @envelope
    def get_plan_workouts(self, plan_id: int, start_date: str | None = None,
                          end_date: str | None = None):
        from ..utils import jsonutil
        ws = plan_repo.get_workouts(plan_id, start_date, end_date)
        for w in ws:
            segs = jsonutil.loads(w.pop("segments_json", None)) or []
            for s in segs:
                # 旧计划段落无 rest_mode：按训练类型补默认
                # （R 重复跑完全恢复=走路/慢跑/静止均可；I 间歇/T 阈值组间慢跑）
                if s.get("type") in ("tempo", "reps") and not s.get("rest_mode"):
                    s["rest_mode"] = "any" if s.get("zone") == "R" else "jog"
            w["segments"] = segs
        return ws

    @envelope
    def set_workout_status(self, workout_id: int, status: str,
                           completed_activity_id: int | None = None):
        plan_repo.set_workout_status(workout_id, status, completed_activity_id)
        return {"saved": True}

    @envelope
    def get_plan_progress(self):
        """计划进度：时期时间线（当前时期高亮）+ 完成进度 + 近 4 周执行率。"""
        from datetime import date as _date, timedelta
        from ..db.repos import activity_repo
        from ..domain.plan_engine import PHASE_ORDER
        from ..utils import dates
        plan = plan_repo.get_active_plan()
        if not plan:
            return None
        phase_weeks = plan_repo.get_phase_weeks(plan)
        today = dates.today()
        today_s = today.isoformat()
        start = _date.fromisoformat(plan["start_date"])
        ws = plan_repo.get_workouts(plan["id"])
        # 时期时间线（0 周的截断时期不展示）
        phases, cursor = [], start
        for p in PHASE_ORDER:
            wks = int(phase_weeks.get(p) or 0)
            if wks <= 0:
                continue
            end = cursor + timedelta(days=wks * 7 - 1)
            phases.append({"phase": p, "weeks": wks, "start_date": cursor.isoformat(),
                           "end_date": end.isoformat(), "current": cursor <= today <= end})
            cursor += timedelta(days=wks * 7)
        # 完成进度：已过日期的课完成情况
        past = [w for w in ws if w["date"] <= today_s]
        done = sum(1 for w in past if w["status"] == "completed")
        # 近 4 周计划 vs 实际跑量（窗口不早于计划开始日：新计划前几周课少，
        # 用 4 周实际跑量对比会把执行率撑爆）
        win_start = max(start, today - timedelta(days=27))
        win_start_s = win_start.isoformat()
        planned_km = sum((w.get("distance_km") or 0) for w in ws
                         if win_start_s <= w["date"] <= today_s)
        acts = activity_repo.list_activities(win_start_s, today_s, limit=1000)
        done_km = sum((a.get("distance_m") or 0) for a in acts) / 1000.0
        weeks_total = int(plan["total_weeks"])
        weeks_elapsed = max(0.0, min(float(weeks_total), (today - start).days / 7.0))
        return {
            "today": today_s, "start_date": plan["start_date"], "race_date": plan["race_date"],
            "total_weeks": weeks_total, "weeks_elapsed": round(weeks_elapsed, 1),
            "phases": phases,
            "workouts_past": len(past), "workouts_done": done,
            "planned_km_4w": round(planned_km, 1), "done_km_4w": round(done_km, 1),
            "compliance_4w": round(done_km / planned_km, 2) if planned_km > 0 else None,
        }

    # ---- 目标向导与计划生成 ----
    @envelope
    def get_goal_wizard_context(self):
        from ..services import plan_service
        return plan_service.wizard_context()

    @envelope
    def preview_plan(self, params: dict):
        from ..services import plan_service
        return plan_service.preview_plan(params)

    @envelope
    def create_goal_and_plan(self, params: dict):
        from ..services import plan_service
        return plan_service.create_goal_and_plan(params)

    # ---- 状态查询（供前端轮询） ----
    @envelope
    def get_sync_states(self):
        return _sync_states_with_meta()

    # ---- 仪表盘 ----
    @envelope
    def get_dashboard(self):
        from ..services import dashboard_service
        return dashboard_service.get_dashboard()

    # ---- AI 教练 ----
    @envelope
    def get_coach_snapshot(self):
        from ..services import coach_service
        return coach_service.get_snapshot()

    @envelope
    def request_coach_advice(self, extra_requested: bool = False, user_note: str = ""):
        from ..services import coach_service
        return coach_service.request_advice(extra_requested, user_note)

    @envelope
    def decide_coach_advice(self, approve: bool):
        from ..services import coach_service
        return coach_service.decide_advice(approve)

    @envelope
    def coach_chat(self, message: str):
        from ..services import coach_service
        return coach_service.chat(message)

    @envelope
    def get_chat_history(self, limit: int = 100):
        from ..services import coach_service
        return coach_service.get_chat_history(limit)

    @envelope
    def decide_chat_adjustments(self, message_id: int, approve: bool):
        from ..services import coach_service
        return coach_service.decide_chat_adjustments(message_id, approve)

    # ---- 活动 ----
    @envelope
    def list_activities(self, start_date: str | None = None, end_date: str | None = None,
                        source: str | None = None, limit: int = 200, offset: int = 0):
        from ..db.repos import activity_repo
        rows = activity_repo.list_activities(start_date, end_date, source, limit, offset)
        return [_with_workout_classification(a) for a in rows]

    @envelope
    def get_activity(self, activity_id: int):
        from ..db.repos import activity_repo
        from ..utils import jsonutil
        a = activity_repo.get_activity(activity_id)
        if not a:
            return None
        a["laps"] = jsonutil.loads(a.get("laps_json"))
        a["structure"] = jsonutil.loads(a.pop("structure_json", None)) or []
        a["samples"] = activity_repo.get_samples(activity_id)
        _with_workout_classification(a, segments=a["structure"])
        return a

    @envelope
    def import_files(self, paths: list):
        from ..garmin import import_service
        return import_service.import_files(paths)

    @envelope
    def open_file_dialog(self):
        import webview
        if not webview.windows:
            raise RuntimeError("窗口未就绪")
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("FIT 文件 (*.fit)", "CSV 文件 (*.csv)", "所有文件 (*.*)"),
        )
        return list(result) if result else []

    # ---- 健康 ----
    @envelope
    def list_health(self, start_date: str, end_date: str | None = None):
        from ..db.repos import health_repo
        return health_repo.get_health(start_date, end_date)

    @envelope
    def list_weekly_pace_hr(self, days: int = 120):
        """各周平均配速与平均心率（ISO 周聚合），供「配速-心率变化曲线」。"""
        from datetime import date, timedelta
        from ..db.repos import activity_repo
        from ..domain.workout_analysis import weekly_pace_hr
        end = date.today()
        start = end - timedelta(days=int(days))
        acts = activity_repo.list_activities(start.isoformat(), limit=3000)
        return weekly_pace_hr(acts, start, end)

    @envelope
    def list_pace_bin_hr(self, days: int = 120):
        """同配速不同时期平均心率对照（30s 配速梯度分桶 × 等宽时期）。"""
        from datetime import date, timedelta
        from ..db.repos import activity_repo
        from ..domain.workout_analysis import pace_bin_hr
        end = date.today()
        start = end - timedelta(days=int(days))
        acts = activity_repo.list_activities(start.isoformat(), limit=3000)
        return pace_bin_hr(acts, start, end)

    @envelope
    def read_clipboard(self):
        """读系统剪贴板文本（WebView2 不放开粘贴，前端 Ctrl+V 回填用）。"""
        from ..utils import clipboard
        return {"text": clipboard.read_text() or ""}

    # ---- Garmin 同步 ----
    @envelope
    def sync_garmin(self):
        """后台线程执行同步（避免阻塞 UI），结果经 sync_state 查询。"""
        import threading
        from ..garmin import sync_service

        # 前置快速失败：未配置账号时线程里的异常 UI 看不见，这里直接返回错误
        if not settings_service.is_mock_mode():
            username, password = settings_service.get_garmin_credentials()
            if not username or not password:
                raise RuntimeError("尚未保存 Garmin 账号，请先在设置页填写账号密码并点击「保存账号」")

        def _run():
            try:
                stats = sync_service.sync_all()
                log.info("同步线程完成: %s", stats)
            except Exception:
                # 失败原因已由 sync_service 写入 sync_state.last_error，UI 轮询可见
                log.exception("同步线程失败")

        threading.Thread(target=_run, daemon=True, name="garmin-sync").start()
        return {"started": True}

    @envelope
    def refresh_activity_detail(self, activity_id: int):
        """按需拉取 Garmin 活动详情（采样曲线）。"""
        from ..db.repos import activity_repo
        from ..garmin import sync_service
        a = activity_repo.get_activity(activity_id)
        if not a or a["source"] != "garmin":
            raise RuntimeError("仅 Garmin 同步的活动支持拉取详情")
        aid = sync_service.fetch_activity_detail(a["external_id"])
        return {"activity_id": aid}

    # ---- 演示数据（仅 mock 模式允许） ----
    @envelope
    def seed_demo(self, weeks: int = 8, clear: bool = False):
        if not settings_service.is_mock_mode():
            raise RuntimeError("仅 mock 模式可用")
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
        from seed_demo_data import seed as _seed
        return _seed(weeks, clear)
