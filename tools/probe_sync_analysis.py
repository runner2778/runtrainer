r"""复现「同步后自动分析」链路（只读：不落库、不发消息、真实调用 AI）。

用法：.venv\Scripts\python.exe tools\probe_sync_analysis.py [活动数]
默认取真实库最近 3 条活动当作「本次同步新增」，输出：
上下文活动行、模型回复、契约校验、护栏结果（不写任何数据）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.ai import guardrails, prompt_builder  # noqa: E402
from runtrainer.ai.contracts import ChatOutput, CoachOutput  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402
from runtrainer.services import coach_service, plan_service  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
today = dates.today()
ctx = coach_service._gather(today, False, "")
if ctx is None:
    print("!! 无活动计划")
    sys.exit(1)
new_rows = [a for a in activity_repo.list_activities(limit=500)
            if a.get("source") == "garmin" and a.get("distance_m")][:n]
print("today:", today, "| 取最近", n, "条活动当新数据")
for a in new_rows:
    print("  ", a["external_id"], dates.ts_to_datetime(a["start_ts"]), a.get("name"))

ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}
prompt = prompt_builder.build_sync_analysis(ctx, new_rows)
print("-- 上下文中的新活动行 --")
print("\n".join(l for l in prompt["user"].splitlines()
                if l.startswith("- ") and "「" in l))
print("-- 上下文中的最近训练活动详情块行数 --")
print(sum(1 for l in prompt["user"].splitlines() if l.startswith("- ")))
client = coach_service._make_client(False)
print("client:", type(client).__name__, getattr(client, "model", "?"),
      "| max_tokens:", getattr(client, "max_tokens", "?"))
# 走与生产相同的新链路：契约校验 + 复述检测（复述附提示重试一次，仍复述抛错）
out = coach_service._validated_no_echo(client, prompt, ChatOutput)
print("-- 契约校验 + 复述防护（_validated_no_echo 通过）--")
print("user_requested:", out.user_requested, "| adjustments:", len(out.adjustments))
print("reply（%d 字，全文）:" % len(out.reply))
print(out.reply)
fake = CoachOutput(summary="sync-analysis", readiness="ok", key_signals=[],
                   adjustments=out.adjustments, add_extra_advice=None, weekly_notes="")
items, glog = guardrails.validate(fake, guardrails.GuardContext(**ctx["guard"]))
print("-- 护栏结果（force=False）--")
print("通过:", len(items), "| 日志:", glog)
for it in items:
    w = next((x for x in ctx["week_workouts"] if x["id"] == it["planned_workout_id"]), None)
    print("   ", it["date"], it["action"], it.get("changes"), "→", w["date"] if w else "?")
print("（本探针只读：未写库、未发消息、未推进游标）")
