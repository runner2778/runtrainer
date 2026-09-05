"""快速探针：goal 向导第 2 步职业双练模式 checkbox 渲染。"""
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
window = webview.create_window("probe", url, js_api=Api(), width=1200, height=800)
out = {}


def loaded():
    time.sleep(3)
    window.evaluate_js("location.hash = '#/goal'")
    time.sleep(2.5)
    window.evaluate_js(
        "[...document.querySelectorAll('#page-goal button')].find(b=>b.textContent.includes('下一步')).click()")
    time.sleep(2)
    out["pro_checkbox"] = json.loads(window.evaluate_js(
        "JSON.stringify({"
        "  label: (document.querySelector('#page-goal').textContent||'').includes('职业双练模式'),"
        "  cb: !!document.querySelector('#page-goal input[type=checkbox]'),"
        "  dblHidden: !!document.querySelector('#page-goal .form-row[x-show*=\\'!form.pro_mode\\']')"
        "})"))
    window.destroy()


window.events.loaded += loaded
webview.start()
print(json.dumps(out, ensure_ascii=False))
