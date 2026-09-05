"""真实同步一次：验证健康 365 分批回填 + max_hr 数据推断。"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.config import init_logging  # noqa: E402
from runtrainer.garmin import sync_service  # noqa: E402
from runtrainer.db.repos import sync_repo  # noqa: E402

init_logging()
logging.getLogger().setLevel(logging.INFO)

try:
    stats = sync_service.sync_all()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
except Exception as e:
    print("同步失败:", e)
    state = sync_repo.get_sync_state("garmin")
    print("last_error:", state["last_error"])
