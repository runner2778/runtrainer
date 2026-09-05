"""验证增量同步不覆盖上次结果：不做全量重置，直接 sync_all，
应无新增活动/健康数据，meta.last_stats 保留最近一次有意义的结果。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import sync_repo
from runtrainer.garmin import sync_service

config.ensure_dirs()
config.init_logging()
database.migrate()

before = sync_repo.get_sync_state("garmin")
before_meta = json.loads(before.get("meta_json") or "{}")
print(f"同步前 last_stats: {json.dumps(before_meta.get('last_stats'), ensure_ascii=False)}")

t0 = time.time()
try:
    stats = sync_service.sync_all()
    print(f"耗时 {time.time() - t0:.1f}s STATS: {json.dumps(stats, ensure_ascii=False)}")
except Exception as e:  # noqa: BLE001
    print(f"同步失败: {type(e).__name__}: {e}")
    sys.exit(1)

after = sync_repo.get_sync_state("garmin")
after_meta = json.loads(after.get("meta_json") or "{}")
print(f"同步后 last_stats: {json.dumps(after_meta.get('last_stats'), ensure_ascii=False)}")
print(f"last_error: {after['last_error']!r}")
