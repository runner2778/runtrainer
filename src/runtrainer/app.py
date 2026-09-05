"""pywebview 窗口启动。"""
from __future__ import annotations

import logging
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import webview

from . import config
from .api.bridge import Api
from .db import database

log = logging.getLogger(__name__)


class _QuietHandler(SimpleHTTPRequestHandler):
    """静态资源请求不打 stderr（打包版会污染控制台窗口）。"""

    def log_message(self, *args) -> None:
        pass


def _start_web_server() -> str:
    """在 127.0.0.1 随机端口起静态服务，返回 index.html 的 http URL。

    WebView2（Chromium）按 CORS 拦截 file:// 下的 ES Module，前端
    js/main.js 在 file:// 协议下无法加载、页面只剩静态外壳，必须走 http。
    服务仅监听本机回环，目录锁定 web 前端目录。
    """
    handler = partial(_QuietHandler, directory=str(config.web_dir()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/index.html"


def _restore_foreground(window) -> None:
    """确保窗口可见并位于前台。

    从脚本/后台拉起进程时，启动器的 STARTUPINFO 会把首次 ShowWindow 变成
    最小化，且发生在页面 loaded 事件之后——所以这里延迟执行，并用 Win32
    直接对 HWND 强制还原。用户双击启动时本函数是无害的空操作。
    """
    try:
        window.show()
        window.restore()
    except Exception:  # noqa: BLE001
        log.exception("窗口恢复前台失败")
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, config.APP_TITLE)
        if hwnd:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():  # 只操作本进程窗口，避免误动同名窗口
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        log.exception("Win32 窗口还原失败")


def _fallback_error(msg: str) -> None:
    """WebView 启动失败时弹原生提示（无 GUI 依赖的兜底路径）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, f"SuperTrainer 启动失败：\n{msg}\n\n"
               "若提示缺少 WebView2 运行时，请安装 Microsoft Edge WebView2 后重试。",
            "SuperTrainer", 0x10,
        )
    except Exception:  # 连弹窗都失败则只留日志
        log.exception("错误弹窗失败")


def _acquire_single_instance() -> bool:
    """Windows 命名互斥锁：重复启动直接退出，避免多窗口并发同步损坏数据。

    （曾出现 10 个实例同时运行，各自启动自动同步，列表 upsert 把详情
    回填的数据清空。）
    """
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Global\\SuperTrainerSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    # 句柄必须全程持有（GC 会释放导致锁失效），挂到模块全局
    global _mutex_handle
    _mutex_handle = handle
    return True


_mutex_handle = None


def run() -> None:
    config.ensure_dirs()
    config.init_logging()
    database.migrate()
    if not _acquire_single_instance():
        log.info("检测到应用已在运行，本次启动退出")
        return
    index = config.web_dir() / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"前端目录缺失：{index}")
    url = _start_web_server()
    log.info("前端页面: %s", url)
    window = webview.create_window(
        config.APP_TITLE,
        url,
        js_api=Api(),
        width=1320,
        height=860,
        min_size=(1024, 700),
    )

    def on_loaded() -> None:
        # 进程被以最小化方式拉起时（如脚本后台启动），延迟恢复窗口到前台
        threading.Timer(2.5, lambda: _restore_foreground(window)).start()

    window.events.loaded += on_loaded
    try:
        webview.start(debug=config.DEBUG)
    except Exception as e:
        log.exception("窗口启动失败")
        _fallback_error(str(e))
        raise
