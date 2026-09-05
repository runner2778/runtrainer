"""诊断：区分幽灵克隆与可见按钮——点击可见按钮看弹窗是否打开。"""
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
        time.sleep(3)
        js("location.hash = '#/calendar'")
        time.sleep(3)
        out["counts"] = js("(() => { const all = [...document.querySelectorAll('#page-calendar .wk')];"
                           " const vis = all.filter(b => b.getBoundingClientRect().width > 0);"
                           " return JSON.stringify({all: all.length, visible: vis.length,"
                           "  dayCells: document.querySelectorAll('#page-calendar .day').length,"
                           "  visDays: [...document.querySelectorAll('#page-calendar .day')]"
                           "    .filter(d => d.getBoundingClientRect().width > 0).length}); })()")
        # 点击第一个「可见的按钮」（真实用户会点到的那个）
        out["vis0"] = js("(() => { const vis = [...document.querySelectorAll('#page-calendar button.wk')]"
                         "  .filter(b => b.getBoundingClientRect().width > 0);"
                         " if (!vis[0]) return 'none';"
                         " const b = vis[0]; return JSON.stringify({wid: b.dataset.wid,"
                         "  html: b.outerHTML.slice(0, 300)}); })()")
        js("(() => { const vis = [...document.querySelectorAll('#page-calendar button.wk')]"
           "  .filter(b => b.getBoundingClientRect().width > 0);"
           " vis[0] && vis[0].click(); })()")
        time.sleep(1.5)
        out["afterVisClick"] = js("(() => { const d = Alpine.$data(document.getElementById('page-calendar'));"
                                  " return JSON.stringify({modal: d.modal ? 'SET' : 'null',"
                                  " masks: document.querySelectorAll('.modal-mask').length,"
                                  " diag: window.__wkDiag || null}); })()")
        # 点弹窗内的「关闭」按钮，验证 modal 子树里的 Alpine @click 是否有效
        js("(() => { const b = [...document.querySelectorAll('.modal .btn')]"
           "  .find(x => x.textContent.indexOf('关闭') >= 0); b && b.click(); })()")
        time.sleep(1.0)
        out["afterModalCloseBtn"] = js("(() => { const d = Alpine.$data(document.getElementById('page-calendar'));"
                                       " return JSON.stringify({modal: d.modal ? 'SET' : 'null',"
                                       " masks: document.querySelectorAll('.modal-mask').length}); })()")
        js("(() => { const d = Alpine.$data(document.getElementById('page-calendar')); d.modal = null; })()")
        time.sleep(0.5)
        out["afterClose"] = js("String(document.querySelectorAll('.modal-mask').length)")
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:500]}")
