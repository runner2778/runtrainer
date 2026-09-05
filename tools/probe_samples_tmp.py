import sys
sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from test_workout_analysis import _samples
import runtrainer.domain.workout_analysis as wa

samples2 = _samples(0.0, [(120, 2.8, 125), (60, 4.2, 175), (60, None, None),
                          (60, 4.2, 176), (120, 2.8, 118)])
rows = sorted((s for s in samples2 if s.get("t_offset_s") is not None and s.get("speed_mps")),
              key=lambda s: s["t_offset_s"])
print("rows:", len(rows))
run_spds = [s["speed_mps"] for s in rows if s["speed_mps"] >= wa.SAMPLE_RUN_MIN_MPS]
print("run_spds:", len(run_spds), "baseline:", sorted(run_spds)[len(run_spds) // 2])
baseline = sorted(run_spds)[len(run_spds) // 2]
n = len(rows)
half = 5
smooth = []
for i in range(n):
    lo, hi = max(0, i - half), min(n, i + half + 1)
    seg = sorted(rows[k]["speed_mps"] for k in range(lo, hi))
    smooth.append(seg[len(seg) // 2])
fast = [v >= baseline * wa.SAMPLE_WORK_FACTOR for v in smooth]
runs = []
i = 0
while i < n:
    if fast[i]:
        j = i
        while j + 1 < n and fast[j + 1]:
            j += 1
        runs.append([i, j])
        i = j + 1
    else:
        i += 1
print("runs:", runs)
merged = []
for r in runs:
    if merged and rows[r[0]]["t_offset_s"] - rows[merged[-1][1]]["t_offset_s"] <= wa.SAMPLE_MERGE_GAP_S:
        merged[-1][1] = r[1]
    else:
        merged.append(list(r))
print("merged:", merged)
works = []
for lo, hi in merged:
    seg = wa._seg_from_rows(rows, lo, hi)
    print("window", lo, hi, seg)
    if (seg["duration_s"] or 0) >= wa.SAMPLE_WORK_MIN_S and (seg["distance_m"] or 0) >= wa.SAMPLE_WORK_MIN_M:
        works.append((lo, hi, seg))
print("works:", len(works))
if len(works) >= 2:
    for (a_lo, a_hi, _), (b_lo, b_hi, _) in zip(works, works[1:]):
        print("block gap:", rows[b_lo]["t_offset_s"] - rows[a_hi]["t_offset_s"])
