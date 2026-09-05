"""无 GUI 全链路演示/验收脚本（M5）：

mock 同步 → 设定目标 → 生成丹尼尔斯课表 → AI 教练（低 HRV 场景）调整 → 批准应用 → 断言不变量。

用法：.venv\\Scripts\\python.exe tools\\demo_script.py [数据目录]
"""
import os
import sys
import tempfile
from datetime import timedelta


def main() -> int:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="runtrainer_demo_")
    os.environ.setdefault("RUNTRAINER_DATA_DIR", data_dir)
    print(f"数据目录: {data_dir}")

    from runtrainer import config
    config.ensure_dirs()
    config.init_logging()
    from runtrainer.db import database
    database.migrate()
    print("✓ 数据库迁移完成")

    # 1. mock 同步（活动 + 健康数据）
    from runtrainer.garmin import sync_service
    stats = sync_service.sync_all()
    print(f"✓ mock 同步完成: {stats}")

    # 2. 目标 + 课表
    from runtrainer.db.repos import plan_repo
    from runtrainer.services import plan_service
    from runtrainer.utils import dates
    real_today = dates.today()
    race = real_today + timedelta(days=84)
    payload = plan_service.create_goal_and_plan({
        "goal": {"distance_m": 21097, "race_date": race.isoformat(),
                 "target_seconds": None, "vdot": 45.0, "name": "半马"},
        "plan": {"base_weekly_km": 40.0},
    })
    plan = plan_repo.get_plan(payload["plan_id"])
    workouts = plan_repo.get_workouts(plan["id"])
    print(f"✓ 课表生成: {payload['total_weeks']} 周 / {len(workouts)} 节课，"
          f"VDOT {payload['vdot']}（{payload['vdot_source']}），"
          f"峰值 {payload['peak_weekly_km']}km")

    # 3. AI 教练：把"今天"移到计划中部的强度课，低 HRV 场景
    hard = next(w for w in workouts
                if w["kind"] in ("T", "I") and 2 <= int(w["week_index"]) <= 8)
    dates.today = lambda: dates.date.fromisoformat(hard["date"])
    from runtrainer.ai.deepseek_client import MockClient
    from runtrainer.services import coach_service
    coach_service._make_client = lambda extra=False: MockClient("low_hrv")
    snap = coach_service.request_advice()
    advice = snap["advice"]
    print(f"✓ AI 建议: [{advice['readiness']}] {advice['summary']}")
    for a in advice["adjustments"]:
        print(f"    {a['applies_date']} {a['action']}: {a['reason']}")
    assert any(a["action"] == "modify" for a in advice["adjustments"]), "低 HRV 场景应有 modify"

    # 4. 批准应用
    res = coach_service.decide_advice(True)
    assert not res["errors"], res["errors"]
    print(f"✓ 批准应用: {res['applied']} 条生效")

    # 5. 不变量断言
    ws = plan_repo.get_workouts(plan["id"])
    hard_kinds = {"T", "I", "R", "M", "TUNEUP", "RACE"}
    by_date = {w["date"]: w for w in ws}
    for w in ws:
        is_hard = w["kind"] in hard_kinds or (w["kind"] == "LR" and w["pace_zone"] == "M")
        if is_hard:
            for off in (1, 2):
                nb = by_date.get((dates.date.fromisoformat(w["date"]) + timedelta(days=off)).isoformat())
                assert nb is None or not (nb["kind"] in hard_kinds
                                          or (nb["kind"] == "LR" and nb["pace_zone"] == "M")), \
                    f"相邻强度课: {w['date']} 与 {nb['date']}"
    changed = [w for w in ws if w["source"] == "ai"]
    assert changed, "应有 AI 调整后的课"
    assert any(w["kind"] == "E" for w in changed), "低 HRV 场景强度课应改为轻松跑"
    race_rows = [w for w in ws if w["kind"] == "RACE"]
    assert len(race_rows) == 1 and race_rows[0]["date"] == plan["race_date"]
    print(f"✓ 不变量通过: 无相邻强度日；AI 调整课 {len(changed)} 节；比赛课在 {plan['race_date']}")

    print("\n全链路演示通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
