"""真实窗口端到端 UI 冒烟：JS 桥 → 演示数据 → 建目标 → 生成课表 → AI 建议 → 批准。"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

url = _start_web_server()
print("加载:", url, flush=True)

window = webview.create_window("端到端", url, js_api=Api(), width=1000, height=700)
out = {}


def js(expr):
    return window.evaluate_js(expr)


def wait_flag(key, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        raw = js(f"window.__flow && window.__flow[{json.dumps(key)}]"
                 f" ? JSON.stringify(window.__flow[{json.dumps(key)}]) : ''")
        if raw:
            return json.loads(raw)
        time.sleep(0.3)
    return {"ok": 0, "err": f"timeout waiting {key}"}


def call(key, expr):
    js(f"window.__flow = window.__flow || {{}};"
       f"window.__f = window.__f || ((k, p) => p.then(v => {{ window.__flow[k] = {{ok: 1, data: v}}; }})"
       f".catch(e => {{ window.__flow[k] = {{ok: 0, err: String(e)}}; }}));"
       f"window.__f({json.dumps(key)}, {expr});")


def loaded():
    try:
        race = (date.today() + timedelta(days=56)).isoformat()
        call("wiz", "window.pywebview.api.get_goal_wizard_context()")
        out["wiz"] = wait_flag("wiz")
        call("seed", "window.pywebview.api.seed_demo(8, true)")
        out["seed"] = wait_flag("seed", 40)
        params = {
            "goal": {"distance_m": 5000, "race_date": race, "target_seconds": None,
                     "vdot": None, "name": "5K"},
            "plan": {"base_weekly_km": 30, "run_days": 5, "long_run_weekday": 6},
        }
        call("prev", f"window.pywebview.api.preview_plan({json.dumps(params)})")
        out["prev"] = wait_flag("prev")
        call("create", f"window.pywebview.api.create_goal_and_plan({json.dumps(params)})")
        out["create"] = wait_flag("create", 40)
        call("coach", "window.pywebview.api.request_coach_advice()")
        out["coach"] = wait_flag("coach", 90)
        call("decide", "window.pywebview.api.decide_coach_advice(true)")
        out["decide"] = wait_flag("decide")
        js("location.hash = '#/calendar'")
        time.sleep(3)
        out["calendar"] = js(
            "JSON.stringify({wkButtons: document.querySelectorAll('#page-calendar .wk').length,"
            " kinds: [...new Set([...document.querySelectorAll('#page-calendar .wk')]"
            "  .map(b => (b.className.match(/kind-\\w+/) || [''])[0]))].join(','),"
            " aiMarks: document.querySelectorAll('#page-calendar .wk.ai').length,"
            " txt: (document.querySelector('#page-calendar').textContent || '')"
            "  .replace(/\\s+/g, ' ').slice(0, 120)})")
        js("location.hash = '#/coach'")
        time.sleep(3)
        out["coachPage"] = js(
            "JSON.stringify({txt: (document.querySelector('#page-coach').textContent || '')"
            "  .replace(/\\s+/g, ' ').slice(0, 200)})")
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 端到端结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:400]}")
