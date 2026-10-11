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

# 后台同步进行中标志：周期自动同步 + 手动点击可能撞车，重叠会并发跑两个
# sync_all（数据层有 UNIQUE 去重，但断点游标读改写可能互相覆盖）
_SYNCING = False


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
            "ai_web_search": settings_service.get_ai_web_search(),
            "garmin_cn": settings_service.is_garmin_cn(),
            "auto_sync_enabled": settings_service.is_auto_sync_enabled(),
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
        elif key == "ai_web_search":
            settings_service.set_ai_web_search(value == "1")
        elif key == "garmin_cn":
            settings_service.set_garmin_cn(value == "1")
        elif key == "auto_sync_enabled":
            settings_service.set_auto_sync_enabled(value == "1")
        else:
            kv_repo.set_setting(key, value)
        return {"saved": True}

    # ---- 手机伴侣屏 ----
    def _companion_view(self) -> dict:
        """合并「设置里开着」与「服务真的在跑」：手机能不能连上只看 running。

        两者会不一致——绑定失败（端口被占）时开关是开的但服务没起来，
        这时必须把 error 透出去，否则用户看着开关开着却连不上。
        """
        from .. import companion
        st = companion.status()
        enabled = settings_service.get_companion_enabled()
        pin = (settings_service.get_or_create_companion_pin() if enabled
               else settings_service.get_companion_pin())
        port = st["port"] or companion.DEFAULT_PORT
        return {
            "enabled": enabled,
            "running": st["running"],
            "port": port,
            "error": st["error"],
            "pin": pin or "",
            "urls": [f"http://{ip}:{port}/" for ip in companion.lan_ips()],
        }

    @envelope
    def get_companion_status(self):
        return self._companion_view()

    @envelope
    def set_companion_enabled(self, enabled: bool):
        from .. import companion
        if enabled:
            settings_service.get_or_create_companion_pin()  # 没有 PIN 先生成
            if not companion.ensure_started():
                raise RuntimeError(
                    "启动失败：" + (companion.status()["error"] or "端口被占用"))
            settings_service.set_companion_enabled(True)
        else:
            companion.stop()
            settings_service.set_companion_enabled(False)
        return self._companion_view()

    @envelope
    def reset_companion_pin(self):
        # handler 每请求读最新 PIN，无需重启服务；重置即刻生效
        pin = settings_service.reset_companion_pin()
        return {**self._companion_view(), "pin": pin}

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
        # 恢复课配速带自愈（旧引擎误按 E 带落库 → 50–59% 恢复带）：
        # 日历每次拉课表即对齐，既有计划无需等重建即时更新
        from ..services.plan_service import ensure_recovery_pace
        ensure_recovery_pace(plan_repo.get_plan(plan_id))
        ws = plan_repo.get_workouts(plan_id, start_date, end_date)
        for w in ws:
            segs = jsonutil.loads(w.pop("segments_json", None)) or []
            for s in segs:
                # 旧计划段落无 rest_mode：按训练类型补默认
                # （R 重复跑完全恢复=走路/慢跑/静止均可；I 间歇/T 阈值组间慢跑）
                if s.get("type") in ("tempo", "reps") and not s.get("rest_mode"):
                    s["rest_mode"] = "any" if s.get("zone") == "R" else "jog"
            w["segments"] = segs
        # 完成判定（读取侧自动配对，不写库）：某课当天跑的实际跑量 ≥ 计划量
        # 一半（且 ≥0.8km）即视为已执行——用户常不手动勾完成。日历 ✓ 标记与
        # dashboard/进度口径一致；skipped/力量课不参与
        from ..domain import load_metrics
        from ..utils import dates as _dates
        from ..db.repos import activity_repo
        auto: set[int] = set()
        if start_date and end_date:
            runs: dict[str, float] = {}
            for a in activity_repo.list_activities(start_date, end_date, limit=2000):
                if a.get("distance_m") and load_metrics.is_running(a.get("sport")):
                    day = _dates.ts_to_date(a["start_ts"]).isoformat()
                    runs[day] = runs.get(day, 0.0) + a["distance_m"] / 1000.0
            auto = load_metrics.workout_auto_done(ws, runs)
        for w in ws:
            w["auto_done"] = w["id"] in auto
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
        race = _date.fromisoformat(plan["race_date"])
        # 计划口径只看课表生命周期 [start_date, race_date] 内的课：开课前/比赛
        # 日之后的旧行、预备课不参与进度与执行率（跟随课表开始/结束）
        ws = [w for w in plan_repo.get_workouts(plan["id"])
              if plan["start_date"] <= w["date"] <= plan["race_date"]]
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
        from ..domain import load_metrics
        # 实际跑步按日汇总（只认跑步：跨训练不是执行课表）
        runs_day: dict[str, float] = {}
        for a in activity_repo.list_activities(plan["start_date"], today_s, limit=3000):
            if a.get("distance_m") and load_metrics.is_running(a.get("sport")):
                day = dates.ts_to_date(a["start_ts"]).isoformat()
                runs_day[day] = runs_day.get(day, 0.0) + a["distance_m"] / 1000.0
        # 完成进度：已过日期课 + 自动完成判定（当天跑步 ≥ 半程计划量视为已执行，
        # 用户不手动勾完成也能推进进度）
        past = [w for w in ws if w["date"] <= today_s]
        auto = load_metrics.workout_auto_done(past, runs_day)
        done = sum(1 for w in past
                   if w["status"] == "completed" or w["id"] in auto)
        # 近 4 周执行覆盖（窗口不早于计划开始日；按计划日配对、逐日封顶，
        # 计划外自由跑不计——新计划前几周课少时不会用海量自由跑撑爆执行率）
        win_start = max(start, today - timedelta(days=27))
        win_rows = [w for w in ws if win_start.isoformat() <= w["date"] <= today_s]
        cov = load_metrics.plan_coverage(win_rows, runs_day)
        weeks_total = int(plan["total_weeks"])
        weeks_elapsed = max(0.0, min(float(weeks_total), (today - start).days / 7.0))
        # 今日决策徽章（与 AI 教练页同一份本地计算；画像模型有 app_state
        # 缓存，数据未变时开销很小。构建失败不拖垮日历）
        try:
            from ..services import coach_service
            ds = coach_service.decision_snapshot(today)
            decision = ds.get("decision") if ds else None
        except Exception:
            decision = None
        return {
            "today": today_s, "start_date": plan["start_date"], "race_date": plan["race_date"],
            "total_weeks": weeks_total, "weeks_elapsed": round(weeks_elapsed, 1),
            "phases": phases,
            "workouts_past": len(past), "workouts_done": done,
            "planned_km_4w": cov["planned_km"], "done_km_4w": cov["done_km"],
            "covered_km_4w": cov["covered_km"],
            "planned_days_4w": cov["planned_days"], "covered_days_4w": cov["covered_days"],
            "compliance_4w": cov["ratio"],
            "today_decision": decision,
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

    @envelope
    def run_quick_assessment(self):
        """一键评估：按最近三周训练（间歇/速度能力占大头）重估成绩并落库。

        落库后仪表盘卡片就显示这次评估；有新活动（含比赛）进库会自动重估。
        评估完顺手把课表配速对齐到新 VDOT（只改配速列、不重排课表）。
        前端拿到结果后仍应广播 sync-done，让其余页面与这张卡一致刷新。
        """
        from ..services import plan_service
        res = plan_service.quick_assessment()
        try:
            if plan_repo.get_active_plan() and res.get("vdot"):
                res["pace_sync"] = plan_service.sync_plan_paces(
                    target_vdot=float(res["vdot"]))
        except Exception:  # noqa: BLE001
            log.exception("评估后配速同步失败（不影响评估结果）")
        return res

    # ---- 课表配速同步 ----
    @envelope
    def get_pace_sync_status(self):
        """课表配速 vs 当前预估 VDOT 的差距（供课表/仪表盘显示徽标）。"""
        from ..services import plan_service
        return plan_service.pace_sync_status()

    @envelope
    def sync_plan_paces(self, force: bool = False, include_past: bool = False):
        """把课表里未执行课的配速列对齐到当前预估 VDOT。

        只动 pace_zone 对应的慢/快两端配速，不重排日期、不改距离/分段/状态。
        force=True 时连「手动指定 VDOT」的目标也一并覆盖。
        """
        from ..services import plan_service
        return plan_service.sync_plan_paces(force=bool(force),
                                            include_past=bool(include_past))

    @envelope
    def get_vdot_table(self, vdot: float | None = None):
        """丹尼尔斯 VDOT 训练表：指定档位（不传则用默认档）的区间明细 + 完整矩阵。"""
        from ..domain import training_table as tt
        from ..services import plan_service
        ctx = plan_service.vdot_table_context(vdot)
        return {**ctx, **tt.daniels_table(ctx["vdot"], ctx.get("rest_hr"),
                                          ctx.get("max_hr"))}

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
    def clear_chat_history(self):
        from ..services import coach_service
        return coach_service.clear_chat_history()

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
        from ..services import plan_service
        from ..db.repos import plan_repo
        res = import_service.import_files(paths)
        out = dict(res or {})
        # 手动导入同样走「同步/导入 → 读取数据 → 成绩估计 → 各强度速度区间
        # → 调整计划配速」联动链：新水平与现计划 VDOT 差 ≥0.2 时原地同步配速
        # （与 Garmin 同步路径同入口；不重排课表）
        if out.get("imported", 0) > 0 and plan_repo.get_active_plan():
            synced = plan_service.refresh_active_plan()
            if synced:
                out["plan_rebuilt"] = True
                out["plan_vdot"] = synced.get("target_vdot")
        return out

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

    # ---- 手动补录活动 ----
    @envelope
    def add_manual_activity(self, payload: dict):
        """手动新增一条活动（距离/用时/心率/步频/步幅/主观感受/用力程度）。

        入库后与手表同步的活动完全同权，并沿联动链重估水平、同步课表配速。
        """
        from ..services import manual_service
        from ..services import plan_service
        res = manual_service.add_activity(payload or {})
        # 与同步/导入同一条联动链：新证据 → 重估水平 → 课表配速同步
        try:
            if plan_repo.get_active_plan():
                sync = plan_service.sync_plan_paces()
                res["pace_sync"] = sync
        except Exception:  # noqa: BLE001
            log.exception("补录后配速同步失败（不影响录入本身）")
        return res

    @envelope
    def update_activity_fields(self, activity_id: int, fields: dict):
        """改活动字段（含主观感受）：距离/用时改了自动重算配速与步幅。"""
        from ..services import manual_service
        return manual_service.edit_activity(int(activity_id), fields or {})

    @envelope
    def delete_activity_record(self, activity_id: int):
        from ..services import manual_service
        ok = manual_service.delete_activity(int(activity_id))
        return {"deleted": bool(ok)}

    @envelope
    def recognize_activity_screenshot(self, path: str | None = None):
        """截图识别：不传 path 则弹文件选择框，用系统 OCR 读出字段供用户确认。

        只做「读」不做「写」——识别结果回填到表单，用户核对后再走
        add_manual_activity 落库（OCR 必然有误差，不该直接入库）。
        """
        import base64
        from pathlib import Path
        from ..services import ocr_service

        if not path:
            import webview
            if not webview.windows:
                raise RuntimeError("窗口未就绪")
            picked = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("图片 (*.png;*.jpg;*.jpeg;*.bmp;*.webp)", "所有文件 (*.*)"),
            )
            if not picked:
                return {"cancelled": True}
            path = picked[0] if isinstance(picked, (list, tuple)) else picked

        res = ocr_service.recognize_activity(str(path))
        out = dict(res)
        # 预览缩略：超过 8MB 不回传（base64 会膨胀 1/3，大图会卡住 UI）
        try:
            p = Path(str(path))
            if p.is_file() and p.stat().st_size <= 8 * 1024 * 1024:
                out["preview_data_url"] = (
                    "data:image/*;base64,"
                    + base64.b64encode(p.read_bytes()).decode("ascii"))
        except Exception:  # noqa: BLE001
            pass
        return out

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
        # 轻量投影行：分析只消费五列，不走 SELECT * 拖全行大 JSON 字段
        acts = activity_repo.list_pace_hr_rows(start.isoformat(), end.isoformat())
        return weekly_pace_hr(acts, start, end)

    @envelope
    def list_weekly_subjective(self, days: int = 180):
        """各周平均主观感受/用力程度（ISO 周聚合），供健康页「主观负荷」曲线。"""
        from datetime import date, timedelta
        from ..db.repos import activity_repo
        from ..domain.workout_analysis import weekly_subjective
        end = date.today()
        start = end - timedelta(days=int(days))
        rows = activity_repo.list_subjective_rows(start.isoformat(), end.isoformat())
        return weekly_subjective(rows, start, end)

    @envelope
    def list_pace_bin_hr(self, days: int = 120):
        """同配速不同时期平均心率对照（30s 配速梯度分桶 × 等宽时期）。"""
        from datetime import date, timedelta
        from ..db.repos import activity_repo
        from ..domain.workout_analysis import pace_bin_hr
        end = date.today()
        start = end - timedelta(days=int(days))
        acts = activity_repo.list_pace_hr_rows(start.isoformat(), end.isoformat())
        return pace_bin_hr(acts, start, end)

    @envelope
    def list_pace_bin_weekly(self, weeks: int = 16):
        """同配速档的近 N 个自然周心率趋势（20s 梯度 × 次内分段 × 滚动窗口周曲线）。"""
        from datetime import date, timedelta
        from ..db.repos import activity_repo
        from ..domain.workout_analysis import (
            PACE_TREND_EDGE_WEEKS, pace_bin_weekly, pace_hr_segments)
        weeks = max(2, min(52, int(weeks)))   # 边界钳制：前端传值不可信
        end = date.today()
        # 多取 roll-1 周：最早的几个点也要有等宽的滚动窗口（见函数说明）
        start = end - timedelta(weeks=weeks + PACE_TREND_EDGE_WEEKS - 1)
        # 段级口径：结构里的 work 段才能让 3:20 这类快档有样本（整场均值会把
        # 间歇课抹平）。无结构的活动由 pace_hr_segments 回退整场，不丢数据。
        rows = activity_repo.list_structure_rows(start.isoformat(), end.isoformat())
        return pace_bin_weekly(pace_hr_segments(rows), end, weeks=weeks)

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

        global _SYNCING
        if _SYNCING:
            return {"started": False, "already_running": True}
        _SYNCING = True

        def _run():
            global _SYNCING
            try:
                stats = sync_service.sync_all()
                log.info("同步线程完成: %s", stats)
            except Exception:
                # 失败原因已由 sync_service 写入 sync_state.last_error，UI 轮询可见
                log.exception("同步线程失败")
            finally:
                _SYNCING = False

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
