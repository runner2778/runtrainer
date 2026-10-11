"""一键评估取数探测：近 3 周真实库里到底有哪些证据可用。只读。

要回答的问题：
  1) 近 3 周练了什么（次数/里程/配速/心率分布）
  2) 间歇课（structure 里的 work 段）有多少、配速与心率什么水平
  3) 储备心率区间所需的 HRmax / HRrest 从哪来、有没有值
  4) 有没有比赛/最佳成绩类的现成数据
"""
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

with database.get_conn() as conn:
    print("=== 表清单 ===")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(tables)

    end = date.today()
    start = end - timedelta(days=20)
    print(f"\n=== 近 3 周（{start} ~ {end}）活动 ===")
    rows = activity_repo.list_structure_rows(start.isoformat(), end.isoformat())
    print(f"活动 {len(rows)} 个")
    for r in sorted(rows, key=lambda x: x["start_ts"]):
        d = datetime.fromtimestamp(r["start_ts"]).strftime("%m-%d %a")
        segs = jsonutil.loads(r.get("structure_json")) or []
        kinds = Counter(s.get("type") or "?" for s in segs)
        km = (r.get("distance_m") or 0) / 1000

        def fmt(p):
            return f"{int(p) // 60}:{int(p) % 60:02d}" if p else "—"

        print(f"  {d}  {km:5.2f}km  配速 {fmt(r.get('avg_pace_s_km')):>5}/km  "
              f"心率 {r.get('avg_hr') or '—':>4}/{r.get('max_hr') or '—':<4}  段 {dict(kinds) or '无结构'}")

    print("\n=== 近 3 周段级证据（按 type） ===")
    agg = defaultdict(lambda: {"n": 0, "km": 0.0, "p": [], "hr": []})
    for r in rows:
        for s in (jsonutil.loads(r.get("structure_json")) or []):
            t = s.get("type") or "?"
            a = agg[t]
            a["n"] += 1
            a["km"] += (s.get("distance_m") or 0) / 1000
            if s.get("pace_s_km"):
                a["p"].append(s["pace_s_km"])
            if s.get("avg_hr"):
                a["hr"].append(s["avg_hr"])
    for t, a in sorted(agg.items()):
        p = a["p"]
        hr = a["hr"]
        print(f"  {t:12} 段数 {a['n']:>3}  里程 {a['km']:6.1f}km  "
              f"配速 {min(p) if p else 0:.0f}~{max(p) if p else 0:.0f}s/km  "
              f"心率 {min(hr) if hr else 0:.0f}~{max(hr) if hr else 0:.0f}")

    print("\n=== 心率基线来源 ===")
    mh = conn.execute("SELECT MAX(max_hr), MAX(avg_hr) FROM activities").fetchone()
    print(f"  全库活动 max_hr 最大值 {mh[0]}；avg_hr 最大值 {mh[1]}")
    for t in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
        hit = [c for c in cols if "rest" in c or "hr" in c or "max" in c]
        if hit and t != "activities":
            print(f"  {t}: {hit}")

    print("\n=== 比赛 / 最佳成绩类数据 ===")
    for t in tables:
        if any(k in t for k in ("race", "best", "predict", "goal", "result")):
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}（{n} 行）: {cols}")

