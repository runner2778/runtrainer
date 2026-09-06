"""验证窗口图标设置（WM_SETICON 生效）：空白窗口 + 读回句柄校验。

用法：.venv\\Scripts\\python tools\\probe_icon.py
只读无副作用：不起应用页面（不触发同步），窗口标题独立避免与运行实例混淆。
"""
import ctypes
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer import config  # noqa: E402
from runtrainer.app import _apply_window_icon  # noqa: E402

TITLE = "SuperTrainer图标验证"
out = {}


def _read_back_icon(hwnd) -> tuple[int, int]:
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_ssize_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_void_p
    small = user32.SendMessageW(hwnd, 0x7F, 0, 0)   # WM_GETICON ICON_SMALL
    big = user32.SendMessageW(hwnd, 0x7F, 1, 0)     # WM_GETICON ICON_BIG
    return small, big


def run(window):
    time.sleep(1.5)  # 等窗口原生句柄就绪
    _apply_window_icon(TITLE)
    time.sleep(0.8)
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, TITLE)
    out["hwnd"] = bool(hwnd)
    if hwnd:
        out["icon"] = dict(zip(("small", "big"), _read_back_icon(hwnd)))
        out["file"] = str(config.icon_path())
        out["file_exists"] = config.icon_path().exists()
    window.destroy()


window = webview.create_window(TITLE, html="<h1>图标验证</h1>", width=320, height=200)
window.events.loaded += run
webview.start()
print("=== 窗口图标验证 ===")
print(f"hwnd: {out.get('hwnd')} | 文件存在: {out.get('file_exists')}")
icon = out.get("icon") or {}
print(f"small 句柄非空: {bool(icon.get('small'))} | big 句柄非空: {bool(icon.get('big'))}")
