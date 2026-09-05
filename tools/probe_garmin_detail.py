"""Garmin CN 真实响应结构探针：list 项 / 单活动 / 详情 metrics 的字段形态。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.services import settings_service
from runtrainer.garmin.garminconnect_adapter import GarminConnectAdapter

config.ensure_dirs()
database.migrate()

acts = activity_repo.list_activities(limit=5)
eid = acts[0]["external_id"] if acts else None
print("样本活动:", eid, acts[0]["name"] if acts else "")

adapter = GarminConnectAdapter(*settings_service.get_garmin_credentials(),
                               is_cn=settings_service.is_garmin_cn())
adapter.login()

# 1) 列表项
items = adapter._client.get_activities(0, 1) or []
if items:
    it = items[0]
    print("\n== 列表项 keys ==")
    print(sorted(it.keys()))
    print("duration:", it.get("duration"), "distance:", it.get("distance"),
          "averageHR:", it.get("averageHR"), "maxHR:", it.get("maxHR"))
    ss = it.get("splitSummaries") or []
    if ss:
        print("splitSummaries[0] keys:", sorted(ss[0].keys()))
        print("splitSummaries[0]:", {k: ss[0].get(k) for k in
              ("duration", "movingDuration", "totalDuration", "distance",
               "averageSpeed", "avgSpeed", "averageHR", "avgHR")})

# 2) 单活动摘要
try:
    s = adapter._client.get_activity(int(eid)) if eid else None
    if s:
        print("\n== 单活动 keys ==")
        print(sorted(s.keys()))
        print("duration:", s.get("duration"), "distance:", s.get("distance"),
              "averageHR:", s.get("averageHR"))
        ss = s.get("splitSummaries") or []
        if ss:
            print("splitSummaries[0] keys:", sorted(ss[0].keys()))
            print("splitSummaries[0]:", {k: ss[0].get(k) for k in
                  ("duration", "movingDuration", "totalDuration", "distance",
                   "averageSpeed", "avgSpeed", "averageHR", "avgHR")})
except Exception as e:
    print("\n单活动摘要失败:", e)

# 3) 详情 metrics
try:
    d = adapter._client.get_activity_details(int(eid)) if eid else None
    print("\n== 详情 keys ==", sorted(d.keys()) if isinstance(d, dict) else type(d))
    adm = d.get("activityDetailMetrics") if isinstance(d, dict) else None
    print("activityDetailMetrics 类型:", type(adm).__name__,
          "长度:", len(adm) if isinstance(adm, (list, tuple)) else "-")
    if isinstance(adm, (list, tuple)) and adm:
        for i, item in enumerate(adm[:3]):
            m = item.get("metrics") if isinstance(item, dict) else None
            print(f"  [{i}] keys={sorted(item.keys()) if isinstance(item, dict) else type(item)}"
                  f" metrics_len={len(m) if isinstance(m, (list, tuple)) else '-'}"
                  f" 首值={m[0] if isinstance(m, (list, tuple)) and m else '-'}")
        last = adm[-1].get("metrics") if isinstance(adm[-1], dict) else None
        print(f"  [末] metrics_len={len(last) if isinstance(last, (list, tuple)) else '-'}"
              f" 末值={last[-1] if isinstance(last, (list, tuple)) and last else '-'}")
    md2 = d.get("metricDescriptors") or []
    print("顶层 metricDescriptors 数:", len(md2))
    for md in md2[:4]:
        print("  desc:", md)
    print("measurementCount:", d.get("measurementCount"),
          "metricsCount:", d.get("metricsCount"),
          "totalMetricsCount:", d.get("totalMetricsCount"))
except Exception as e:
    print("\n详情失败:", e)
