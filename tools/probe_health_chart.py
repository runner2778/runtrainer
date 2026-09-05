"""诊断：shown() 运行时健康页是否可见。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

url = _start_web_server()
window = webview.create_window("probe", url, js_api=Api(), width=1200, height=800)


def run(w):
    time.sleep(3)
    window.evaluate_js(
        "(() => {"
        " const d = Alpine.$data(document.getElementById('page-health'));"
        " const origShown = d.shown.bind(d);"
        " const origRender = d.renderCharts.bind(d);"
        " window.__log = [];"
        " d.shown = function() {"
        "   const el = document.getElementById('page-health');"
        "   window.__log.push(['shown', document.querySelector('#health-hrv-chart').offsetWidth,"
        "     d._renderDeferred, el.getAttribute('style') || '',"
        "     document.documentElement.dataset.theme]);"
        "   return origShown();"
        " };"
        " d.renderCharts = function() {"
        "   window.__log.push(['render', document.querySelector('#health-hrv-chart').offsetWidth]);"
        "   return origRender();"
        " };"
        "})()")
    time.sleep(1)
    window.evaluate_js("location.hash = '#/health'")
    time.sleep(3)
    out = window.evaluate_js(
        "JSON.stringify({log: window.__log,"
        "  secVis: (() => { const el = document.getElementById('page-health');"
        "    const s = getComputedStyle(el);"
        "    return {disp: s.display, vis: s.visibility, w: el.offsetWidth}; })(),"
        "  deferred: Alpine.$data(document.getElementById('page-health'))._renderDeferred,"
        "  inst: !!echarts.getInstanceByDom(document.getElementById('health-pacehr-chart'))"
        "})")
    print(out, flush=True)
    window.destroy()


webview.start(run, window)
