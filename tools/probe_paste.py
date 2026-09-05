"""粘贴链路探针：真实窗口内定位输入框 → 派发 Ctrl+V keydown → 检查回填。

用法：.venv\\Scripts\\python tools\\probe_paste.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

url = _start_web_server()
print("加载:", url, flush=True)

window = webview.create_window("粘贴探针", url, js_api=Api(), width=1000, height=700)
out = {}


def js(expr):
    return window.evaluate_js(expr)


def settle(sec=1.5):
    time.sleep(sec)


def loaded():
    try:
        settle(2.5)
        js("location.hash = '#/settings'")
        settle(2.5)
        # 1) 输入框清单
        out["inputs"] = json.loads(js(
            "JSON.stringify([...document.querySelectorAll('#page-settings input')]"
            "  .map(i => ({id: i.id, type: i.type, ph: i.placeholder||''})))"))
        # 2) 对第一个输入框派发真实 KeyboardEvent
        js("(() => { const i = document.querySelector('#page-settings input');"
           " if (!i) return 'noinput'; i.focus(); i.value = '';"
           " const ev = new KeyboardEvent('keydown', {key: 'v', ctrlKey: true, bubbles: true, cancelable: true});"
           " const r = i.dispatchEvent(ev);"
           " return JSON.stringify({dispatched: r, val: i.value}); })()")
        settle(1.2)
        out["keydown_path"] = json.loads(js(
            "JSON.stringify({val: document.querySelector('#page-settings input').value})"))
        # 3) 对照：直接调 bridge 看数据
        out["bridge"] = json.loads(js(
            "JSON.stringify(pywebview.api.read_clipboard ? 'has_api' : 'no_api')"))
        # 4) 派发原生 paste 事件（WebView2 若派发，clipboardData 为空）
        js("(() => { const i = document.querySelector('#page-settings input');"
           " i.focus(); i.value = '';"
           " const ev = new ClipboardEvent('paste', {bubbles: true, cancelable: true});"
           " const r = i.dispatchEvent(ev);"
           " return JSON.stringify({dispatched: r, val: i.value}); })()")
        settle(1.2)
        out["paste_path"] = json.loads(js(
            "JSON.stringify({val: document.querySelector('#page-settings input').value})"))
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 粘贴探针结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:300]}")
