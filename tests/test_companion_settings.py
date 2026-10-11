"""手机伴侣屏的服务端开关（bridge）+ 手机页是否真的能取到。

分两层：开关逻辑把 companion 门面打桩（不真占 8088 端口），
静态页则起真 socket 验证 web/mobile 里确实有能打开的页面——
「后端做完了但 web/mobile 不存在」是这套功能最容易悄悄回归的地方。
"""
import http.client

import pytest

from runtrainer import config
from runtrainer.api.bridge import Api
from runtrainer.companion.server import CompanionServer
from runtrainer.services import settings_service


class _FakeCompanion:
    """companion 门面替身：记录调用，不 bind 端口。bind_ok=False 模拟端口被占。"""

    DEFAULT_PORT = 8088

    def __init__(self, bind_ok=True):
        self.bind_ok = bind_ok
        self.started = False
        self.error = None
        self.ip = "192.168.1.23"
        self.calls = []

    def status(self):
        return {"running": self.started, "port": 8088 if self.started else None,
                "error": self.error}

    def ensure_started(self, port=8088):
        self.calls.append("ensure_started")
        if not self.bind_ok:
            self.error = "address already in use"
            return False
        self.started = True
        return True

    def stop(self):
        self.calls.append("stop")
        self.started = False

    def lan_ips(self):
        return [self.ip]


@pytest.fixture
def fake(monkeypatch):
    f = _FakeCompanion()
    monkeypatch.setattr("runtrainer.companion.status", f.status)
    monkeypatch.setattr("runtrainer.companion.ensure_started", f.ensure_started)
    monkeypatch.setattr("runtrainer.companion.stop", f.stop)
    monkeypatch.setattr("runtrainer.companion.lan_ips", f.lan_ips)
    monkeypatch.setattr("runtrainer.companion.DEFAULT_PORT", _FakeCompanion.DEFAULT_PORT)
    return f


def test_status_defaults_off(fake_keyring, fake):
    d = Api().get_companion_status()["data"]
    assert d["enabled"] is False
    assert d["running"] is False
    # 关着的时候不生成 PIN（避免库里凭空多一条凭据）
    assert d["pin"] == ""


def test_enable_generates_pin_and_persists(fake_keyring, fake):
    d = Api().set_companion_enabled(True)["data"]
    assert d["enabled"] is True and d["running"] is True
    assert len(d["pin"]) == 6 and d["pin"].isdigit()
    assert d["urls"] == ["http://192.168.1.23:8088/"]
    assert settings_service.get_companion_enabled() is True
    assert fake.calls == ["ensure_started"]
    # 再读一次设置与开关一致，PIN 不重新生成
    again = Api().get_companion_status()["data"]
    assert again["pin"] == d["pin"]


def test_enable_bind_failure_reports_error(fake_keyring, fake):
    fake.bind_ok = False
    r = Api().set_companion_enabled(True)
    assert r["ok"] is False and "启动失败" in r["error"]
    # 起不来就不能把开关记成开——否则重启后仍会尝试绑定失败端口
    assert settings_service.get_companion_enabled() is False


def test_disable_stops_and_persists(fake_keyring, fake):
    Api().set_companion_enabled(True)
    d = Api().set_companion_enabled(False)["data"]
    assert d["enabled"] is False and d["running"] is False
    assert settings_service.get_companion_enabled() is False
    assert "stop" in fake.calls


def test_reset_pin_rotates(fake_keyring, fake):
    first = Api().set_companion_enabled(True)["data"]["pin"]
    second = Api().reset_companion_pin()["data"]["pin"]
    assert second != first and len(second) == 6
    assert settings_service.get_companion_pin() == second


# ---- 手机页静态资源 ----

def test_mobile_page_ships_and_serves():
    page = config.web_dir() / "mobile" / "index.html"
    assert page.exists(), "web/mobile/index.html 缺失：手机端打开会 404"
    srv = CompanionServer.start(0, host="127.0.0.1")  # directory 默认 web/mobile
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.bound_port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 200
        # 页面必须真的请求配对接口，否则配对流程是死的
        assert "SuperTrainer 手机版" in body and "/api/pair" in body
    finally:
        srv.stop()
