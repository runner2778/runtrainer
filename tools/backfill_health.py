"""多轮同步回填一年健康数据（每轮 90 天，最多 6 轮）。

每轮之间稍作停顿，避免 Garmin 限流。任一轮 health_error 立即停止
（断点不动，人工查看后重跑）。追平（无 health_backfill）即停。
"""
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.config import init_logging  # noqa: E402
from runtrainer.garmin import sync_service  # noqa: E402

init_logging()
logging.getLogger().setLevel(logging.WARNING)  # 每轮打太多，只看告警

MAX_ROUNDS = 6
for rnd in range(1, MAX_ROUNDS + 1):
    print(f"\n=== 第 {rnd} 轮 ===", flush=True)
    try:
        stats = sync_service.sync_all()
    except Exception as e:
        print(f"同步异常: {e}")
        break
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    if stats.get("health_error"):
        print("health_error 出现，停止（断点未动，可稍后重跑本脚本）")
        break
    if not stats.get("health_backfill"):
        print("已追平到今天，完成")
        break
    time.sleep(8)  # 轮间缓一缓，避免限流
else:
    print(f"\n已达 {MAX_ROUNDS} 轮上限；如未追平可再次运行本脚本")
