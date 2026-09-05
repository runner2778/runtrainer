"""真实账号同步调试：保存凭据 → 重置断点 → 全量同步 → 输出统计与状态。

附带验证：档案覆盖、mock 清理、课表按最新 VDOT 重建结果。
用法：.venv\\Scripts\\python tools\\sync_real.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo, plan_repo, profile_repo, sync_repo
from runtrainer.garmin import sync_service
from runtrainer.services import settings_service

config.ensure_dirs()
config.init_logging()
database.migrate()

print("== 读取凭据（只存于 Windows 凭据管理器，绝不落盘/日志） ==")
u, p = settings_service.get_garmin_credentials()
print(f"  keyring username={u!r} password={'已设置' if p else '未设置'}")
print(f"  mock_mode={settings_service.is_mock_mode()} garmin_cn={settings_service.is_garmin_cn()}")

print("== 主题切到黑红暗色 ==")
settings_service.set_theme("dark")
print(f"  theme={settings_service.get_theme()!r}")

print("== 重置断点（旧游标来自 mock 时代，已指向今天，导致真实数据拉不到） ==")
sync_repo.set_sync_state("garmin", meta={}, error=None)

print("== 开始真实同步 ==")
t0 = time.time()
try:
    stats = sync_service.sync_all()
    print(f"  耗时 {time.time() - t0:.1f}s")
    print(f"  STATS: {json.dumps(stats, ensure_ascii=False)}")
except Exception as e:  # noqa: BLE001
    print(f"  耗时 {time.time() - t0:.1f}s")
    print(f"  同步失败: {type(e).__name__}: {e}")

print("== 同步后验证 ==")
prof = profile_repo.get_profile() or {}
print(f"  档案: nickname={prof.get('nickname')!r} sex={prof.get('sex')!r} "
      f"birth={prof.get('birth_year')!r} h={prof.get('height_cm')!r} "
      f"w={prof.get('weight_kg')!r} vo2max={prof.get('vo2max')!r}")
acts = activity_repo.list_activities(limit=1000)
real = [a for a in acts if a["source"] == "garmin" and not str(a["external_id"]).startswith("mock_")]
mock_left = [a for a in acts if str(a["external_id"]).startswith("mock_") or a["source"] == "demo"]
print(f"  活动: 总数={len(acts)} 真实={len(real)} 演示残留={len(mock_left)}")
plan = plan_repo.get_active_plan()
if plan:
    print(f"  计划: id={plan['id']} vdot={plan['vdot']} "
          f"race={plan['race_date']} status={plan['status']}")
st = sync_repo.get_sync_state("garmin")
print(f"  同步状态: last_sync_ts={st['last_sync_ts']} last_error={st['last_error']!r}")
print(f"  meta={st['meta_json']}")
