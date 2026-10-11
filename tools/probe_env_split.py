"""环境折算的收益拆分：坡度项 / 跑步机项 各自贡献多少。

担心点：两个常数都是从真实库 OLS 标定出来的，但回测只有 17 场真实比赛——
万一「MAE 变小」其实是被某一两场带出来的，那就是过拟合而不是真修正。
做法：用子进程 + 前置 patch，把 evidence 的两个常数分别置零再跑同一个回测。
"""
from __future__ import annotations

import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.db import database  # noqa: E402

database.migrate()
with database.get_conn() as conn:
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, distance_m, avg_pace_s_km, elevation_gain_m FROM activities"
        " WHERE distance_m >= 3000").fetchall()]

tm = [r for r in rows if evm.is_treadmill(r)]
hilly = [r for r in rows
         if (r.get("elevation_gain_m") or 0) / max(r["distance_m"] / 1000, 1) >= 10]
flat_ok = [r for r in rows
           if (r.get("elevation_gain_m") or 0) / max(r["distance_m"] / 1000, 1) < 2
           and not evm.is_treadmill(r)]
print(f"真实库 ≥3km 活动 {len(rows)} 条 | 跑步机 {len(tm)} 条 | 坡度≥10m/km {len(hilly)} 条"
      f" | 近平地非跑步机 {len(flat_ok)} 条")
if tm:
    print("  跑步机样例:", [r["name"] for r in tm[:8]])
ap = [r["avg_pace_s_km"] for r in rows if r["avg_pace_s_km"]]
tp = [r["avg_pace_s_km"] for r in tm if r["avg_pace_s_km"]]
fp = [r["avg_pace_s_km"] for r in flat_ok if r["avg_pace_s_km"]]
hp = [r["avg_pace_s_km"] for r in hilly if r["avg_pace_s_km"]]
print("  平均配速: 全体 %.1f | 跑步机 %s | 近平地 %s | 高坡度 %s"
      % (statistics.mean(ap),
         "%.1f(n=%d)" % (statistics.mean(tp), len(tp)) if tp else "—",
         "%.1f(n=%d)" % (statistics.mean(fp), len(fp)) if fp else "—",
         "%.1f(n=%d)" % (statistics.mean(hp), len(hp)) if hp else "—"))

BACKTEST = ROOT / "tools" / "backtest_predict_v2.py"
WRAPPER = """
import sys
sys.path.insert(0, r"{src}")
from runtrainer.domain import evidence as evm
evm.GRADE_PACE_S_KM_PER_M = float({grade})
evm.TREADMILL_PACE_DELTA_S_KM = float({treadmill})
with open(r"{bt}", encoding="utf-8") as f:
    code = f.read()
exec(compile(code, r"{bt}", "exec"), {{"__name__": "__main__", "__file__": r"{bt}"}})
"""


def run(label: str, grade: float, treadmill: float) -> None:
    print(f"\n== {label} ==")
    src = WRAPPER.format(src=SRC, bt=BACKTEST, grade=grade, treadmill=treadmill)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     dir=str(ROOT / "tools"), encoding="utf-8") as f:
        f.write(src)
        path = f.name
    out = subprocess.run([sys.executable, path], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=str(ROOT))
    txt = out.stdout + (out.stderr or "")
    keep = ("真实比赛 · 证据管线", "5K · 新(真)", "10K · 新(真)", "半马 · 新(真)",
            "After·证据管线")
    for line in txt.splitlines():
        s = line.strip()
        if any(k in s for k in keep):
            print("   " + s)
    if out.returncode != 0:
        print("   !! 回测子进程异常退出")
        print("\n".join(txt.splitlines()[-15:]))
    Path(path).unlink(missing_ok=True)


G = evm.GRADE_PACE_S_KM_PER_M
T = evm.TREADMILL_PACE_DELTA_S_KM
run("A 现状（坡度 + 跑步机）", G, T)
run("B 只关坡度项", 0.0, T)
run("C 只关跑步机项", G, 0.0)
run("D 两项都关（≈批33 原状）", 0.0, 0.0)
