"""只读探针：拆开证据管线，看每个距离的预测被哪条通道拖慢。

做法：不在生产代码里加任何东西，只在探针里 monkeypatch 几个纯函数，
把「各通道在场值 / 权重 / 每距离最终 VDOT」打出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402
from runtrainer.services import plan_service  # noqa: E402

database.migrate()

LOG: dict[str, object] = {"race_channels": [], "buckets": [], "fitness": None,
                          "pred": []}


def fmt(sec) -> str:
    h, r = divmod(int(round(sec)), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main() -> None:
    orig_race = ab._race_channel
    orig_be = ab._be_bucket_vdot
    orig_fit = evm.current_fitness
    orig_pt = vd.predict_time

    def race_spy(races_graded, as_of, label=None):
        val, detail = orig_race(races_graded, as_of, label)
        LOG["race_channels"].append({"label": label, "vdot": val, "detail": detail})
        return val, detail

    def be_spy(fitness, buckets):
        val = orig_be(fitness, buckets)
        LOG["buckets"].append({"buckets": buckets, "vdot": val})
        return val

    def fit_spy(evidence, deltas, as_of=None, **kw):
        out = orig_fit(evidence, deltas, as_of, **kw)
        LOG["fitness"] = out
        return out

    def pt_spy(dist, v):
        sec = orig_pt(dist, v)
        LOG["pred"].append({"dist": dist, "v": v, "sec": sec})
        return sec

    ab._race_channel = race_spy
    ab._be_bucket_vdot = be_spy
    evm.current_fitness = fit_spy
    vd.predict_time = pt_spy

    res = plan_service.ability_assessment()
    ab._race_channel, ab._be_bucket_vdot = orig_race, orig_be
    evm.current_fitness = orig_fit
    vd.predict_time = orig_pt

    print("== 比赛通道（全开，label=None 为混合池化） ==")
    for r in LOG["race_channels"]:
        print(f"  {str(r['label']):>4} → {r['vdot']}   {r['detail'][:6]}")

    print("\n== 各距离用的最快分段档 ==")
    for b in LOG["buckets"]:
        print(f"  {b['buckets']} → {b['vdot']}")

    fit = LOG["fitness"] or {}
    print("\n== 训练通道（近 90 天，已做个人转换） ==")
    for k in ("interval", "steady", "best_effort"):
        f = fit.get(k)
        if f:
            print(f"  {k:>12} vdot={f.get('vdot')} n={f.get('n')} "
                  f"detail={json.dumps({x: y for x, y in f.items() if x not in ('vdot', 'n')}, ensure_ascii=False)[:200]}")
    print("  best_effort_buckets =",
          json.dumps({k: v.get("vdot") for k, v in (fit.get("best_effort_buckets") or {}).items()},
                     ensure_ascii=False))

    print("\n== 每距离最终 VDOT → 预测 ==")
    lbl_of = {v: k for k, v in vd.DISTANCES.items()}
    for p in LOG["pred"]:
        print(f"  {lbl_of.get(p['dist'], p['dist']):>4} v_at={p['v']}  → {fmt(p['sec'])}")

    print("\n== 生产 predictions / year_bests / 个人比赛 ==")
    print("  vdot =", res.get("vdot"))
    print("  predictions =", json.dumps({k: fmt(v) for k, v in (res.get("predictions") or {}).items()},
                                        ensure_ascii=False))
    print("  year_bests =", json.dumps(res.get("year_bests"), ensure_ascii=False))
    print("  personal_races =", res.get("personal_races"))
    ph = res.get("personal_history") or []
    print(f"  personal_history n={len(ph)}")
    for e in ph:
        print(f"    {e.get('day')} {str(e.get('label')):>4} actual={e.get('actual_vdot')} "
              f"pred={e.get('pred_vdot')} quality={e.get('quality')}")


if __name__ == "__main__":
    main()
