"""M2：同步服务测试（MockAdapter 全链路 + 降级路径 + 批19 同步优化）。"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from runtrainer.db.repos import activity_repo, health_repo, profile_repo, sync_repo
from runtrainer.garmin import sync_service
from runtrainer.garmin.adapter import AdapterError, RawActivity, RawDailyHealth


def _mk_activity(eid: str, days_ago: int, dist_m: float = 5000.0,
                 dur_s: float = 1800.0) -> RawActivity:
    """窗口内测试活动（非 mock 桩数据）。"""
    start = int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())
    return RawActivity(external_id=eid, name="测试跑", sport="running",
                       start_ts=start, tz_offset_min=480,
                       duration_s=dur_s, distance_m=dist_m)


class FakeGarmin:
    """真实模式桩：概要/健康可编程，详情无采样（正测「无采样不重拉」）。"""

    def __init__(self, activities: list[RawActivity] | None = None):
        self.activities = list(activities or [])
        self.detail_eids: list[str] = []
        self.health_calls: list[tuple[date, date]] = []

    def login(self, username=None, password=None):
        return None

    def fetch_profile(self) -> dict:
        return {}

    def fetch_activities(self, since, limit=100):
        return list(self.activities)

    def fetch_activity_detail(self, external_id: str) -> RawActivity:
        self.detail_eids.append(external_id)
        src = next(a for a in self.activities if a.external_id == external_id)
        return RawActivity(external_id=external_id, name=src.name, sport="running",
                           start_ts=src.start_ts, tz_offset_min=480,
                           duration_s=src.duration_s, distance_m=src.distance_m,
                           samples=[])  # 详情确实无采样曲线

    def fetch_daily_health(self, start: date, end: date) -> list[RawDailyHealth]:
        self.health_calls.append((start, end))
        out, d = [], start
        while d <= end:
            out.append(RawDailyHealth(date=d))
            d += timedelta(days=1)
        return out


def test_sync_all_full_flow():
    """mock 模式下：档案/活动/健康/断点状态全链路。"""
    stats = sync_service.sync_all()
    assert stats["activities"] > 0
    assert stats["health_days"] > 0
    assert profile_repo.get_profile()["nickname"] == "演示用户"
    # 活动落库 source=garmin
    acts = activity_repo.list_activities(source="garmin", limit=500)
    assert len(acts) == stats["activities"]
    assert all(a["external_id"].startswith("mock_") for a in acts)
    # 健康数据按日期一行
    assert health_repo.get_health("1970-01-01")
    # 断点状态
    state = sync_repo.get_sync_state("garmin")
    assert state["last_sync_ts"] is not None
    assert state["last_error"] is None


def test_sync_incremental_no_duplicates():
    sync_service.sync_all()
    n1 = activity_repo.count_activities()
    sync_service.sync_all()  # 第 2 轮：健康分批回填推进到下一批
    assert activity_repo.count_activities() == n1  # 活动去重：无新增
    rows = health_repo.get_health("1970-01-01")
    dates = [r["date"] for r in rows]
    assert len(dates) == len(set(dates))  # 健康按日期覆盖，无重复行


def test_sync_health_backfill_chunks(monkeypatch):
    """一年回溯分批：每轮最多 HEALTH_DAYS_PER_SYNC 天，断点推进到该批末尾。"""
    from runtrainer.utils import dates
    sync_service.sync_all()
    meta = json.loads(sync_repo.get_sync_state("garmin")["meta_json"])
    # 首批：today-365 起 90 天；断点 = 批末，未追平 today 时带回溯提示
    assert meta["last_health_date"] == (dates.today() - timedelta(days=365 - 89)).isoformat()
    st = meta["last_stats"]
    assert st["health_days"] == 90
    assert "回溯中" in st["health_backfill"]


def test_sync_error_recorded(monkeypatch):
    class BrokenAdapter:
        name = "garmin"

        def login(self):
            raise AdapterError("Cloudflare 拦截")

    monkeypatch.setattr(sync_service, "get_adapter", lambda: BrokenAdapter())
    with pytest.raises(AdapterError):
        sync_service.sync_all()
    state = sync_repo.get_sync_state("garmin")
    assert "Cloudflare" in state["last_error"]


def test_sync_missing_credentials(monkeypatch):
    """非 mock 模式且无凭据 → 明确错误，且错误写入 sync_state（UI 可见）。"""
    from runtrainer.services import settings_service
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_garmin_credentials", lambda: (None, None))
    with pytest.raises(AdapterError, match="配置 Garmin 账号"):
        sync_service.sync_all()
    state = sync_repo.get_sync_state("garmin")
    assert "配置 Garmin 账号" in state["last_error"]


def test_sync_failure_preserves_cursor(monkeypatch):
    """失败不清空断点游标，下次同步不会跳过失败窗口。"""
    sync_service.sync_all()
    meta_before = sync_repo.get_sync_state("garmin")["meta_json"]

    class BrokenAdapter:
        name = "garmin"

        def login(self):
            raise AdapterError("Cloudflare 拦截")

    monkeypatch.setattr(sync_service, "get_adapter", lambda: BrokenAdapter())
    with pytest.raises(AdapterError):
        sync_service.sync_all()
    state = sync_repo.get_sync_state("garmin")
    assert "Cloudflare" in state["last_error"]
    assert state["meta_json"] == meta_before


def test_fetch_activity_detail():
    sync_service.sync_all()
    acts = activity_repo.list_activities(source="garmin", limit=1)
    aid = sync_service.fetch_activity_detail(acts[0]["external_id"])
    a = activity_repo.get_activity(aid)
    assert a["has_samples"] == 1
    assert len(activity_repo.get_samples(aid)) > 0


def test_list_detail_missing_filters_attempted_and_window():
    """缺详情扫描（批19）：已成功拉过（无采样）不重拉、窗口外不重拉。"""
    now = datetime.now(timezone.utc)
    cutoff = int((now - timedelta(days=365)).timestamp())

    def seed(eid, days_ago, attempted):
        aid, _ = activity_repo.upsert_activity({
            "source": "garmin", "external_id": eid, "file_path": None,
            "name": "x", "sport": "running",
            "start_ts": int((now - timedelta(days=days_ago)).timestamp()),
            "tz_offset_min": 480, "has_samples": 0})
        if attempted:
            activity_repo.mark_detail_attempted(aid)

    seed("a-new", 5, False)     # 窗口内未尝试 → 待回填
    seed("a-done", 8, True)     # 拉过但无采样 → 跳过（断永久重拉环）
    seed("a-old", 400, False)   # 窗口外 → 不回溯
    missing = activity_repo.list_detail_missing(cutoff, "garmin")
    assert [m["external_id"] for m in missing] == ["a-new"]
    # (source, external_id) 索引直达 + 标记读写
    row = activity_repo.get_by_external("garmin", "a-done")
    assert row and row["detail_attempted"] == 1
    assert row["start_ts"] >= cutoff
    assert activity_repo.get_by_external("garmin", "不存在") is None


def test_real_sync_detail_backfill_no_refetch(monkeypatch):
    """真实模式全链路（批19）：详情并行回填打 detail_attempted；
    第二轮同步不再重拉无采样活动（此前每轮白费 2 请求/条）。"""
    from runtrainer.services import settings_service
    fake = FakeGarmin([_mk_activity("111", 5), _mk_activity("222", 10)])
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(sync_service, "get_adapter", lambda: fake)

    stats = sync_service.sync_all()
    assert stats["activities"] == 2
    assert stats["details_backfilled"] == 2
    assert sorted(fake.detail_eids) == ["111", "222"]
    # 详情拉过（无采样）→ 打标记；健康断点按批推进
    rows = activity_repo.list_activities(source="garmin", limit=10)
    assert all(r["detail_attempted"] == 1 for r in rows)
    # 健康拉取被切成 3 段并行（90 天 / 3 段），覆盖不重不漏
    assert len(fake.health_calls) == 3
    first_health = fake.health_calls[0][0]
    last_health = fake.health_calls[-1][1]
    assert sum((e - s).days + 1 for s, e in fake.health_calls) == 90

    # 第二轮增量同步：详情零重拉（断环生效），健康拉下一批
    fake.activities = []
    stats2 = sync_service.sync_all()
    assert stats2["activities"] == 0
    assert len(fake.detail_eids) == 2  # 未再拉任何详情
    assert fake.health_calls[-1][1] > last_health


def test_health_chunks_spans_real_mode(monkeypatch):
    """健康切 3 段并行（批19）：短范围走串行（主实例），不建多实例。"""
    from runtrainer.services import settings_service
    fake = FakeGarmin()
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(sync_service, "get_adapter", lambda: fake)
    today = date(2026, 9, 8)
    # 12 天 → 3 段 (4/4/4)；每段覆盖无缝隙
    days = sync_service._fetch_health_chunks(fake, today - timedelta(days=11), today)
    assert len(days) == 12
    assert [f"{s}→{e}" for s, e in fake.health_calls] == [
        "2026-08-28→2026-08-31", "2026-09-01→2026-09-04", "2026-09-05→2026-09-08"]
    # 同日范围：并发切段无意义，走串行单次调用
    fake.health_calls.clear()
    days = sync_service._fetch_health_chunks(fake, today, today)
    assert len(fake.health_calls) == 1 and len(days) == 1
