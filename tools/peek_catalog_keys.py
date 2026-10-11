"""列出课表模板的全部 key/pace_zone，用于挑选「典型课」示例键。只读。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.domain import workout_catalog as wc  # noqa: E402

by_key = {}


def add(t):
    by_key.setdefault(t.key, t)


for name in dir(wc):
    obj = getattr(wc, name)
    if isinstance(obj, wc.Template):
        add(obj)
for dist, groups in wc.Q_TABLES.items():
    for g, phases in groups.items():
        for ph, lst in phases.items():
            for t in lst:
                add(t)
for ph in ("base", "early", "transition", "final", "taper"):
    for dist in (5000, 21097, 42195):
        add(wc.lr_template(ph, 0, dist))

rows = sorted(by_key.values(), key=lambda t: (t.pace_zone or "zzz", t.key))
for t in rows:
    print("%-8s zone=%-9s kind=%-9s %s" % (t.key, t.pace_zone, t.kind, t.name))
print("共", len(rows), "个模板")
