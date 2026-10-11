"""只读探针：为「预估成绩系统持续优化」找真实库缺陷。

假设清单（逐条用真实数据验证，不成立就标【证据不足】）：
  H1 跑步机活动与户外活动在同心率下的配速存在系统差 → 跑步机会污染证据
  H2 证据管线里各 kind 的样本密度是否够（interval/steady/best_effort）
  H3 预估 VDOT 与课表 VDOT 的差距分布（配速同步是否真需要）
  H4 爬升对配速的影响是否被忽略（同心率下爬升 vs 平路）
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo, plan_repo, profile_repo  # noqa: E402
from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

database.migrate()

TM_HINTS = ("treadmill", "跑步机", "indoor", "室内", "跑步機")


def is_treadmill(a: dict) -> bool:
    n = (a.get("name") or "").lower()
    return any(h in n for h in TM_HINTS)


def h1_treadmill_bias(acts: list[dict]) -> None:
    print("=" * 72)
    print("H1 跑步机 vs 户外：同心率下的配速差")
    rows = []
    for a in acts:
        p, hr, d = a.get("avg_pace_s_km"), a.get("avg_hr"), a.get("distance_m") or 0
        if not p or not hr or hr < 100 or d < 3000 or p > 600:
            continue
        rows.append((is_treadmill(a), p, hr, d))
    tm = [r for r in rows if r[0]]
    od = [r for r in rows if not r[0]]
    print(f"  样本：跑步机 {len(tm)}  户外 {len(od)}")
    if len(tm) < 5 or len(od) < 5:
        print("  【证据不足】样本太少")
        return

    # 按心率分桶比同档配速
    def bucket(hr: float) -> str:
        return f"{int(hr // 10 * 10)}-{int(hr // 10 * 10) + 10}"

    from collections import defaultdict
    b_tm: dict[str, list[float]] = defaultdict(list)
    b_od: dict[str, list[float]] = defaultdict(list)
    for t, p, hr, _ in rows:
        (b_tm if t else b_od)[bucket(hr)].append(p)

    print(f"  {'心率档':>8} {'跑步机 n':>8} {'TM 配速':>9} {'户外 n':>7} {'OD 配速':>9} {'差(OD-TM)':>10}")
    diffs = []
    for k in sorted(set(b_tm) | set(b_od)):
        a_tm, a_od = b_tm.get(k, []), b_od.get(k, [])
        m_tm = statistics.median(a_tm) if a_tm else None
        m_od = statistics.median(a_od) if a_od else None
        d = (m_od - m_tm) if (m_tm and m_od) else None
        if d is not None:
            diffs.append(d)
        f = lambda v: f"{int(v // 60)}:{int(v % 60):02d}" if v else "  --  "  # noqa: E731
        print(f"  {k:>8} {len(a_tm):>8} {f(m_tm):>9} {len(a_od):>7} {f(m_od):>9} "
              f"{(f'+{d:.0f}s' if d and d > 0 else (f'{d:.0f}s' if d else '--')):>10}")
    if diffs:
        print(f"  → 中位系统差（户外配速 − 跑步机配速）：{statistics.median(diffs):+.0f} s/km"
              f"（正=同心率户外更慢=跑步机偏快）")


def h2_evidence_density(acts_raw: list[dict]) -> None:
    print("=" * 72)
    print("H2 证据管线各 kind 密度（走生产入口 plan_service.ability_assessment）")
    from runtrainer.services import plan_service
    res = plan_service.ability_assessment()
    print(f"  口径 window_days={res.get('window_days')} model={res.get('model')} "
          f"vdot={res.get('vdot')}")
    print(f"  predictions={res.get('predictions')}")
    print(f"  intervals={res.get('prediction_intervals')}")
    ev_list = res.get("ev_list") or []
    from collections import Counter
    print(f"  证据 {len(ev_list)} 条：{dict(Counter(e.get('kind') for e in ev_list))}")
    for e in ev_list[:20]:
        print(f"    {e.get('day')} {str(e.get('kind')):12s} {str(e.get('subtype')):10s} "
              f"vdot={e.get('vdot')} n={e.get('n_reps')} hr_ratio={e.get('hr_ratio')}")
    print("  evidence 明细（看板展示）：")
    for e in (res.get("evidence") or [])[:12]:
        print(f"    - {e.get('source')} :: {str(e.get('detail'))[:70]}")
    print(f"  race_vdot={res.get('race_vdot')}  grades="
          f"{[(g.get('label'), g.get('grade')) for g in (res.get('race_grades') or [])][:10]}")


def h3_vdot_gap(acts: list[dict]) -> None:
    print("=" * 72)
    print("H3 预估 VDOT vs 课表 VDOT（配速同步现状）")
    from runtrainer.services import plan_service
    plan = plan_repo.get_active_plan()
    if not plan:
        print("  【证据不足】无活动课表")
        return
    print(f"  课表 VDOT = {plan.get('vdot')}  引擎 {plan.get('engine_version')}  "
          f"生成于 {plan.get('generated_at')}")
    quick = plan_service.quick_assessment()
    print(f"  近 21 天快评 VDOT = {quick.get('vdot')}  预测 = {quick.get('predictions')}")
    print(f"  目标/比赛日：", end="")
    from runtrainer.db.repos import goal_repo
    g = goal_repo.get_active_goal()
    print(f"{g.get('distance_m')}m  {g.get('race_date')}  vdot={g.get('vdot')} "
          f"({g.get('vdot_source')})" if g else "无")
    pl = vd.pace_table(plan.get("vdot")) if plan.get("vdot") else None
    es = vd.pace_table(quick.get("vdot")) if quick.get("vdot") else None
    if pl and es:
        print(f"  {'zone':10s} {'课表':>8} {'预估':>8} {'差 s/km':>9}")
        for k in ("E", "M", "T1", "T", "I", "R"):
            v1, v2 = pl[k], es[k]
            v1 = v1["slow_s_km"] if isinstance(v1, dict) else v1
            v2 = v2["slow_s_km"] if isinstance(v2, dict) else v2
            f = lambda v: f"{int(v // 60)}:{int(v % 60):02d}"  # noqa: E731
            print(f"  {k:10s} {f(v1):>8} {f(v2):>8} {v1 - v2:>+9d}")
    ws = plan_repo.get_workouts(plan["id"])
    stale = 0
    for w in ws:
        z = w.get("pace_zone")
        if not z or z in ("E", "RECOVERY"):
            continue
        if z in es and w.get("pace_slow_s_km") and abs(w["pace_slow_s_km"] - es[z]) > 0.5:
            stale += 1
    print(f"  课表已排课 {len(ws)} 节；按当前预估 VDOT 应改配速的定配速课：{stale}")


def h4_elevation(acts: list[dict]) -> None:
    print("=" * 72)
    print("H4 爬升对配速的影响（同心率档内按坡度差分组）")
    rows = []
    for a in acts:
        p, hr, d = a.get("avg_pace_s_km"), a.get("avg_hr"), a.get("distance_m") or 0
        g = a.get("elevation_gain_m")
        if not p or not hr or hr < 100 or d < 5000 or p > 600 or g is None:
            continue
        rows.append((g / (d / 1000.0), p, hr))
    if len(rows) < 20:
        print(f"  【证据不足】样本 {len(rows)}")
        return
    flat = [r for r in rows if r[0] < 5]
    hilly = [r for r in rows if r[0] >= 10]
    print(f"  平路(<5m/km) n={len(flat)}  爬升(>=10m/km) n={len(hilly)}")
    from collections import defaultdict
    fb, hb = defaultdict(list), defaultdict(list)
    for g, p, hr in flat:
        fb[int(hr // 10)].append(p)
    for g, p, hr in hilly:
        hb[int(hr // 10)].append(p)
    print(f"  {'HR档':>5} {'平路 n':>7} {'平路配速':>9} {'爬升 n':>7} {'爬升配速':>9} {'差':>8}")
    for k in sorted(set(fb) & set(hb)):
        if len(fb[k]) < 3 or len(hb[k]) < 3:
            continue
        mf, mh = statistics.median(fb[k]), statistics.median(hb[k])
        f = lambda v: f"{int(v // 60)}:{int(v % 60):02d}"  # noqa: E731
        print(f"  {k * 10:>5} {len(fb[k]):>7} {f(mf):>9} {len(hb[k]):>7} {f(mh):>9} {mh - mf:>+8.0f}")
    print("  （正 = 同心率爬升更慢 → 爬升课的证据 VDOT 被系统性低估）")


def main() -> None:
    acts = activity_repo.list_activities(limit=3000)
    print(f"真实库活动 {len(acts)} 条，区间 "
          f"{acts[-1]['start_ts']} → {acts[0]['start_ts']}")
    h1_treadmill_bias(acts)
    h2_evidence_density(acts)
    h3_vdot_gap(acts)
    h4_elevation(acts)


if __name__ == "__main__":
    main()
