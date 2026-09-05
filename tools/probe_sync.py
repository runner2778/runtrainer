"""诊断：真实窗口验证同步 UX——无凭据同步的报错路径 + 凭据保存/清除机制。"""
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

window = webview.create_window("诊断", url, js_api=Api(), width=1000, height=700)
result = {}


def wait_flag(key, timeout=30):
    """轮询 window[key] 直到有值，返回其字符串。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = window.evaluate_js(f"window.{key} || null")
        if v is not None:
            return v
        time.sleep(0.3)
    return None


def loaded():
    time.sleep(4)
    window.evaluate_js("location.hash = '#/settings'")
    time.sleep(2)

    # 1) 无凭据直接点同步 → bridge 应立即返回错误信封
    window.evaluate_js(
        "window.__r = null;"
        "window.pywebview.api.sync_garmin().then(x => window.__r = JSON.stringify(x),"
        " e => window.__r = 'REJECT:' + e);"
    )
    result["sync_no_creds"] = wait_flag("__r")

    # 2) 保存凭据 → 读取设置 → 清除凭据（还原现场）
    window.evaluate_js(
        "window.__r2 = null;"
        "window.pywebview.api.save_garmin_credentials('probe_test_user', 'probe_test_pw')"
        ".then(x => window.__r2 = JSON.stringify(x), e => window.__r2 = 'REJECT:' + e);"
    )
    result["save"] = wait_flag("__r2")
    window.evaluate_js(
        "window.__r3 = null;"
        "window.pywebview.api.get_settings().then(x => window.__r3 = JSON.stringify(x),"
        " e => window.__r3 = 'REJECT:' + e);"
    )
    settings = wait_flag("__r3")
    try:
        st = json.loads(settings)
        result["settings_after_save"] = {
            "garmin_username": st.get("data", {}).get("garmin_username"),
            "has_garmin_password": st.get("data", {}).get("has_garmin_password"),
            "sync_states": st.get("data", {}).get("sync_states"),
        }
    except Exception as e:  # noqa: BLE001
        result["settings_after_save"] = repr(e)
    window.evaluate_js("window.pywebview.api.clear_garmin_credentials();")
    time.sleep(1)

    # 3) 设置页渲染快照：确认保存表单/同步状态表格显示
    expr = (
        "(function () {"
        " var sec = document.querySelector('#page-settings');"
        " return JSON.stringify({"
        "   txt: (sec.textContent || '').replace(/\\s+/g, ' ').slice(0, 300),"
        "   saveBtn: !!Array.from(sec.querySelectorAll('button')).find(b => b.textContent.includes('保存账号')),"
        "   syncBtn: !!Array.from(sec.querySelectorAll('button')).find(b => b.textContent.includes('立即同步'))"
        " });"
        " })()"
    )
    try:
        result["settings_view"] = window.evaluate_js(expr)
    except Exception as e:  # noqa: BLE001
        result["settings_view"] = json.dumps({"eval异常": repr(e)})
    window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 同步 UX 诊断 ===")
for k, v in result.items():
    print(f"{k}: {v}")
