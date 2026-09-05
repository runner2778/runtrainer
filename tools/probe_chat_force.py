"""复现「今天改成长距离」聊天链路（只读：不落库、不改课表、真实调用 AI）。

用法：.venv\Scripts\python.exe tools\probe_chat_force.py [消息]
默认消息 = “今天改成长距离”。输出：今天课表、模型回复、user_requested、护栏结果。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.ai import guardrails, prompt_builder  # noqa: E402
from runtrainer.ai.contracts import ChatOutput, CoachOutput  # noqa: E402
from runtrainer.services import coach_service, plan_service  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

msg = sys.argv[1] if len(sys.argv) > 1 else "今天改成长距离"
today = dates.today()
print("today:", today)
ctx = coach_service._gather(today, False, msg)   # user_note=msg → 进【用户消息】块
if ctx is None:
    print("!! 无活动计划")
    sys.exit(1)

plan = ctx["plan"]
print("阶段/周:", plan["current_week"], plan["current_phase"], "| 本周目标 km:", plan["week_km"],
      "| 距比赛:", ctx["race_in_days"], "天")
print("-- 今天已有课 --")
for tw in ctx["today_workouts"]:
    print(" ", tw)
print("-- 未来 7 天课表（id/日期/类型/标题/状态）--")
for w in ctx["week_workouts"]:
    print(f"  id={w['id']} {w['date']} {w['kind']} [{w.get('title')}] 状态{w.get('status')} slot{w.get('slot') or 1}")

ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}
prompt = prompt_builder.build_chat(ctx, [])
client = coach_service._make_client(False)
print("client:", type(client).__name__, getattr(client, "model", "?"))
raw = client.chat_json(prompt["system"], prompt["user"], prompt["data"])
print("-- 模型原始返回(前 800 字) --")
print((raw if isinstance(raw, str) else repr(raw))[:800])

# 走真实校验+护栏链路（不落库）
out = ChatOutput.model_validate(raw if isinstance(raw, dict) else None) if isinstance(raw, dict) else None
if out is None and isinstance(raw, str):
    import json as _json
    out = ChatOutput.model_validate_json(raw)
print("-- 契约校验结果 --")
print("user_requested:", out.user_requested)
print("adjustments 数:", len(out.adjustments))
for a in out.adjustments:
    print("   ", a.model_dump())
fake = CoachOutput(summary="chat", readiness="ok", key_signals=[],
                   adjustments=out.adjustments, add_extra_advice=None, weekly_notes="")
items, glog = guardrails.validate(fake, guardrails.GuardContext(
    **ctx["guard"], force=bool(out.user_requested)))
print("-- 护栏结果（force=%s）--" % bool(out.user_requested))
print("通过条数:", len(items))
for it in items:
    print("   ", it)
print("护栏日志:", glog)
print("-- 若自动应用，这些课会变成 --")
for it in items:
    w = next((x for x in ctx["week_workouts"] if x["id"] == it["planned_workout_id"]), None)
    if w:
        print(f"  id={w['id']} {w['date']} {w['kind']}→{it['changes']}  (原:{w.get('title')})")
