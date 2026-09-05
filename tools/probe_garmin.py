"""诊断：真实账号的 profile / activities 原始返回结构（全字段）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.services import settings_service

u, p = settings_service.get_garmin_credentials()
from garminconnect import Garmin

client = Garmin(u, p, is_cn=True)
client.login()

prof = client.get_user_profile()
print("== profile 全部键 ==")
print(json.dumps(list(prof.keys()), ensure_ascii=False))
print("== profile 非空值 ==")
print(json.dumps({k: v for k, v in prof.items() if v not in (None, "", [], {})},
                 ensure_ascii=False)[:800])

acts = client.get_activities(0, 3)
print(f"== activity[0] 全部键 ==")
print(json.dumps(list(acts[0].keys()), ensure_ascii=False))
print("== activity[0] 非空值 ==")
print(json.dumps({k: v for k, v in acts[0].items()
                  if v not in (None, "", [], {})}, ensure_ascii=False)[:1500])
