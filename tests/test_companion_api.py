"""手机伴侣 HTTP 接口（批24 M1）：真 socket 直测 handler 全链路。

fixture 起真实 CompanionServer（127.0.0.1:0 临时端口，测试不打到 LAN），
keyring 打桩、PIN 由设置生成；mock 模式（默认开）保证 coach_chat 等
长任务无需网络即可完成。
"""
import http.client
import json
import time

import pytest

from runtrainer import config
from runtrainer.services import settings_service
from runtrainer.companion.server import CompanionServer


def _req(port: int, method: str, path: str, body=None, pin=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if pin:
        headers["X-Companion-PIN"] = pin
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        j = json.loads(raw)
    except ValueError:
        j = None
    return resp.status, raw, j


@pytest.fixture
def server(tmp_path, fake_keyring):
    (tmp_path / "index.html").write_text("<html>mobile-test</html>",
                                         encoding="utf-8")
    (tmp_path / "sub").mkdir()
    settings_service.set_companion_enabled(True)
    pin = settings_service.generate_companion_pin()
    srv = CompanionServer.start(0, directory=str(tmp_path), host="127.0.0.1")
    yield srv, pin
    srv.stop()


def _wrong_pin(pin: str) -> str:
    return f"{(int(pin) + 1) % 1_000_000:06d}"


# ---- 静态与健康 ----

def test_health_no_auth(server):
    srv, _ = server
    status, _, j = _req(srv.bound_port, "GET", "/api/health")
    assert status == 200
    assert j["app"] == config.APP_TITLE
    assert j["service"] == config.APP_NAME
    assert j["engine_version"] == config.ENGINE_VERSION


def test_static_serves_mobile_index(server):
    srv, _ = server
    status, raw, _ = _req(srv.bound_port, "GET", "/")
    assert status == 200 and "mobile-test" in raw


def test_static_no_directory_listing(server):
    srv, _ = server
    status, _, _ = _req(srv.bound_port, "GET", "/sub/")
    assert status == 404


# ---- 鉴权 ----

def test_missing_pin_401(server):
    srv, _ = server
    status, _, j = _req(srv.bound_port, "POST", "/api/get_dashboard", body={})
    assert status == 401 and j["ok"] is False


def test_wrong_pin_401(server):
    srv, pin = server
    status, _, j = _req(srv.bound_port, "POST", "/api/get_dashboard",
                        body={}, pin=_wrong_pin(pin))
    assert status == 401 and "PIN" in j["error"]


def test_correct_pin_passthrough(server):
    srv, pin = server
    status, _, j = _req(srv.bound_port, "POST", "/api/get_dashboard",
                        body={}, pin=pin)
    assert status == 200 and j["ok"] is True and isinstance(j["data"], dict)


def test_pair_endpoint(server):
    srv, pin = server
    status, _, j = _req(srv.bound_port, "POST", "/api/pair", body={"pin": pin})
    assert status == 200 and j["ok"] is True
    status, _, _ = _req(srv.bound_port, "POST", "/api/pair",
                        body={"pin": _wrong_pin(pin)})
    assert status == 401


# ---- 白名单与参数校验 ----

def test_unknown_method_404(server):
    srv, pin = server
    # POST 不在白名单 = 403（不暴露哪些方法存在，白名单外一律拒绝）
    status, _, _ = _req(srv.bound_port, "POST", "/api/no_such_method",
                        body={}, pin=pin)
    assert status == 403
    # GET 只开放 health/task/静态，其余 /api/* 一律 404
    status, _, _ = _req(srv.bound_port, "GET", "/api/no_such_method")
    assert status == 404


def test_whitelist_out_403(server):
    srv, pin = server
    status, _, j = _req(srv.bound_port, "POST",
                        "/api/save_garmin_credentials",
                        body={"username": "x", "password": "y"}, pin=pin)
    assert status == 403


def test_bad_params_400(server):
    srv, pin = server
    status, _, j = _req(srv.bound_port, "POST", "/api/get_plan_workouts",
                        body={}, pin=pin)
    assert status == 400 and "plan_id" in j["error"]
    status, _, j = _req(srv.bound_port, "POST", "/api/decide_coach_advice",
                        body={"approve": "yes"}, pin=pin)
    assert status == 400


def test_gate_lockout_429(server):
    srv, pin = server
    wrong = _wrong_pin(pin)
    for _ in range(5):
        status, _, _ = _req(srv.bound_port, "POST", "/api/get_dashboard",
                            body={}, pin=wrong)
        assert status == 401
    # 锁定后即使正确 PIN 也 429，带 retry_after
    status, _, j = _req(srv.bound_port, "POST", "/api/get_dashboard",
                        body={}, pin=pin)
    assert status == 429
    assert j["retry_after"] >= 5


# ---- 长任务（mock 模式，无网络）----

def test_coach_chat_async_and_poll(server, monkeypatch):
    """202 → 轮询 → done 生命周期（打桩 chat：mock 模式也要求已有计划，
    而本测试只关心 handler 的任务管道）。"""
    srv, pin = server
    from runtrainer.services import coach_service
    monkeypatch.setattr(coach_service, "chat",
                        lambda message: {"reply": f"echo {message}"})
    status, _, j = _req(srv.bound_port, "POST", "/api/coach_chat",
                        body={"message": "你好", "request_id": "req-poll-1"},
                        pin=pin)
    assert status == 202
    assert j["status"] == "running" and j["task_id"]
    tid = j["task_id"]
    for _ in range(100):  # mock 模式很快，至多等 5s
        st, _, tj = _req(srv.bound_port, "GET", f"/api/task/{tid}")
        if tj and tj["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert tj["status"] == "done"
    assert tj["result"]["ok"] is True


def test_coach_chat_request_id_idempotent(server, monkeypatch):
    """同 request_id 重发（手机切后台丢响应后重试）→ 缓存结果，AI 只调一次。"""
    srv, pin = server
    from runtrainer.services import coach_service
    calls = []

    def slow_chat(message):
        calls.append(message)
        time.sleep(0.5)
        return {"reply": "mock 回复"}

    monkeypatch.setattr(coach_service, "chat", slow_chat)

    status, _, j = _req(srv.bound_port, "POST", "/api/coach_chat",
                        body={"message": "hi", "request_id": "idem-1"}, pin=pin)
    assert status == 202
    tid = j["task_id"]
    # 运行中重发同 request_id → 202 同一 task_id，不重复提交
    status, _, j2 = _req(srv.bound_port, "POST", "/api/coach_chat",
                         body={"message": "hi", "request_id": "idem-1"}, pin=pin)
    assert status == 202 and j2["task_id"] == tid
    # 轮询到完成
    for _ in range(100):
        st, _, tj = _req(srv.bound_port, "GET", f"/api/task/{tid}")
        if tj and tj["status"] == "done":
            break
        time.sleep(0.05)
    assert tj["status"] == "done" and tj["result"]["ok"] is True
    # 完成后重发同 request_id → 200 直接缓存，chat 仍只被调一次
    status, _, j3 = _req(srv.bound_port, "POST", "/api/coach_chat",
                         body={"message": "hi", "request_id": "idem-1"}, pin=pin)
    assert status == 200
    assert j3["data"]["status"] == "done"
    assert len(calls) == 1


def test_task_unknown_404(server):
    srv, _ = server
    status, _, _ = _req(srv.bound_port, "GET", "/api/task/deadbeef")
    assert status == 404
