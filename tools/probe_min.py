"""诊断：最小页面复现 template x-for 重复渲染（区分环境问题 vs 应用代码问题）。"""
import json
import sys
import tempfile
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

WEB = Path(__file__).resolve().parents[1] / "web"

PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body x-data="{}">
<div id="named" x-data="comp">
  <template x-for="n in items" :key="n"><button class="k-named" x-text="n"></button></template>
</div>
<div id="inline" x-data="{ items: [1,2,3] }">
  <template x-for="n in items" :key="n"><button class="k-inline" x-text="n"></button></template>
</div>
<div id="plain" x-data="{ items: [1,2,3] }">
  <div x-for="n in items" :key="n"><button class="k-plain" x-text="n"></button></div>
</div>
<script src="/vendor/alpine.min.js"></script>
<script>
window.__dbg = { starts: 0, factories: 0, errors: [] };
window.addEventListener('error', e => window.__dbg.errors.push('err: ' + (e.message || '')));
window.addEventListener('unhandledrejection', e => window.__dbg.errors.push(
  'rej: ' + ((e.reason && e.reason.stack) ? e.reason.stack.split('\\n').slice(0, 3).join(' | ') : String(e.reason))));
window.Alpine.data('comp', () => { window.__dbg.factories++; return { items: [1, 2, 3] }; });
const _s = window.Alpine.start.bind(window.Alpine);
window.Alpine.start = function () { window.__dbg.starts++; return _s(); };
window.Alpine.start();
</script>
</body></html>"""

tmpdir = tempfile.mkdtemp(prefix="probe_min_")
(tmpdir_path := Path(tmpdir)).joinpath("index.html").write_text(PAGE, encoding="utf-8")
(tmpdir_path / "vendor").mkdir()
(tmpdir_path / "vendor" / "alpine.min.js").write_bytes((WEB / "vendor" / "alpine.min.js").read_bytes())


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


handler = partial(_Quiet, directory=tmpdir)
server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
import threading
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
print("加载:", url, flush=True)

window = webview.create_window("诊断", url, width=800, height=600)
result = {}


def loaded():
    time.sleep(3)
    expr = (
        "(function () {"
        " var c = function (sel) { return document.querySelectorAll(sel).length; };"
        " return JSON.stringify({"
        "   named: c('#named .k-named'), inline: c('#inline .k-inline'), plain: c('#plain .k-plain'),"
        "   dbg: window.__dbg"
        " });"
        " })()"
    )
    try:
        result["min"] = window.evaluate_js(expr)
    except Exception as e:  # noqa: BLE001
        result["min"] = json.dumps({"eval异常": repr(e)})
    window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 最小页面诊断 ===")
print(result.get("min", "无"))
