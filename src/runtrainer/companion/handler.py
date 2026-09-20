"""手机伴侣 HTTP 处理器：web/mobile 静态 + 受限 JSON API。

安全模型（威胁模型详见 README，HTTP 明文只防好奇邻居）：
- 业务请求须带 X-Companion-PIN 头，每请求与 keyring 最新 PIN 比对
  （hmac.compare_digest 防时序侧信道）；重置 PIN 即时生效
- PinGate：每连错满 5 次锁定，窗口 5s→15s→60s→300s 递增；锁定期一律
  429 + retry_after（正确 PIN 也不放行，防穷举）
- 白名单 9 方法，其余一律 403（凭据类/文件类/桌面专属/破坏性全禁）
- 长任务（AI 聊天/建议）后台线程 + request_id 幂等 + 202 轮询
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import threading
import time
from http.server import SimpleHTTPRequestHandler

from .. import config
from ..api.bridge import Api
from ..services import settings_service
from .tasks import get_store

log = logging.getLogger(__name__)

MAX_BODY = 1_048_576  # 1MB

# 白名单：方法 → (必填参数{名:类型}, 可选参数{名:类型}, 是否长任务)
_SPECS: dict[str, tuple[dict[str, type], dict[str, type], bool]] = {
    "get_dashboard": ({}, {}, False),
    "get_coach_snapshot": ({}, {}, False),
    "decide_coach_advice": ({"approve": bool}, {}, False),
    "request_coach_advice": ({}, {"extra_requested": bool, "user_note": str}, True),
    "get_active_plan": ({}, {}, False),
    "get_plan_workouts": ({"plan_id": int},
                          {"start_date": str, "end_date": str}, False),
    "get_chat_history": ({}, {"limit": int}, False),
    "coach_chat": ({"message": str}, {"request_id": str}, True),
    "decide_chat_adjustments": ({"message_id": int, "approve": bool}, {}, False),
}


def _validate(spec: tuple, body: dict) -> tuple[dict | None, str | None]:
    req, opt, _ = spec
    args: dict = {}
    for name, typ in req.items():
        if name not in body:
            return None, f"缺少参数 {name}"
        if _bad_type(body[name], typ):
            return None, f"参数 {name} 类型错误"
        args[name] = body[name]
    for name, typ in opt.items():
        if name not in body or body[name] is None:
            continue
        if _bad_type(body[name], typ):
            return None, f"参数 {name} 类型错误"
        args[name] = body[name]
    if "message" in args and len(args["message"]) > 2000:
        return None, "message 过长（最多 2000 字）"
    return args, None


def _bad_type(v, typ: type) -> bool:
    if typ is int:
        return isinstance(v, bool) or not isinstance(v, int)
    return not isinstance(v, typ)


_monotonic = time.monotonic  # 测试注入点


class PinGate:
    """PIN 连续失败退避。"""

    MAX_FAILS = 5
    LOCK_STEPS = (5, 15, 60, 300)

    def __init__(self) -> None:
        self._fails = 0
        self._locked_until = 0.0

    def retry_after(self) -> int:
        left = self._locked_until - _monotonic()
        return max(0, int(math.ceil(left)))

    @property
    def is_locked(self) -> bool:
        return self.retry_after() > 0

    def register_failure(self) -> None:
        self._fails += 1
        if self._fails % self.MAX_FAILS == 0:
            step = min(self._fails // self.MAX_FAILS - 1,
                       len(self.LOCK_STEPS) - 1)
            self._locked_until = _monotonic() + self.LOCK_STEPS[step]

    def on_success(self) -> None:
        self._fails = 0


_api_instance: Api | None = None
_api_lock = threading.Lock()


def _api() -> Api:
    global _api_instance
    with _api_lock:
        if _api_instance is None:
            _api_instance = Api()
        return _api_instance


class CompanionHandler(SimpleHTTPRequestHandler):
    server_version = "SuperTrainerCompanion/1.0"

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, *args) -> None:  # 同 _QuietHandler：不打 stderr
        pass

    def end_headers(self) -> None:
        # 与桌面端约定一致：全部禁缓存，改版重启即新
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def list_directory(self, path) -> None:  # 禁目录列表
        self.send_error(404)

    # ---- 响应辅助 ----
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 轮询场景不需要 keep-alive；主动关闭避免未读请求体引发 RST 吞响应
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            self._send_json(413, {"ok": False, "error": "请求体过大"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return None
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
            return None
        return data

    def _check_pin(self) -> bool:
        """鉴权：锁定期 429；缺 PIN 401 不计数；错 PIN 401 且计数。"""
        gate: PinGate = self.server.pin_gate
        if gate.is_locked:
            self._send_json(429, {"ok": False, "error": "尝试次数过多，请稍后再试",
                                  "retry_after": gate.retry_after()})
            return False
        provided = self.headers.get("X-Companion-PIN") or ""
        expected = settings_service.get_companion_pin() or ""
        if not provided:
            self._send_json(401, {"ok": False, "error": "缺少 X-Companion-PIN"})
            return False
        if not hmac.compare_digest(provided.encode("utf-8"),
                                   expected.encode("utf-8")):
            gate.register_failure()
            self._send_json(401, {"ok": False, "error": "PIN 错误",
                                  "retry_after": gate.retry_after()})
            return False
        gate.on_success()
        return True

    # ---- GET ----
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/health":  # 配对页连通性探测，免鉴权
            self._send_json(200, {"app": config.APP_TITLE,
                                  "service": config.APP_NAME,
                                  "engine_version": config.ENGINE_VERSION})
            return
        if path.startswith("/api/task/"):
            task = get_store().get(path.rsplit("/", 1)[-1])
            if task is None:
                self._send_json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._send_json(200, task)
            return
        if path.startswith("/api/"):
            self._send_json(404, {"ok": False, "error": "未知接口"})
            return
        super().do_GET()  # web/mobile 静态

    # ---- POST ----
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        # 一律先读请求体（上限 1MB）：任何早退路径都不留未读数据，
        # 避免 Windows 关连接时发 RST 吞掉已写出的响应
        body = self._read_body()
        if body is None:
            return
        if path == "/api/pair":
            self._handle_pair(body)
            return
        if not path.startswith("/api/"):
            self._send_json(404, {"ok": False, "error": "未知接口"})
            return
        method = path[len("/api/"):]
        spec = _SPECS.get(method)
        if spec is None:
            self._send_json(403, {"ok": False, "error": "该方法不允许手机端调用"})
            return
        if not self._check_pin():
            return
        args, err = _validate(spec, body)
        if err:
            self._send_json(400, {"ok": False, "error": err})
            return
        if spec[2]:
            self._dispatch_long(method, args)
            return
        try:
            result = getattr(_api(), method)(**args)
        except Exception as e:  # noqa: BLE001
            log.exception("伴侣接口 %s 内部错误", method)
            self._send_json(500, {"ok": False, "error": f"内部错误：{e}"})
            return
        self._send_json(200, result)  # bridge 信封透传

    def _handle_pair(self, body: dict) -> None:
        gate: PinGate = self.server.pin_gate
        if gate.is_locked:
            self._send_json(429, {"ok": False, "error": "尝试次数过多，请稍后再试",
                                  "retry_after": gate.retry_after()})
            return
        pin = body.get("pin")
        expected = settings_service.get_companion_pin() or ""
        if (not isinstance(pin, str) or not pin or not expected
                or not hmac.compare_digest(pin.encode("utf-8"),
                                           expected.encode("utf-8"))):
            gate.register_failure()
            self._send_json(401, {"ok": False, "error": "PIN 错误",
                                  "retry_after": gate.retry_after()})
            return
        gate.on_success()
        self._send_json(200, {"ok": True, "data": {"paired": True}})

    def _dispatch_long(self, method: str, args: dict) -> None:
        request_id = args.pop("request_id", None) or None
        def fn():
            return getattr(_api(), method)(**args)
        tid, cached, _new = get_store().submit(method, request_id, fn)
        if cached is not None:  # 已完成同 request_id → 零重复计费
            self._send_json(200, {"ok": True, "data": {
                "task_id": tid, "status": "done", "result": cached}})
        else:
            self._send_json(202, {"task_id": tid, "status": "running"})
