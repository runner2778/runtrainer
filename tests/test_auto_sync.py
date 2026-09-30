"""批27：自动同步开关（设置键 / bridge 字段）+ sync_garmin 并发锁。"""
import threading

from runtrainer.api import bridge
from runtrainer.api.bridge import Api
from runtrainer.db.repos import kv_repo
from runtrainer.services import settings_service


def test_auto_sync_default_enabled():
    """新库无该键时默认开启（用户确认的默认值）。"""
    assert settings_service.is_auto_sync_enabled() is True
    assert kv_repo.get_setting("auto_sync_enabled") is None


def test_auto_sync_set_get_roundtrip():
    settings_service.set_auto_sync_enabled(False)
    assert settings_service.is_auto_sync_enabled() is False
    settings_service.set_auto_sync_enabled(True)
    assert settings_service.is_auto_sync_enabled() is True


def test_get_settings_exposes_auto_sync():
    data = Api().get_settings()["data"]
    assert data["auto_sync_enabled"] is True


def test_bridge_set_setting_persists_auto_sync():
    Api().set_setting("auto_sync_enabled", "0")
    assert Api().get_settings()["data"]["auto_sync_enabled"] is False
    Api().set_setting("auto_sync_enabled", "1")
    assert Api().get_settings()["data"]["auto_sync_enabled"] is True


def _enable_real_mode(monkeypatch):
    """非 mock + 已配置凭据：让 sync_garmin 通过前置检查进入置锁分支。"""
    monkeypatch.setattr(settings_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(settings_service, "get_garmin_credentials", lambda: ("u", "p"))


def test_sync_garmin_rejects_when_already_syncing(monkeypatch):
    """后端锁：已在同步时再调用返回 already_running，且不起新线程。"""
    _enable_real_mode(monkeypatch)
    started: list[int] = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: started.append(1))

    bridge._SYNCING = True
    try:
        res = Api().sync_garmin()
    finally:
        bridge._SYNCING = False

    assert res == {"ok": True, "data": {"started": False, "already_running": True}}
    assert started == []  # 未创建同步线程


def test_sync_garmin_concurrent_call_blocked_until_done(monkeypatch):
    """完整链路：第一轮进行中（阻塞在 sync_all）时第二轮被拒；结束后标志复位。"""
    _enable_real_mode(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def fake_sync_all():
        calls["n"] += 1
        entered.set()
        release.wait(5)
        return {}

    monkeypatch.setattr("runtrainer.garmin.sync_service.sync_all", fake_sync_all)

    try:
        first = Api().sync_garmin()["data"]
        assert first["started"] is True
        assert entered.wait(5)  # 第一轮已进入 sync_all
        second = Api().sync_garmin()["data"]
        assert second == {"started": False, "already_running": True}
        assert calls["n"] == 1
    finally:
        release.set()
        # 等后台线程退出后标志应已复位
        for _ in range(100):
            if not bridge._SYNCING:
                break
            threading.Event().wait(0.02)
    assert bridge._SYNCING is False
    assert calls["n"] == 1
