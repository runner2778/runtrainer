"""Garmin 同步编排：登录 → 档案 → 活动增量 → 每日健康 → 断点状态记录。

失败统一抛 AdapterError 并写入 sync_state.last_error，由 UI 横幅提示降级
（手动导入不受影响）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from ..db.repos import activity_repo, health_repo, profile_repo, sync_repo
from ..domain.workout_analysis import infer_max_hr
from ..services import settings_service
from ..utils import dates
from .adapter import AdapterError, GarminAdapter, RawActivity, RawDailyHealth
from .garminconnect_adapter import GarminConnectAdapter
from .mock_adapter import MockAdapter

log = logging.getLogger(__name__)

SOURCE = "garmin"
DEFAULT_LOOKBACK_DAYS = 365
HEALTH_MAX_BACKFILL_DAYS = 365
# 健康回溯分批：每天 4 个端点（sleep/hrv/stress/summary），全年约 1500 次
# 调用，单轮拉完会超时/限流 → 每轮最多 HEALTH_DAYS_PER_SYNC 天，
# 分多轮逐步回填一年（每轮断点推进，可断点续传）
HEALTH_DAYS_PER_SYNC = 90
# 新活动详情回填窗口：与概要拉取范围一致（一年）——用户要求历史数据
# 具体到圈/课程，只回填半年会让更早的活动没有结构，课程识别只剩平均配速
DETAIL_BACKFILL_WINDOW_DAYS = 365


def get_adapter() -> GarminAdapter:
    """按设置返回适配器：mock 模式无账号亦可跑通全链路。"""
    if settings_service.is_mock_mode():
        return MockAdapter()
    username, password = settings_service.get_garmin_credentials()
    if not username or not password:
        raise AdapterError("请先在设置页配置 Garmin 账号")
    return GarminConnectAdapter(username, password, is_cn=settings_service.is_garmin_cn())


def _activity_to_row(a: RawActivity) -> dict:
    return {
        "source": SOURCE, "external_id": a.external_id, "file_path": None,
        "name": a.name, "sport": a.sport, "start_ts": a.start_ts,
        "tz_offset_min": a.tz_offset_min, "duration_s": a.duration_s,
        "distance_m": a.distance_m, "avg_pace_s_km": a.avg_pace_s_km,
        "avg_hr": a.avg_hr, "max_hr": a.max_hr, "avg_cadence": a.avg_cadence,
        "max_cadence": a.max_cadence, "stride_length_m": a.stride_length_m,
        "aerobic_te": a.aerobic_te, "anaerobic_te": a.anaerobic_te,
        "exercise_load": a.exercise_load,
        "elevation_gain_m": a.elevation_gain_m,
        "elevation_loss_m": a.elevation_loss_m, "calories": a.calories,
        "laps_json": json.dumps(a.laps, ensure_ascii=False) if a.laps else None,
        "has_samples": 0,
    }


def _health_to_fields(h: RawDailyHealth) -> dict:
    return {
        "source": SOURCE, "sleep_start_ts": h.sleep_start_ts, "sleep_end_ts": h.sleep_end_ts,
        "sleep_duration_s": h.sleep_duration_s, "deep_s": h.deep_s, "light_s": h.light_s,
        "rem_s": h.rem_s, "awake_s": h.awake_s, "sleep_score": h.sleep_score,
        "resting_hr": h.resting_hr, "avg_hr": h.avg_hr, "max_hr": h.max_hr,
        "hrv_avg_ms": h.hrv_avg_ms, "hrv_status": h.hrv_status, "stress_avg": h.stress_avg,
        "body_battery_min": h.body_battery_min, "body_battery_max": h.body_battery_max,
        "steps": h.steps, "raw_json": json.dumps(h.raw, ensure_ascii=False) if h.raw else None,
    }


def sync_all() -> dict:
    """完整同步。成功更新断点 meta；失败记录 error（保留断点）后抛 AdapterError。"""
    try:
        # get_adapter 在 try 内：未配置账号等前置失败也要写入 sync_state，
        # 否则 UI 看不到任何失败迹象（此前静默显示"正常"）。
        adapter = get_adapter()
        adapter.login()
        stats = {"profile": False, "activities": 0, "health_days": 0}

        # 1) 档案：真实适配器以新数据覆盖（含身高体重/vo2max），
        #    mock 演示仅填充空缺字段（演示数据不覆盖真实档案）
        try:
            p = adapter.fetch_profile()
            cur = profile_repo.get_profile() or {}
            if isinstance(adapter, MockAdapter):
                fill = {k: v for k, v in p.items()
                        if v is not None and not cur.get(k) and k in ("nickname", "sex", "birth_year")}
            else:
                fill = {k: v for k, v in p.items()
                        if v is not None and v != cur.get(k) and
                        k in ("nickname", "sex", "birth_year", "height_cm", "weight_kg", "vo2max")}
            if fill:
                profile_repo.upsert_profile(fill)
                stats["profile"] = True
        except Exception as e:
            log.warning("档案拉取失败（非致命）: %s", e)

        # 1.5) 真实模式下先清遗留演示健康行（upsert 合并会保留 mock 残留值，
        #      必须先删掉再回溯，避免假数据与真实数据混在一起）
        if not settings_service.is_mock_mode():
            removed_health = health_repo.purge_legacy_health()
            if removed_health:
                stats["health_purged"] = removed_health

        # 2) 活动增量（游标存于 meta.cursor_ts；失败不动游标，成功才推进）
        state = sync_repo.get_sync_state(SOURCE)
        meta = json.loads(state["meta_json"]) if state["meta_json"] else {}
        # 注意：last_sync_ts 语义是"上次尝试时间"，不能当游标用——
        # 否则每次同步都只拉最近 1 天。无游标时回溯 DEFAULT_LOOKBACK_DAYS。
        since_ts = meta.get("cursor_ts") or \
            int((datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).timestamp())
        since = datetime.fromtimestamp(since_ts - 86400, timezone.utc)  # 重叠 1 天防漏
        # fetch_activities 内部按 Garmin 页偏移翻页拉全量（历史不再被截断）
        created = 0
        new_acts: list[tuple[str, int]] = []
        for a in adapter.fetch_activities(since, limit=100):
            _, is_new = activity_repo.upsert_activity(_activity_to_row(a))
            created += 1 if is_new else 0
            if is_new:
                new_acts.append((a.external_id, a.start_ts))
        stats["activities"] = created

        # 2.5) 详情回填：采样曲线（心率/配速/步频）+ 训练内容分段
        #      （间歇/休息识别）。仅真实模式；窗口内「新活动 + 已存但缺详情
        #      的旧活动」都回填——分页修复前的老数据只有概要，圈级/课程级
        #      信息缺失。单条失败不影响整体（下轮重试）。
        if not settings_service.is_mock_mode():
            cutoff = int((datetime.now(timezone.utc)
                          - timedelta(days=DETAIL_BACKFILL_WINDOW_DAYS)).timestamp())
            missing = [(a["external_id"], a["start_ts"]) for a in
                       activity_repo.list_activities(source=SOURCE, limit=3000)
                       if a["start_ts"] >= cutoff and not a["has_samples"]]
            new_ids = {eid for eid, _ in new_acts}
            to_fill = new_acts + [(eid, ts) for eid, ts in missing if eid not in new_ids]
            if to_fill:
                filled = _backfill_activity_details(adapter, to_fill)
                if filled:
                    stats["details_backfilled"] = filled

        # 3) 每日健康（断点：上次健康同步日期；回溯上限一年、每轮 90 天分批）
        last_health = meta.get("last_health_date")
        start = (date.fromisoformat(last_health) + timedelta(days=1)) if last_health else \
            dates.today() - timedelta(days=HEALTH_MAX_BACKFILL_DAYS)
        end = min(dates.today(), start + timedelta(days=HEALTH_DAYS_PER_SYNC - 1))
        if start <= end:
            try:
                days = adapter.fetch_daily_health(start, end)
            except AdapterError as e:
                # 健康拉取失败不再中断整个同步（活动增量/课表重建已完成）：
                # 断点不动，下轮重试同一批
                days = []
                stats["health_error"] = str(e)
                log.warning("健康数据本轮拉取失败（非致命，下轮重试）: %s", e)
            pulled = []
            for h in days:
                health_repo.upsert_daily_health(h.date.isoformat(), _health_to_fields(h))
                stats["health_days"] += 1
                pulled.append(h.date)
            if pulled:
                meta["last_health_date"] = max(pulled).isoformat()
                if end < dates.today():
                    stats["health_backfill"] = \
                        f"回溯中：已到 {meta['last_health_date']}，再点同步继续分批回拉"

        # 3.5) 最大心率数据推断：采样峰值前 5 均值远比年龄公式准。
        #      仅真实模式；档案缺失时直接填，已有值与推断差 >8 bpm 才覆盖
        #      （2-3 bpm 的差异视为手表测量波动，不动用户手动设置的值）
        if not settings_service.is_mock_mode():
            peaks = activity_repo.list_sample_peak_hr(source=SOURCE, limit=5000)
            inf = infer_max_hr(peaks) if peaks else None
            if inf:
                prof = profile_repo.get_profile() or {}
                cur = prof.get("max_hr")
                if not cur or abs(cur - inf["value"]) > 8:
                    profile_repo.upsert_profile({"max_hr": inf["value"]})
                    stats["max_hr_inferred"] = \
                        f"最大心率按 {inf['n']} 次活动推断 {cur or '—'} → {inf['value']}"

        # 4) 真实同步成功后：清掉 mock/demo 演示活动，并按最新水平重建课表
        if not settings_service.is_mock_mode():
            removed = activity_repo.delete_demo_activities()
            if removed:
                stats["mock_purged"] = removed
            try:
                from ..services import plan_service
                refreshed = plan_service.refresh_active_plan()
                if refreshed:
                    stats["plan_rebuilt"] = True
                    stats["plan_vdot"] = refreshed["vdot"]
                    stats["plan_vdot_source"] = refreshed["vdot_source"]
            except Exception as e:
                log.warning("课表重建失败（非致命）: %s", e)

        # 4.5) 同步后有新训练数据 → AI 教练自动读取精确数据生成分析总结 +
        #      未来几天建议（教练消息 kind=sync_analysis，AI 教练页可见）。
        #      失败不阻断同步；去重游标在 coach_service 内，重复同步不重复计费。
        if not settings_service.is_mock_mode() and new_acts:
            try:
                from ..services import coach_service
                res = coach_service.auto_analyze_new_activities(new_acts)
                if res:
                    stats["auto_analysis"] = \
                        f"已自动分析 {res['activities_analyzed']} 条新训练"
                    if res["adjustment_count"]:
                        stats["auto_analysis"] += f"，附 {res['adjustment_count']} 条调整建议"
                    stats["auto_analysis"] += "（AI 教练页查看）"
            except Exception as e:
                log.warning("同步后自动分析失败（非致命）: %s", e)
                stats["auto_analysis_error"] = "AI 分析未生成，可在 AI 教练页手动询问"

        meta["cursor_ts"] = int(datetime.now(timezone.utc).timestamp())
        # 增量无变化的同步不要用全零统计覆盖上次结果：
        # 设置页「本次结果」保持最近一次有意义的结果
        if not (stats.get("activities") or stats.get("health_days")
                or stats.get("health_error") or stats.get("plan_rebuilt")
                or stats.get("mock_purged")):
            if meta.get("last_stats"):
                stats = meta["last_stats"]
        meta["last_stats"] = stats
        sync_repo.set_sync_state(SOURCE, meta=meta, error=None)
        log.info("Garmin 同步完成: %s", stats)
        return stats
    except Exception as e:
        err = str(e)
        sync_repo.record_sync_error(SOURCE, err)
        if isinstance(e, AdapterError):
            raise
        raise AdapterError(err) from e


def _save_activity_detail(detail: RawActivity, structure: list[dict]) -> int:
    """详情入库：采样曲线 + 训练内容结构。返回活动 id。"""
    from ..domain.workout_analysis import analyze_structure
    row = _activity_to_row(detail)
    # 保留已存活动的主体字段（start_ts 以已存为准）
    existing = None
    for a in activity_repo.list_activities(source=SOURCE, limit=1000):
        if a["external_id"] == detail.external_id:
            existing = a
            break
    if existing:
        row["start_ts"] = existing["start_ts"]
        row["tz_offset_min"] = existing["tz_offset_min"]
        # 详情缺项时保留已存值：Garmin 单活动响应字段残缺（曾把全库
        # distance/duration/心率覆盖成 None），好数据不能被 None 冲掉
        for k, v in row.items():
            if v is None and existing.get(k) is not None:
                row[k] = existing[k]
    # Garmin 列表概要常缺 averageHR：详情采样补算平均心率/配速，
    # 否则课程识别（心率区归类）对这类活动只能返回「匀速跑」
    if detail.samples:
        hrs = [s.get("hr") for s in detail.samples if s.get("hr")]
        if row.get("avg_hr") is None and hrs:
            row["avg_hr"] = round(sum(hrs) / len(hrs), 1)
        spds = [s.get("speed_mps") for s in detail.samples if s.get("speed_mps")]
        if row.get("avg_pace_s_km") is None and spds:
            row["avg_pace_s_km"] = round(1000 / (sum(spds) / len(spds)), 1)
    if structure is None:
        structure = analyze_structure(detail.laps, detail.duration_s, detail.distance_m,
                                      samples=detail.samples)
    row["structure_json"] = json.dumps(structure, ensure_ascii=False) if structure else None
    # 详情无采样时不要清掉已有采样标记（采样行仍在库里，标记要保持一致）
    row["has_samples"] = 1 if detail.samples else int(existing.get("has_samples") or 0) if existing else 0
    aid, _ = activity_repo.upsert_activity(row)
    if detail.samples:
        activity_repo.save_samples(aid, [
            (s.get("t_offset_s"), s.get("hr"), s.get("speed_mps"), s.get("cadence"), s.get("altitude_m"))
            for s in detail.samples
        ])
    return aid


def _backfill_activity_details(adapter: GarminAdapter, new_acts: list[tuple[str, int]]) -> int:
    """新活动详情回填（近 180 天窗口内），单条失败不影响整体。返回回填数。"""
    from ..domain.workout_analysis import analyze_structure
    cutoff = int((datetime.now(timezone.utc)
                  - timedelta(days=DETAIL_BACKFILL_WINDOW_DAYS)).timestamp())
    filled = 0
    for external_id, start_ts in new_acts:
        if start_ts < cutoff:
            continue
        try:
            detail = adapter.fetch_activity_detail(external_id)
            structure = analyze_structure(detail.laps, detail.duration_s, detail.distance_m,
                                          samples=detail.samples)
            _save_activity_detail(detail, structure)
            filled += 1
        except Exception as e:
            log.warning("活动 %s 详情回填失败（非致命）: %s", external_id, e)
    return filled


def fetch_activity_detail(external_id: str) -> int:
    """按需拉取单条活动详情（含采样曲线），更新 DB。返回活动 id。"""
    adapter = get_adapter()
    adapter.login()
    detail = adapter.fetch_activity_detail(external_id)
    return _save_activity_detail(detail, None)
