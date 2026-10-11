"""只读探针：主观数据（feel/rpe）在预估系统、健康系统、仪表盘三处的接入是否真的出数。

不写库、不改状态；用真实 DATA_DIR 的只读查询。
"""
import json
import sys
from datetime import date, timedelta

sys.path.insert(0, "src")

from runtrainer.db.repos import activity_repo, plan_repo
from runtrainer.domain.workout_analysis import weekly_subjective
from runtrainer.services import dashboard_service, plan_service


def hr(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


hr("1. 库里有多少活动带主观标签")
rows = activity_repo.list_subjective_rows()
labelled_rpe = [r for r in rows if r.get("rpe")]
labelled_feel = [r for r in rows if r.get("feel")]
print(f"总活动 {len(rows)}；带 RPE {len(labelled_rpe)}；带 feel {len(labelled_feel)}")

hr("2. weekly_subjective 真实输出（近 180 天）")
end = date.today()
start = end - timedelta(days=180)
wks = weekly_subjective(
    activity_repo.list_subjective_rows(start.isoformat(), end.isoformat()), start, end)
print(f"周数 {len(wks)}；有 RPE 均值的周 {sum(1 for w in wks if w['rpe_avg'] is not None)}")
for w in wks[:5]:
    print("  ", json.dumps(w, ensure_ascii=False))

hr("3. 仪表盘 subjective 块")
d = dashboard_service.get_dashboard()
print(json.dumps(d.get("subjective"), ensure_ascii=False, indent=2))

hr("4. 课表配速漂移状态")
print(json.dumps(plan_service.pace_sync_status(), ensure_ascii=False, indent=2))

hr("5. 环境折算试算（高坡度 vs 平地，同配速）")
from runtrainer.domain import evidence as evm
flat = {"name": "河边跑", "distance_m": 10000, "elevation_gain_m": 20}
hilly = {"name": "爬坡跑", "distance_m": 10000, "elevation_gain_m": 200}
tread = {"name": "Treadmill Running", "distance_m": 10000, "elevation_gain_m": 0}
for tag, a in (("平地", flat), ("爬坡", hilly), ("跑步机", tread)):
    print(f"  {tag}: 实测 330 s/km → 等效 {evm.env_adjusted_pace(a, 330.0):.1f} s/km")

hr("6. RPE 降权")
print("  rpe_weight: " + ", ".join(
    f"{v}→{evm.rpe_weight(v)}" for v in (None, 2, 5, 7, 10)))

hr("7. 有活跃课表时：手动补录会不会触发配速同步")
plan = plan_repo.get_active_plan()
print("活跃课表:", "有" if plan else "无")
