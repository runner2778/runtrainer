r"""复现「教练聊天」链路（只读：不落库、不发消息、真实调用 AI）。

用法：.venv\Scripts\python.exe tools\probe_chat_echo.py [消息]
默认消息「我这几天跑得有点累，帮我看看」。输出：模型、max_tokens、
复述防护（_validated_no_echo）结果、reply 全文、调整/重估建议。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.ai import prompt_builder  # noqa: E402
from runtrainer.ai.contracts import ChatOutput  # noqa: E402
from runtrainer.db.repos import chat_repo  # noqa: E402
from runtrainer.services import coach_service, plan_service  # noqa: E402
from runtrainer.utils import dates  # noqa: E402

message = sys.argv[1] if len(sys.argv) > 1 else "我这几天跑得有点累，帮我看看"
today = dates.today()
ctx = coach_service._gather(today, False, message)
if ctx is None:
    print("!! 无活动计划")
    sys.exit(1)
ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}
# 与生产相同：AI 上下文带最近 50 条消息（含被「清空对话」隐藏的——保留记忆）
prompt = prompt_builder.build_chat(ctx, chat_repo.list_messages(limit=50))
client = coach_service._make_client(False)
print("client:", client.model, "| max_tokens:", client.max_tokens)
out = coach_service._validated_no_echo(client, prompt, ChatOutput)
print("reply（%d 字）:" % len(out.reply))
print(out.reply)
print("user_requested:", out.user_requested, "| adjustments:", len(out.adjustments),
      "| rebuild:", out.rebuild_plan, "| profile_updates:", out.profile_updates)
print("（本探针只读：未写库、未发消息）")
