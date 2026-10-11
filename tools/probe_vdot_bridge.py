"""bridge.get_vdot_table 现网契约查看器：默认档解析 + 指定档覆盖。只读。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.db import database  # noqa: E402

database.migrate()
api = Api()

for arg in (None, 52.0, 68.0):
    r = api.get_vdot_table(arg)
    if not r.get("ok"):
        print(arg, "ERR", r.get("error"))
        continue
    d = r["data"]
    print("arg=%-6s ok vdot=%-5s src=%-8s label=%s" % (
        arg, d["vdot"], d["vdot_source"], d["vdot_label"]))
    if arg is None:
        print("   estimate_vdot=%s plan_vdot=%s rest_hr=%s max_hr=%s" % (
            d["estimate_vdot"], d["plan_vdot"], d["rest_hr"], d["max_hr"]))
        print("   zones=%d  matrix=%d  equivalent=%s" % (
            len(d["zones"]), len(d["matrix"]),
            {k: "%d:%02d" % (v // 60, v % 60) for k, v in d["equivalent"].items()}))
        for z in d["zones"]:
            hr = z["hr"] or {}
            print("   %-6s %s %s %s→%s anchors=%s sessions=%d" % (
                z["label"], z["band"], z["mark"] or "-",
                z["pace_slow_s_km"], z["pace_fast_s_km"],
                [a["mark"] for a in z["anchors"]], len(z["sessions"])))
        print("   keys:", sorted(d.keys()))
        m0 = d["matrix"][0]
        print("   矩阵列:", list(m0.keys()), " rows=", len(d["matrix"]))
        print("   等效列(=DISTANCES):", list(m0["races"]))
        cur = [r for r in d["matrix"] if r["vdot"] == round(d["vdot"])][0]
        print("   当前档 %s:" % cur["vdot"],
              {k: "%d:%02d" % (v // 60, v % 60) for k, v in cur["races"].items()})
    else:
        print("   json_head:", json.dumps(d["zones"][1], ensure_ascii=False)[:160])
