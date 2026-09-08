"""Garmin 同步编排：登录 → 档案 → 活动增量 → 每日健康 → 断点状态记录。

失败统一抛 AdapterError 并写入 sync_state.last_error，由 UI 横幅提示降级
（手动导入不受影响）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
# 同步后自动 AI 分析失败后的冷却：LLM 调用慢/429/解析失败时连点同步
# 会反复重试同一批活动（每次最长 ~2×超时）。失败后 X 秒内跳过分析，
# 只把失败原因写日志——同步本身已不被分析阻塞（分析在后台线程跑）。
AUTO_ANALYZE_COOLDOWN_S = 600

# 后台自动分析互斥（进程内）：并发同步只允许一个分析线程
_auto_analyze_lock = threading.Lock()


def _auto_analyze_worker() -> None:
    """同步后自动分析的后台线程体：失败只记日志+写冷却时间，不打扰主流程。"""
    if not _auto_analyze_lock.acquire(blocking=False):
        log.info("已有后台自动分析在跑，本轮跳过")
        return
    try:
        from ..services import coach_service
        state = sync_repo.get_sync_state(SOURCE)
        meta = json.loads(state["meta_json"]) if state["meta_json"] else {}
        fail_ts = meta.get("analysis_fail_ts")
        if fail_ts and time.time() - float(fail_ts) < AUTO_ANALYZE_COOLDOWN_S:
            log.info("自动分析在失败冷却期内（%ds 前失败），本轮跳过", time.time() - float(fail_ts))
            return
        res = coach_service.auto_analyze_new_activities()
        if res:
            log.info("后台自动分析完成：%s", res)
    except Exception as e:
        log.warning("同步后自动分析失败（后台，非致命，%ds 内不再重试）: %s",
                    AUTO_ANALYZE_COOLDOWN_S, e)
        try:
            # 写失败冷却时间（重读合并：主流程可能刚推进过游标/last_stats）
            state = sync_repo.get_sync_state(SOURCE)
            meta = json.loads(state["meta_json"]) if state["meta_json"] else {}
            meta["analysis_fail_ts"] = time.time()
            sync_repo.set_sync_state(SOURCE, meta=meta)
        except Exception:
            pass
    finally:
        _auto_analyze_lock.release()


def _maybe_auto_analyze_async() -> str | None:
    """把同步后自动分析移出同步关键路径：Garmin 数据入库后立即返回，
    AI 分析在 daemon 线程慢慢跑（消息就绪后教练页可见）。

    此前分析同步执行——模型慢/429/超时会拖住同步按钮几十秒到数分钟，
    是「同步慢」的主因（Garmin 拉取本身秒级）。返回给 stats 的提示文案。
    """
    try:
        state = sync_repo.get_sync_state(SOURCE)
        meta = json.loads(state["meta_json"]) if state["meta_json"] else {}
        fail_ts = meta.get("analysis_fail_ts")
        if fail_ts and time.time() - float(fail_ts) < AUTO_ANALYZE_COOLDOWN_S:
            return "AI 自动分析在冷却期内跳过（上次分析失败，稍后再试）"
        threading.Thread(target=_auto_analyze_worker, daemon=True).start()
        return "AI 分析后台进行中（教练页稍后可见结果）"
    except Exception as e:
        log.warning("自动分析启动失败（非致命）: %s", e)
        return None


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
        for a in adapter.fetch_activities(since, limit=100):
            _, is_new = activity_repo.upsert_activity(_activity_to_row(a))
            created += 1 if is_new else 0
        stats["activities"] = created

        # 2.5) 详情回填：采样曲线（心率/配速/步频）+ 训练内容分段
        #      （间歇/休息识别）。仅真实模式；窗口内「新活动 + 已存但缺详情
        #      的旧活动」都回填——分页修复前的老数据只有概要，圈级/课程级
        #      信息缺失。单条失败不影响整体（下轮重试）。
        #      missing 直接查 DB 投影（本轮新活动概要已落库，自然含在其中），
        #      判据带 detail_attempted：真实无采样的活动拉过即不再重拉。
        if not settings_service.is_mock_mode():
            cutoff = int((datetime.now(timezone.utc)
                          - timedelta(days=DETAIL_BACKFILL_WINDOW_DAYS)).timestamp())
            missing = activity_repo.list_detail_missing(cutoff, SOURCE)
            if missing:
                filled = _backfill_activity_details(missing)
                if filled:
                    stats["details_backfilled"] = filled

        # 3) 每日健康（断点：上次健康同步日期；回溯上限一年、每轮 90 天分批）
        last_health = meta.get("last_health_date")
        start = (date.fromisoformat(last_health) + timedelta(days=1)) if last_health else \
            dates.today() - timedelta(days=HEALTH_MAX_BACKFILL_DAYS)
        end = min(dates.today(), start + timedelta(days=HEALTH_DAYS_PER_SYNC - 1))
        if start <= end:
            try:
                days = _fetch_health_chunks(adapter, start, end)
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

        # 4.5) 同步后 → AI 教练自动分析（新训练点评 + 未来几天建议，消息
        #      kind=sync_analysis）。自 4.6 起在后台 daemon 线程执行：分析要
        #      调 LLM（慢/429/超时可达分钟级），不能让它拖住同步按钮。
        #      coach_service 按 last_analysis_act_ts 兜底去重（上轮失败/跳过的
        #      活动本轮补上）；失败有冷却（AUTO_ANALYZE_COOLDOWN_S）防连点
        #      同步反复打 LLM；成功游标才推进、不重复计费。
        if not settings_service.is_mock_mode():
            note = _maybe_auto_analyze_async()
            if note:
                stats["auto_analysis"] = note

        meta["cursor_ts"] = int(datetime.now(timezone.utc).timestamp())
        # 增量无变化的同步不要用全零统计覆盖上次结果：设置页「本次结果」
        # 保持最近一次有真实内容的摘要。复用前剔除过程性键——否则「上次
        # 重建过计划/分析过」会作为本轮结果反复显示（plan_rebuilt 每轮为
        # True 的假象即由此而来：本轮其实没重建）。
        if not (stats.get("activities") or stats.get("health_days")
                or stats.get("health_error") or stats.get("plan_rebuilt")
                or stats.get("mock_purged") or stats.get("auto_analysis")
                or stats.get("health_backfill") or stats.get("details_backfilled")
                or stats.get("max_hr_inferred")):
            if meta.get("last_stats"):
                stats = {k: v for k, v in meta["last_stats"].items()
                         if not (k.startswith("plan_") or k.startswith("auto_analysis")
                                 or k.startswith("max_hr_")
                                 or k in ("health_purged", "mock_purged"))}
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
    # 保留已存活动的主体字段（start_ts 以已存为准）。
    # UNIQUE(source, external_id) 索引直达，替代此前全表线性扫描（N 活动 O(N²)）
    existing = activity_repo.get_by_external(SOURCE, detail.external_id)
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


def _backfill_activity_details(missing: list[dict]) -> int:
    """缺详情活动回填（DETAIL_BACKFILL_WINDOW_DAYS 窗口内），并行加速。

    - 速度：串行 2 请求/活动改为小线程池（3 worker，各自独立登录实例——
      garmin client 非线程安全不能跨线程共享；Cloudflare 防护下并发不宜过大）。
      首同步数百活动时墙钟约省 75%。
    - 准确：单条失败不影响整体（下轮重试）；成功即打 detail_attempted 标记，
      真实无采样的活动不再每轮被重复拉取（断掉永久重拉环）。
    """
    from ..domain.workout_analysis import analyze_structure
    if not missing:
        return 0
    cutoff = int((datetime.now(timezone.utc)
                  - timedelta(days=DETAIL_BACKFILL_WINDOW_DAYS)).timestamp())

    # 每个线程独立持有一个已登录适配器（threading.local 缓存，线程池复用）
    _local = threading.local()

    def _adapter() -> GarminAdapter:
        a = getattr(_local, "adapter", None)
        if a is None:
            a = get_adapter()
            a.login()
            _local.adapter = a
        return a

    def _fill_one(m: dict) -> int:
        if m["start_ts"] < cutoff:
            return 0
        external_id = m["external_id"]
        try:
            detail = _adapter().fetch_activity_detail(external_id)
            structure = analyze_structure(detail.laps, detail.duration_s, detail.distance_m,
                                          samples=detail.samples)
            aid = _save_activity_detail(detail, structure)
            activity_repo.mark_detail_attempted(aid)
            return 1
        except Exception as e:
            log.warning("活动 %s 详情回填失败（非致命，下轮重试）: %s", external_id, e)
            return 0

    filled = 0
    workers = min(3, len(missing))
    if workers <= 1:
        for m in missing:
            filled += _fill_one(m)
        return filled
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n in ex.map(_fill_one, missing):
            filled += n
    return filled


def _fetch_health_chunks(adapter: GarminAdapter, start: date, end: date) -> list[RawDailyHealth]:
    """健康逐日拉取：mock 串行（演示实例已登录）；真实模式切 3 段并行。

    每段独立登录实例（client 非线程安全）。段内单日失败由适配器跳过
    （断点按成功日推进）；整段失败仅日志——该段断点不推进，下轮重拉该段，
    不拖累其余成功段。
    """
    total_days = (end - start).days + 1
    if isinstance(adapter, MockAdapter) or total_days <= 1:
        return adapter.fetch_daily_health(start, end)
    spans = []
    seg_days = max(1, (total_days + 2) // 3)
    s = start
    while s <= end:
        e = min(end, s + timedelta(days=seg_days - 1))
        spans.append((s, e))
        s = e + timedelta(days=1)

    def _seg(s0: date, s1: date) -> list:
        a = get_adapter()
        a.login()
        return a.fetch_daily_health(s0, s1)

    days: list = []
    with ThreadPoolExecutor(max_workers=len(spans)) as ex:
        futures = [ex.submit(_seg, s0, s1) for s0, s1 in spans]
        for f in futures:
            try:
                days.extend(f.result())
            except AdapterError as e:
                log.warning("健康分片拉取失败（该分段下轮重试）: %s", e)
    return days


def fetch_activity_detail(external_id: str) -> int:
    """按需拉取单条活动详情（含采样曲线），更新 DB。返回活动 id。"""
    adapter = get_adapter()
    adapter.login()
    detail = adapter.fetch_activity_detail(external_id)
    aid = _save_activity_detail(detail, None)
    # 无论详情有无采样都标记：无采样活动不被后续同步永久重拉
    activity_repo.mark_detail_attempted(aid)
    return aid
