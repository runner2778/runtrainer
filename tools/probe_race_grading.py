"""只读探针：18 场「比赛」的分级审计——哪些被判次全力、依据是什么。"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.services import plan_service  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

database.migrate()

CALLS: list[dict] = []


def main() -> None:
    orig = evm.grade_race

    def spy(race, evidence, deltas, max_hr=None, prior_races=None):
        q = orig(race, evidence, deltas, max_hr, prior_races)
        vals = []
        day = race.get("day")
        try:
            rd = date.fromisoformat(str(day)[:10])
            lo = rd - timedelta(days=evm.RACE_EVIDENCE_DAYS)
            for r in evidence:
                if not (lo <= date.fromisoformat(r["day"]) <= rd):
                    continue
                if r["kind"] not in evm.KINDS:
                    continue
                d = (deltas.get(r["kind"]) or {}).get("delta", evm.CONV_PRIOR[r["kind"]])
                vals.append((r["kind"], round(evm._eff_adjusted(r["vdot"], r["kind"], r.get("hr_ratio")) + d, 1)))
        except Exception as e:  # noqa: BLE001
            vals = [("ERR", str(e))]
        CALLS.append({"day": day, "label": race.get("label"),
                      "actual": race.get("actual_vdot"), "hr_ref": race.get("hr_ref"),
                      "name": race.get("name"), "quality": q,
                      "prior": prior_races, "train_vals": vals})

    evm.grade_race = spy
    res = plan_service.ability_assessment()
    evm.grade_race = orig

    print("== 比赛分级审计 ==")
    for c in CALLS:
        t = c["train_vals"]
        med = sorted(v for _, v in t)[len(t) // 2] if len(t) >= evm.RACE_EVIDENCE_MIN_N else None
        print(f"  {c['day']} {str(c['label']):>4} 成绩VDOT={c['actual']:<5} hr_ref={c['hr_ref']} "
              f"→ {c['quality']:<7} 赛前30天训练证据 n={len(t)} 中位={med}  "
              f"prior={c['prior']}  «{c['name']}»")

    print("\n== 个人误差学习历史 ==")
    print("  res['personal_races'] =", res.get("personal_races"))
    try:
        from runtrainer.services import plan_service as ps
        ph = ps.load_personal_history() if hasattr(ps, "load_personal_history") else None
        print("  load_personal_history =", json.dumps(ph, ensure_ascii=False)[:600])
    except Exception as e:  # noqa: BLE001
        print("  ", e)

    print("\n== 各通道（同探针 probe_prediction_drag 口径） ==")
    print("  vdot =", res.get("vdot"))
    print("  predictions =", json.dumps(res.get("predictions"), ensure_ascii=False))
    print("  year_bests =", json.dumps(res.get("year_bests"), ensure_ascii=False))
    prof = profile_repo.get_profile() or {}
    acts = activity_repo.list_activities(limit=3000)
    emax = ab.estimate_max_hr(prof.get("max_hr"), acts)
    print("  est_max =", emax)
    for r in ab.detect_races(acts, emax):
        print(f"    detected: {r['day']} {r['label']:>4} vdot={r['actual_vdot']} "
              f"hr_ref={r['hr_ref']} feel={r.get('feel')} rpe={r.get('rpe')} «{r['name']}»")


if __name__ == "__main__":
    main()
