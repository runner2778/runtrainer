r"""实测「同步后仪表盘自动更新」链路（真实窗口 + 真实库，测完清理）。

用法：.venv\Scripts\python.exe tools\probe_dashboard_sync.py
流程：进仪表盘读本周实际跑量 → 模拟 Garmin 同步直插两条今日活动 →
dispatch sync-done（<2s 内连续两次，覆盖 shown() 防双载场景）→
读 DOM 确认数值与表格更新 → 删除插入的活动恢复原状。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402
from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

url = _start_web_server()
print("加载:", url, flush=True)

window = webview.create_window("验证", url, js_api=Api(), width=1200, height=800)
out = {"ok": False}

READ_JS = ("JSON.stringify({"
           " doneHero: (() => { const hs = document.querySelectorAll('#page-dashboard .hero .big');"
           "  return hs.length > 1 ? hs[1].textContent.trim() : null; })(),"
           " wkRow: (() => { const t = document.querySelectorAll('#page-dashboard table')[0];"
           "  if (!t) return null; const rows = [...t.querySelectorAll('tbody tr')]"
           "   .filter(r => r.children.length > 2);"
           "  return rows.length ? rows[rows.length - 1].children[2].textContent.trim() : null; })()})")

EXTERNAL_IDS = [f"probe-dash-sync-{int(time.time())}-a", f"probe-dash-sync-{int(time.time())}-b"]


def js(expr):
    return window.evaluate_js(expr)


def settle(sec=2.0):
    time.sleep(sec)


def read_km():
    return json.loads(js(READ_JS))


def insert_fake(km: float, external_id: str) -> None:
    """模拟 Garmin 同步写入：一条今天的跑步活动。"""
    activity_repo.upsert_activity({
        "source": "garmin", "external_id": external_id, "file_path": None,
        "name": "同步模拟", "sport": "跑步",
        "start_ts": dates.date_to_ts(dates.today()) + 18 * 3600, "tz_offset_min": 0,
        "duration_s": int(km * 330), "distance_m": km * 1000,
        "avg_pace_s_km": 330.0, "avg_hr": 150.0, "laps_json": None,
        "has_samples": 0,
    })


def cleanup() -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM activities WHERE external_id IN (?, ?)",
                     tuple(EXTERNAL_IDS))


def loaded():
    try:
        settle(3)
        js("location.hash = '#/dashboard'")
        settle(3)
        out["before"] = read_km()
        # 模拟同步：插 5km → dispatch sync-done（此时距上次 load 可能 <2s）
        insert_fake(5.0, EXTERNAL_IDS[0])
        js("window.dispatchEvent(new Event('sync-done'))")
        settle(0.2)
        # 2s 防双载场景：立刻再插 3km 并 dispatch（上次刷新距今 <2s）
        insert_fake(3.0, EXTERNAL_IDS[1])
        js("window.dispatchEvent(new Event('sync-done'))")
        settle(3)
        out["after"] = read_km()
        b, a = out["before"], out["after"]
        delta = (float(a["wkRow"]) - float(b["wkRow"])) if (b.get("wkRow") and a.get("wkRow")) else None
        out["delta_km"] = delta
        # 两次同步共 +8km：DOM 必须两次都刷新（含 <2s 的第二次）
        out["ok"] = (delta is not None and delta >= 8.0
                     and a["doneHero"] is not None and a["wkRow"] is not None)
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        cleanup()
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== 同步刷新验证结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)}")
print("已清理模拟活动：", EXTERNAL_IDS)
