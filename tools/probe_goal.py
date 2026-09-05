"""诊断：仅导航到 #/goal，检查距离卡片是否重复渲染（聚焦 dist-card 计数）。"""
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


def loaded():
    time.sleep(4)
    window.evaluate_js(
        "window.__errs = [];"
        "window.addEventListener('error', e => window.__errs.push('err: ' + (e.message || '')));"
        "window.addEventListener('unhandledrejection', e => window.__errs.push("
        "  'rej: ' + ((e.reason && e.reason.stack) ? e.reason.stack.split('\\n').slice(0, 3).join(' | ') : String(e.reason))));"
    )
    window.evaluate_js("location.hash = '#/goal'")
    time.sleep(2)
    expr = (
        "(function () {"
        " var sec = document.querySelector('#page-goal');"
        " var cards = Array.from(document.querySelectorAll('#page-goal .dist-card'));"
        " var tpls = document.querySelectorAll('#page-goal template');"
        " var xfor = document.querySelectorAll('#page-goal [x-for]');"
        " var btn = document.querySelectorAll('#page-goal .grid.cols-2 button');"
        " return JSON.stringify({"
        "   secCount: document.querySelectorAll('#page-goal').length,"
        "   cardCount: cards.length,"
        "   btnCount: btn.length,"
        "   cardTexts: cards.map(function (c) { return (c.textContent || '').replace(/\\s+/g, ' '); }),"
        "   templateCount: tpls.length,"
        "   xforCount: xfor.length,"
        "   htmlHead: sec ? sec.innerHTML.slice(0, 500) : 'NO-SECTION',"
        "   dbg: window.__dbg || null,"
        "   errs: window.__errs.slice()"
        " });"
        " })()"
    )
    try:
        result["goal"] = window.evaluate_js(expr)
    except Exception as e:  # noqa: BLE001
        result["goal"] = json.dumps({"eval异常": repr(e)})
    # 再等 1.5s 复查一次（排除异步二次求值）
    time.sleep(1.5)
    expr2 = (
        "(function () {"
        " var cards = document.querySelectorAll('#page-goal .dist-card');"
        " return JSON.stringify({cardCount2: cards.length});"
        " })()"
    )
    try:
        result["recheck"] = window.evaluate_js(expr2)
    except Exception as e:  # noqa: BLE001
        result["recheck"] = json.dumps({"eval异常": repr(e)})
    window.destroy()


window.events.loaded += loaded
webview.start()
print("=== goal 诊断 ===")
print("首次快照:", result.get("goal", "无"))
print("复查:", result.get("recheck", "无"))
