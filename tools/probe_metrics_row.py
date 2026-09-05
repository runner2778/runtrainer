"""打印全部 metricDescriptors 与一行原始 metrics 值对齐。"""
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
eid = activity_repo.list_activities(limit=1)[0]["external_id"]

adapter = GarminConnectAdapter(*settings_service.get_garmin_credentials(),
                               is_cn=settings_service.is_garmin_cn())
adapter.login()
d = adapter._client.get_activity_details(int(eid))
descs = d.get("metricDescriptors") or []
rows = d.get("activityDetailMetrics") or []
print("描述符数:", len(descs), "行数:", len(rows))
for i, md in enumerate(descs):
    print(f"  [{md.get('metricsIndex')}] {md.get('key'):30s} unit={md.get('unit')}")
for ri in (0, 650, 1301):
    row = rows[ri].get("metrics") if isinstance(rows[ri], dict) else rows[ri]
    print(f"row[{ri}]:", row)
