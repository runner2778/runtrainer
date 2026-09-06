"""实测智谱任意模型（走应用同款 DeepSeekClient）。

用法：probe_zhipu_live.py [模型名]  默认取 PROVIDERS["zhipu"] 首个候选。
第一步 ping 连通（JSON 契约）；成功后可用真实教练提示词验质量。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.ai.deepseek_client import PROVIDERS, DeepSeekClient  # noqa: E402
from runtrainer.services import settings_service  # noqa: E402

info = PROVIDERS["zhipu"]
model = sys.argv[1] if len(sys.argv) > 1 else info["models"][0]
key = settings_service.get_ai_key("zhipu")
print("zhipu key present:", bool(key), "| model:", model)
client = DeepSeekClient(key or "", model, base_url=info["base_url"],
                        max_tokens=info.get("max_tokens", 8192),
                        extra_body=info.get("extra_body"))
print("extra_body:", info.get("extra_body"))
t0 = time.monotonic()
out = client.chat_json(
    "你是测试助手", '只输出一个 JSON 对象：{"ping": "pong"}，不要输出其他内容')
print(f"连通耗时 {time.monotonic() - t0:.1f}s, 返回: {json.dumps(out, ensure_ascii=False)}")

if len(sys.argv) > 2:  # 质量模式：第二参数为训练者消息
    from runtrainer.ai import prompt_builder  # noqa: E402
    from runtrainer.ai.contracts import ChatOutput  # noqa: E402
    from runtrainer.db.repos import chat_repo  # noqa: E402
    from runtrainer.services import coach_service, plan_service  # noqa: E402
    from runtrainer.utils import dates  # noqa: E402

    message = sys.argv[2]
    ctx = coach_service._gather(dates.today(), False, message)
    if ctx is None:
        print("!! 无活动计划，跳过质量测试")
        sys.exit(0)
    ctx["ability"] = (plan_service.wizard_context() or {}).get("ability") or {}
    prompt = prompt_builder.build_chat(ctx, chat_repo.list_messages(limit=50))
    t1 = time.monotonic()
    out = coach_service._validated_no_echo(client, prompt, ChatOutput)
    print(f"\n质量测试耗时 {time.monotonic() - t1:.1f}s")
    print(f"reply（{len(out.reply)} 字）:")
    print(out.reply)
    print("user_requested:", out.user_requested, "| adjustments:", len(out.adjustments))
    print("（本探针只读：未写库、未发消息）")
