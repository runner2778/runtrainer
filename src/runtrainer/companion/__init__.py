"""手机伴侣屏门面：开关开启时在局域网提供手机访问（HTTP + 6 位 PIN）。

- 默认关闭；app 启动时按设置热启用，启动失败仅日志不阻断主程序
- bind 0.0.0.0:8088（LAN 可见），PIN 存 keyring，威胁模型见 README
- 进程内单例（模块级 _server），stop 后再次 ensure_started 可重启
- 包内/引用方一律相对导入：Python 3.14 stdlib 有顶层 companion 包，
  绝对导入会撞名
"""
from __future__ import annotations

import logging
import socket
import threading

from .server import DEFAULT_PORT, CompanionServer  # noqa: F401

log = logging.getLogger(__name__)

_server: CompanionServer | None = None
_lock = threading.Lock()
_last_error: str | None = None


def lan_ips() -> list[str]:
    """本机局域网 IPv4 列表（手机访问地址，多网卡全列）。

    UDP connect 到 8.8.8.8:80 只向路由表要默认出接口的源地址（不真正
    发包）；取不到时回退 getaddrinfo 枚举本机 IPv4，再失败返回空。
    """
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def ensure_started(port: int = DEFAULT_PORT) -> bool:
    """开启服务（幂等）。bind 失败（端口占用）→ 记 _last_error 返回 False。"""
    global _server, _last_error
    with _lock:
        if _server is not None and _server.is_alive():
            return True
        try:
            _server = CompanionServer.start(port)
            _last_error = None
            log.info("手机伴侣服务已启动 0.0.0.0:%d", _server.bound_port)
            return True
        except OSError as e:
            _last_error = str(e)
            log.warning("手机伴侣服务启动失败：%s", e)
            return False


def stop() -> None:
    global _server
    with _lock:
        if _server is not None:
            try:
                _server.stop()
            except Exception:  # noqa: BLE001
                pass
            _server = None


def is_running() -> bool:
    with _lock:
        return _server is not None and _server.is_alive()


def status() -> dict:
    with _lock:
        running = _server is not None and _server.is_alive()
        return {"running": running,
                "port": _server.bound_port if running else None,
                "error": _last_error}
