"""诊断：逐页切换 hash 路由，记录每页渲染状态与报错，定位空数据下前端 bug。"""
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

window = webview.create_window("诊断", url, js_api=Api(), width=800, height=600)
result = {}

PAGES = ["dashboard", "calendar", "goal", "coach", "activities", "health", "settings"]


def loaded():
    time.sleep(4)
    window.evaluate_js(
        "window.__errs = [];"
        "window.addEventListener('error', e => window.__errs.push('err: ' + (e.message || '')));"
        "window.addEventListener('unhandledrejection', e => window.__errs.push("
        "  'rej: ' + ((e.reason && e.reason.stack) ? e.reason.stack.split('\\n').slice(0, 3).join(' | ') : String(e.reason))));"
        "window.__snaps = [];"
    )
    for pg in PAGES:
        window.evaluate_js(f"location.hash = '#/{pg}'")
        time.sleep(2)
        expr = (
            f"(function () {{"
            f" var sec = document.querySelector('#page-{pg}');"
            f" var vis = [];"
            f" ['dashboard','calendar','goal','coach','activities','health','settings'].forEach(function (p) {{"
            f"   var s = document.querySelector('#page-' + p);"
            f"   vis.push(p + ':' + (s ? (s.style.display || 'show') : 'missing'));"
            f" }});"
            f" return JSON.stringify({{page: '{pg}', kids: sec ? sec.children.length : -1,"
            f"   txt: sec ? (sec.textContent || '').replace(/\\s+/g, ' ').slice(0, 100) : 'NO-SECTION',"
            f"   vis: vis.join(','), errs: window.__errs.slice()}});"
            f" }})()"
        )
        try:
            snap = window.evaluate_js(expr)
        except Exception as e:  # noqa: BLE001
            snap = json.dumps({"page": pg, "eval异常": repr(e)})
        result[pg] = snap
        window.evaluate_js("window.__errs.length = 0")
    window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 诊断结果 ===")
for pg in PAGES:
    print(f"--- {pg} ---")
    print(result.get(pg, "无"))
