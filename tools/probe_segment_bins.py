"""按「次内分段」口径量化各配速档的样本量，比较档宽方案。只读。

与 probe_fill_schemes.py 的区别：那份用整次平均配速（avg_pace_s_km），
本份用 structure_json 的**段级** pace_s_km——间歇课会被拆成 work/rest，
快档（3:20–4:00）因此才有样本。同时比较含/不含 rest 段的影响。

方案：统一 15s / 20s / 30s 三档宽，各报「滚动 4 周窗」下的出点周数。
"""
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.utils import dates as dutil  # noqa: E402

WEEKS = 16
ROLL = 4
MIN_N = 2
end = date.today()
end_mon = end - timedelta(days=end.weekday())
mons = [end_mon - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]
start = mons[0] - timedelta(weeks=ROLL - 1)   # 滚动窗要多取

acts = activity_repo.list_activities(start.isoformat(), end.isoformat(), limit=5000)
print(f"窗口 {mons[0]} ~ {end}（{WEEKS} 自然周，另多取 {ROLL - 1} 周喂滚动窗）")
print(f"活动 {len(acts)} 个")

rows = []            # (wi, pace, hr, dist, type)
n_no_struct = n_no_seg = 0
for a in acts:
    raw = a.get("structure_json")
    segs = json.loads(raw) if raw else None
    if not segs:
        n_no_struct += 1
        continue
    day = dutil.ts_to_date(a["start_ts"])
    wi = (day - timedelta(days=day.weekday()) - mons[0]).days // 7
    for s in segs:
        p, hr, d = s.get("pace_s_km"), s.get("avg_hr"), s.get("distance_m")
        if not p or not hr or not d:
            n_no_seg += 1
            continue
        rows.append((wi, float(p), float(hr), float(d), s.get("type") or "?"))

print(f"无 structure 活动 {n_no_struct}，段缺 pace/hr/dist 的 {n_no_seg}")
print(f"段总数 {len(rows)}（含 rest/recovery）")
print()


def coverage(sub, bin_s, min_n, kinds=None):
    """滚动 ROLL 周窗下，出点周数。"""
    use = [r for r in sub if kinds is None or r[4] in kinds]
    if not use:
        return 0, 0
    got = 0
    for w in range(WEEKS):
        d = hw = n = 0.0
        for (wi, p, hr, dist, _t) in use:
            if w - ROLL + 1 <= wi <= w:
                d += dist
                hw += hr * dist
                n += 1
        if n >= min_n and d:
            got += 1
    return got, len(use)


for bin_s in (15, 20, 30):
    print(f"=== 档宽 {bin_s}s（含 rest）===")
    cells = defaultdict(list)
    for r in rows:
        b = int(r[1] // bin_s) * bin_s
        if b < 180 or b > 900:
            continue
        cells[b].append(r)
    for b in sorted(cells):
        sub = cells[b]
        if len(sub) < 3:
            continue
        m, s = divmod(b, 60)
        cov, n = coverage(sub, bin_s, MIN_N)
        dk = sorted({r[4] for r in sub})
        print(f"  {m}:{s:02d}  段{n:>3}  出点 {cov:>2}/{WEEKS}  类型 {','.join(dk)}")
    print()

print("=== 档宽 15s：排除 rest/recovery（只看 work/continuous）===")
cells = defaultdict(list)
for r in rows:
    b = int(r[1] // 15) * 15
    if b < 180 or b > 900:
        continue
    cells[b].append(r)
for b in sorted(cells):
    sub = cells[b]
    pure = [r for r in sub if r[4] in ("work", "continuous", "?")]
    if len(pure) < 3:
        continue
    m, s = divmod(b, 60)
    cov_all, n_all = coverage(sub, 15, MIN_N)
    cov_pure, n_pure = coverage(sub, 15, MIN_N, kinds=("work", "continuous", "?"))
    print(f"  {m}:{s:02d}  全部段{n_all:>3}(出点{cov_all:>2})  "
          f"纯跑段{n_pure:>3}(出点{cov_pure:>2})")
