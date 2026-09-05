"""重置健康数据断点：删除 sync_state 的 last_health_date。

旧 120 天窗口把断点推进到今天，新 365 天分批逻辑因 start>end 被跳过。
删掉断点后，下一轮同步从 today-365 开始分批回填（每轮 90 天）。
保留 cursor_ts/last_stats 等其余字段。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402
from runtrainer.db.repos import sync_repo  # noqa: E402

SOURCE = "garmin"
state = sync_repo.get_sync_state(SOURCE)
meta = json.loads(state.get("meta_json")) if state.get("meta_json") else {}
removed = meta.pop("last_health_date", None)
print(f"删除前 last_health_date = {removed}")
sync_repo.set_sync_state(SOURCE, meta=meta, error=state.get("last_error"))
print("已重置；下一轮同步将从今天-365 天开始分批回填")
print("剩余 meta keys:", sorted(meta.keys()))
