"""看真实库 365 天 pace_bin_hr 的档位范围与时期数。只读。"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.domain.workout_analysis import pace_bin_hr  # noqa: E402

today = date.today()
acts = activity_repo.list_activities(source="garmin", limit=5000)
out = pace_bin_hr(acts, today - timedelta(days=365), today)
print("bins:", [b["start_s"] for b in out["bins"]])
print("n_periods:", len(out["periods"]))
for p in out["periods"]:
    print(" ", p["start"], "~", p["end"], "label:", p["label"])
    print("    hr:", p["hr"], "\n    runs:", p["runs"])
print("summary:", out.get("summary"))
