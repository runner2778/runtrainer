"""真实按键探针：SendInput 发系统级 Ctrl+V，验证 WebView2 是否把按键交给 DOM。

用法：.venv\\Scripts\\python tools\\probe_real_key.py
"""
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

url = _start_web_server()
print("加载:", url, flush=True)

TITLE = "真实按键探针"
window = webview.create_window(TITLE, url, js_api=Api(), width=1000, height=700)
out = {}

# ---- SendInput ----
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V = 0x11, 0x56


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KI(ctypes.Union):
    # 64 位下 INPUT 必须 40 字节：union 按最大成员 MOUSEINPUT(32) 计
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("i",)
    _fields_ = [("type", wintypes.DWORD), ("i", _KI)]


user32 = ctypes.WinDLL("user32", use_last_error=True)


def send_key(vk, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    r = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if r != 1:
        raise OSError(f"SendInput failed: {ctypes.get_last_error()}")


def press_ctrl_v():
    send_key(VK_CONTROL)
    send_key(VK_V)
    time.sleep(0.05)
    send_key(VK_V, up=True)
    send_key(VK_CONTROL, up=True)


def js(expr):
    return window.evaluate_js(expr)


def loaded():
    try:
        time.sleep(2.5)
        js("location.hash = '#/settings'")
        time.sleep(2.5)
        # 聚焦第一个输入框并注册 keydown/paste 探听器（写进 window 供查询）
        js("(() => { const i = document.querySelector('#page-settings input');"
           " if (!i) return 'noinput'; i.focus(); i.value = '';"
           " window.__ev = {keys: [], pastes: 0};"
           " document.addEventListener('keydown', e => {"
           "   if (e.key === 'v' || e.key === 'Control') window.__ev.keys.push(e.key); }, true);"
           " document.addEventListener('paste', () => window.__ev.pastes++, true);"
           " return 'focused'; })()")
        time.sleep(0.5)
        # 置前窗口 + 真实按键
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        out["hwnd"] = hwnd
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        press_ctrl_v()
        time.sleep(1.5)
        out["result"] = json.loads(js(
            "JSON.stringify({val: document.querySelector('#page-settings input').value,"
            " ev: window.__ev})"))
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 真实按键探针结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:300]}")
