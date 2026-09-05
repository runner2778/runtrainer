r"""仪表盘聚合链路只读探针：对真实库跑 get_dashboard，打印各块内容。

用法：.venv\Scripts\python.exe tools\probe_dashboard.py
不写库、不调 AI（教练块只读今日缓存与最新聊天）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.services import dashboard_service  # noqa: E402

d = dashboard_service.get_dashboard()
print("today:", d["today"], "| has_plan:", d["has_plan"])

r = d["readiness"]
print("恢复度:", r["status"], r["label"], "@", r["date"])
for it in r["items"]:
    print("   -", it["label"], it["value"], "→", it["status"])
print("   备注:", r["note"])

if d["has_plan"]:
    rc = d["race"]
    print("比赛:", rc["name"], rc["race_date"], "| 剩余", rc["days_left"], "天 |",
          f"第 {rc['current_week']}/{rc['total_weeks']} 周 | VDOT", rc["vdot"],
          f"| 进度 {rc['progress_pct']}%")
    wl = d["week_load"]
    print("本周负荷: 实际", wl["done_km"], "/ 计划", wl["planned_km"], "km |",
          f"完成 {wl['done_n']}/{wl['planned_n']} 次 | 课表完成 {wl['done_plan']} 节 | pct", wl["pct"])
    print("今日训练:", len(d["today_workouts"]), "节")
    for w in d["today_workouts"]:
        print("   -", f"slot{w['slot']}", w["kind"], w["title"],
              f"{w['distance_km'] or 0}km/{w['duration_min'] or 0}min", w["status"])
    k = d["kpis"]
    c7 = k["compliance_7d"]
    print("KPI: 完成度7d", c7["done_km"], "/", c7["planned_km"], "km =", c7["ratio"],
          "| ACWR", k["acwr"], k["acwr_status"],
          "| 单调性", k["monotony"], "| 应变", k["strain"],
          "| 上周", k["last_week_km"], "km")
    print("周序列（近 8 周 计划/实际）:")
    for w in d["weekly_series"]:
        mark = " ←本周" if w["current"] else ""
        print(f"   {w['label']}  {w['planned_km']:>6} / {w['done_km']:>6}{mark}")

print("健康趋势:", len(d["health_trend"]), "天")
for h in d["health_trend"][-3:]:
    print("   ", h["date"], "HRV", h["hrv"], h["hrv_status"], "RHR", h["resting_hr"],
          "睡眠", h["sleep_score"])

c = d["coach"]
print("教练今日建议:", (c["advice"] or {}).get("summary"))
if c["advice"]:
    for s in c["advice"]["key_signals"]:
        print("   -", s)
if c["last_chat"]:
    print("最新聊天:", c["last_chat"]["role"], c["last_chat"]["kind"],
          "|", c["last_chat"]["content"], "...")
s = d["sync"]
print("同步: last_sync_ts", s["last_sync_ts"], "| error:", s["error"],
      "| last_stats:", s["last_stats"])
print("（本探针只读：未写库、未调 AI）")
