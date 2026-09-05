"""M2：同步服务测试（MockAdapter 全链路 + 降级路径）。"""
import json
from datetime import timedelta

import pytest

from runtrainer.db.repos import activity_repo, health_repo, profile_repo, sync_repo
from runtrainer.garmin import sync_service
from runtrainer.garmin.adapter import AdapterError


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
