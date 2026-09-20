"""伴侣 HTTP 服务：开关开启时 bind 0.0.0.0:8088，只服务 web/mobile + /api/*。

与桌面静态服务（127.0.0.1 随机端口）分属两个线程互不干扰——合在一起
会把无鉴权的桌面 UI 暴露到局域网。
"""
from __future__ import annotations

import threading
from functools import partial
from http.server import ThreadingHTTPServer

from .. import config
from .handler import CompanionHandler, PinGate

DEFAULT_PORT = 8088


class CompanionServer:
    """包装 ThreadingHTTPServer：bind 失败抛 OSError 不吞（供调用方回滚开关）。"""

    def __init__(self, httpd: ThreadingHTTPServer):
        self._httpd = httpd
        self._thread: threading.Thread | None = None

    @classmethod
    def start(cls, port: int = DEFAULT_PORT,
              directory: str | None = None,
              host: str = "0.0.0.0") -> "CompanionServer":
        mobile_dir = directory or str(config.web_dir() / "mobile")
        handler = partial(CompanionHandler, directory=mobile_dir)
        httpd = ThreadingHTTPServer((host, port), handler)  # 占用即抛 OSError
        httpd.daemon_threads = True
        httpd.pin_gate = PinGate()  # 共享退避状态（跨请求/连接）
        server = cls(httpd)
        server._thread = threading.Thread(target=httpd.serve_forever,
                                          daemon=True, name="companion-http")
        server._thread.start()
        return server

    @property
    def bound_port(self) -> int:
        return self._httpd.server_address[1]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
