"""只读探针：课表已排课的配速列 vs pace_table(plan.vdot) 的漂移成因。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import plan_repo  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402

database.migrate()


def main() -> None:
    plan = plan_repo.get_active_plan()
    pv = plan["vdot"]
    paces = vd.pace_table(pv)
    ws = plan_repo.get_workouts(plan["id"])
    print(f"plan#{plan['id']} vdot={pv} workouts={len(ws)}")
    print(f"pace_table={ {k: v for k, v in paces.items() if k != 'vdot'} }")

    by_src = Counter()
    bad = []
    for w in ws:
        z = w.get("pace_zone")
        if not z:
            by_src["(no zone)"] += 1
            continue
        exp = paces.get(z)
        if exp is None:
            by_src[f"zone={z} 不在 pace_table"] += 1
            continue
        if isinstance(exp, dict):
            exp_s, exp_f = exp["slow_s_km"], exp["fast_s_km"]
        else:
            exp_s = exp_f = exp
        got_s, got_f = w.get("pace_slow_s_km"), w.get("pace_fast_s_km")
        ok = (got_s is not None and abs(got_s - exp_s) < 0.5
              and got_f is not None and abs(got_f - exp_f) < 0.5)
        if not ok:
            by_src[f"漂移 src={w.get('source')} zone={z} status={w.get('status')}"] += 1
            bad.append((w["date"], w["slot"], z, w.get("source"), w.get("status"),
                        w.get("title"), got_s, got_f, exp_s, exp_f))
        else:
            by_src["ok"] += 1

    print("\n=== 分类 ===")
    for k, v in by_src.most_common():
        print(f"  {v:4d}  {k}")

    print("\n=== 漂移明细（前 25）===")
    print(f"  {'date':10s} {'slot':4s} {'zone':5s} {'src':8s} {'status':8s} "
          f"{'存slow':>7} {'应slow':>7} {'存fast':>7} {'应fast':>7}  title")
    for d, s, z, src, st, t, gs, gf, es, ef in bad[:25]:
        f = lambda v: f"{int(v // 60)}:{int(v % 60):02d}" if v else "  --  "  # noqa: E731
        print(f"  {d:10s} {str(s):4s} {str(z):5s} {str(src):8s} {str(st):8s} "
              f"{f(gs):>7} {f(es):>7} {f(gf):>7} {f(ef):>7}  {t}")

    # 引擎 source 的课是否漂移
    eng = [w for w in ws if w.get("source") == "engine"]
    print(f"\n引擎课 {len(eng)} 节，其中漂移 "
          f"{sum(1 for b in bad if b[3] == 'engine')} 节；"
          f"其他 source：{Counter(w.get('source') for w in ws)}")
    # 按日期看漂移分布
    print(f"漂移课日期范围：{min(b[0] for b in bad) if bad else '-'} → "
          f"{max(b[0] for b in bad) if bad else '-'}")
    print(f"课表日期范围：{min(w['date'] for w in ws)} → {max(w['date'] for w in ws)}")


if __name__ == "__main__":
    main()
