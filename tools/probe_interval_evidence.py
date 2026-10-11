"""间歇证据从哪来：structure_json 的 work 段在真实库里到底覆盖多少课。只读。

要回答：
  1) activities 表有哪些字段（有没有 laps_json）
  2) 近 16 周 / 近 3 周，多少活动有非 continuous 段
  3) 有 work 段的课 vs 全部课，长什么样（是不是计划课才生成结构）
  4) HRR 需要的 profile.max_hr/rest_hr/vo2max 和 daily_health.resting_hr 近况
"""
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.db import database  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

with database.get_conn() as conn:
    print("=== activities 字段 ===")
    print([c[1] for c in conn.execute("PRAGMA table_info(activities)")])

    print("\n=== profile ===")
    r = conn.execute("SELECT * FROM profile LIMIT 1").fetchone()
    print(dict(r) if r else "无档案")

    print("\n=== daily_health 近 30 天 resting_hr ===")
    rs = conn.execute(
        "SELECT date, resting_hr FROM daily_health WHERE date >= date('now','-30 day')"
        " ORDER BY date").fetchall()
    print(f"{len(rs)} 天有记录：" + ", ".join(f"{x['date']}={x['resting_hr']}" for x in rs[-12:]))

    end = date.today()
    for label, days in (("近 3 周", 21), ("近 16 周", 112)):
        start = end - timedelta(days=days)
        rows = conn.execute(
            "SELECT start_ts, distance_m, avg_pace_s_km, avg_hr, max_hr, structure_json,"
            " name FROM activities"
            " WHERE date(start_ts,'unixepoch','localtime') >= date(?) ORDER BY start_ts",
            (start.isoformat(),)).fetchall()
        with_struct = 0
        with_work = 0
        kinds = Counter()
        for a in rows:
            segs = jsonutil.loads(a["structure_json"]) or []
            if segs:
                with_struct += 1
            ts = {s.get("type") for s in segs}
            kinds.update(ts)
            if ts & {"work"}:
                with_work += 1
        print(f"\n=== {label} ===")
        print(f"  活动 {len(rows)} 个；有 structure: {with_struct}；含 work 段: {with_work}")
        print(f"  段类型分布: {dict(kinds)}")

    # 全库范围：有 work 段的活动都叫什么、什么特征
    print("\n=== 全库含 work 段的活动（最近 15 个）===")
    rows = conn.execute(
        "SELECT start_ts, distance_m, avg_pace_s_km, avg_hr, structure_json, name,"
        " sport FROM activities ORDER BY start_ts DESC").fetchall()
    hits = []
    for a in rows:
        segs = jsonutil.loads(a["structure_json"]) or []
        if any((s.get("type") == "work") for s in segs):
            hits.append(a)
    print(f"  全库共 {len(hits)} 个")
    for a in hits[:15]:
        d = datetime.fromtimestamp(a["start_ts"]).strftime("%Y-%m-%d")
        segs = jsonutil.loads(a["structure_json"]) or []
        c = Counter(s.get("type") for s in segs)
        p = a["avg_pace_s_km"]
        print(f"  {d} {a['sport'] or '?':10} {(a['distance_m'] or 0) / 1000:5.2f}km "
              f"{(f'{int(p) // 60}:{int(p) % 60:02d}' if p else '—'):>5}/km "
              f"{dict(c)}  name={a['name']}")

    # 有非 continuous 段的活动是否都对应 planned_workouts
    print("\n=== 计划完成情况（近 3 周）===")
    pw = conn.execute(
        "SELECT date, kind, status FROM planned_workouts"
        " WHERE date >= date('now','-21 day') ORDER BY date").fetchall()
    print(f"  planned_workouts 近 3 周 {len(pw)} 条: "
          + ", ".join(f"{x['date']}:{x['kind']}:{x['status']}" for x in pw[:20]))
