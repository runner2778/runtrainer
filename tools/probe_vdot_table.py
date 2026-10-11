"""丹尼尔斯 VDOT 训练表：只读打印某一档的整页数据，用于人工核对口径。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.domain import training_table as tt  # noqa: E402


def fmt(s: int) -> str:
    return "%d:%02d" % (s // 60, s % 60)


vdot = float(sys.argv[1]) if len(sys.argv) > 1 else 54.0
rest, mx = 52.0, 190.0
d = tt.daniels_table(vdot, rest, mx)

print("VDOT", d["vdot"], "  静息", rest, "最大", mx)
print("等效成绩:", {k: fmt(v) for k, v in d["equivalent"].items()})
print()
for z in d["zones"]:
    hr = z["hr"] or {}
    band = ("%.0f–%.0f%%HRR" % (hr["lo"] * 100, hr["hi"] * 100)) if hr else "-"
    bpm = ("  %d–%d bpm" % (hr["bpm_lo"], hr["bpm_hi"])) if "bpm_lo" in hr else ""
    anc = " ".join("%s %s" % (a["mark"], fmt(a["pace_s_km"])) for a in z["anchors"])
    print("%-9s %-8s %-9s %-11s %s–%s  %s" % (
        z["label"], z["band"], band + bpm, z["mark"] or "-",
        fmt(z["pace_slow_s_km"]), fmt(z["pace_fast_s_km"]), anc))
    for s in z["sessions"]:
        print("      · %-22s %s" % (s["name"], s["detail"][:46]))
print()
m = d["matrix"]
print("矩阵 %d 行，VDOT %d → %d" % (len(m), m[0]["vdot"], m[-1]["vdot"]))
for row in m:
    if row["vdot"] in (30, 40, 54, 85):
        print("  %2d  E %s–%s  M %s  T1 %s  T %s  I %s  R %s" % (
            row["vdot"], fmt(row["E"][0]), fmt(row["E"][1]), fmt(row["M"]),
            fmt(row["T1"]), fmt(row["T"]), fmt(row["I"]), fmt(row["R"])))
