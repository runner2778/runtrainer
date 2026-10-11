"""只读：捕捉回测每一步的 ev_fitness（含 best_effort 分档）与当时的转换差。

用于验证假设：best_effort 的转换差是「整池」学的（short 档主导），却被
原样加到 mid/long 档上——档位越低，欠修正越多，长距离预测被拖慢。
"""
from __future__ import annotations

import io
import sys
import json
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtrainer.domain import evidence as evm  # noqa: E402

CAPS: list[dict] = []
_orig_cf = evm.current_fitness


def spy(evidence, deltas, as_of=None, half_life=None):
    hl = evm.EVIDENCE_HALF_LIFE_DAYS if half_life is None else half_life
    out = _orig_cf(evidence, deltas, as_of, hl)
    CAPS.append({
        "as_of": str(as_of),
        "deltas": {k: v for k, v in (deltas or {}).items()},
        "fitness": out,
        "be_recs": [{"day": r["day"], "vdot": r["vdot"], "dist": r.get("dist"),
                     "hr_ratio": r.get("hr_ratio"), "eff": r.get("eff")}
                    for r in evidence if r.get("kind") == "best_effort"],
    })
    return out


evm.current_fitness = spy

src = (ROOT / "tools" / "backtest_predict_v2.py").read_text(encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(compile(src, str(ROOT / "tools" / "backtest_predict_v2.py"), "exec"),
         {"__name__": "__main__",
          "__file__": str(ROOT / "tools" / "backtest_predict_v2.py")})

TARGETS = {"2026-10-07", "2026-10-04", "2026-06-21", "2026-07-19", "2026-04-04"}
seen = set()
print("== best_effort 分档：eff / 加差后 vdot / 条数 ==")
for c in CAPS:
    d = c["as_of"][:10]
    if d not in TARGETS or d in seen:
        continue
    seen.add(d)
    f = c["fitness"]
    be = f.get("best_effort") or {}
    bk = f.get("best_effort_buckets") or {}
    delta = (c["deltas"].get("best_effort") or {}).get("delta")
    print(f"\n  as_of {d}  best_effort delta={delta}")
    print(f"    整池: eff={be.get('eff')} raw={be.get('raw')} vdot={be.get('vdot')} n={be.get('n')}")
    for name in ("short", "mid", "long"):
        b = bk.get(name)
        if b:
            print(f"    {name:>5} 档: eff={b['eff']:<6} raw={b['raw']:<6} "
                  f"vdot={b['vdot']:<6} n={b['n']:<3} last={b['last_day']}")
    for kind in ("interval", "steady"):
        g = f.get(kind) or {}
        if g:
            dd = (c["deltas"].get(kind) or {}).get("delta")
            print(f"    {kind:>8}: eff={g.get('eff')} vdot={g.get('vdot')} n={g.get('n')} delta={dd}")

print("\n== 每档 eff 与「同场全开比赛 VDOT」的差（理想分档差） ==")
for c in CAPS:
    d = c["as_of"][:10]
    if d not in TARGETS:
        continue
    bk = (c["fitness"].get("best_effort_buckets") or {})
    if not bk:
        continue
    print(f"  as_of {d}: " + json.dumps(
        {k: {"eff": v["eff"], "vdot": v["vdot"], "n": v["n"]} for k, v in bk.items()},
        ensure_ascii=False))

print("\n== 15K（long 档）原始记录 ==")
for c in CAPS:
    d = c["as_of"][:10]
    if d not in TARGETS:
        continue
    rows = [r for r in c["be_recs"] if r.get("dist") == "15K"]
    print(f"  as_of {d}  n={len(rows)}")
    for r in sorted(rows, key=lambda x: x["day"]):
        eff = evm._eff_adjusted(r['vdot'], 'best_effort', r['hr_ratio'])
        print(f"    {r['day']} vdot={r['vdot']:<6} eff={eff:<6.1f} hr_ratio={r['hr_ratio']}")
    mids = [r for r in c["be_recs"] if r.get("dist") == "10K"]
    print(f"    （10K 档 n={len(mids)}: "
          + ", ".join(f"{r['day']}={evm._eff_adjusted(r['vdot'],'best_effort',r['hr_ratio']):.1f}" for r in sorted(mids, key=lambda x: x['day']))
          + "）")
