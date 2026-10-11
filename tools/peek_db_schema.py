"""只读探针：真实库 activities 表结构与字段充盈度（为「手动输入活动」定字段）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer import config  # noqa: E402


def main() -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    print("DB:", config.DB_PATH)
    print("user_version:", conn.execute("PRAGMA user_version").fetchone()[0])
    print()

    tabs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tabs)
    print()

    for t in ("activities",):
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"--- {t} ({n} rows) ---")
        for c in cols:
            name = c["name"]
            try:
                filled = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE {name} IS NOT NULL AND {name} != ''"
                ).fetchone()[0]
                d = conn.execute(
                    f"SELECT MIN({name}) lo, MAX({name}) hi FROM {t} "
                    f"WHERE {name} IS NOT NULL AND {name} != ''").fetchone()
                rng = f"  range=[{d['lo']}, {d['hi']}]" if d["lo"] is not None else ""
            except Exception as e:  # noqa: BLE001
                filled, rng = -1, f"  <{e}>"
            print(f"  {name:26s} {c['type']:10s} filled={filled:5d}{rng}")
        print()

    # 跑步活动按年分布 + 来源
    print("--- activities by year/source ---")
    for r in conn.execute(
        "SELECT substr(start_ts,1,4) y, source, COUNT(*) n FROM activities "
        "GROUP BY y, source ORDER BY y DESC, n DESC"):
        print(f"  {r['y']}  {str(r['source']):12s} {r['n']}")
    print()

    print("--- distinct activityType / name samples (last 15) ---")
    for r in conn.execute(
            "SELECT start_ts, name, distance_m, avg_pace_s_km, avg_hr, max_hr "
            "FROM activities ORDER BY start_ts DESC LIMIT 15"):
        print("  ", dict(r))
    conn.close()


if __name__ == "__main__":
    main()
