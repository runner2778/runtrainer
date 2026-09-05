"""诊断：直接读 settingsPage 组件状态，区分 init 未跑 vs DOM 未响应。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

url = _start_web_server()
window = webview.create_window("探针", url, js_api=Api(), width=1200, height=800)
out = {}


def js(expr):
    return window.evaluate_js(expr)


def loaded():
    try:
        time.sleep(4)
        out["state"] = js("(() => { const el = document.getElementById('page-settings');"
                          " const d = window.Alpine && Alpine.$data ? Alpine.$data(el) : null;"
                          " return JSON.stringify(d ? {hasPw: d.hasGarminPassword,"
                          " nick: d.profile && d.profile.nickname,"
                          " syncN: d.syncStates && d.syncStates.length,"
                          " theme: d.theme} : null); })()")
        out["rows"] = js("JSON.stringify([...document.querySelectorAll('#page-settings tbody tr')]"
                         ".map(r => r.textContent.replace(/\\s+/g,' ')))")
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:700]}")
