"""实测智谱 GLM-4-Flash 调通（走应用同款 DeepSeekClient）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.ai.deepseek_client import PROVIDERS, DeepSeekClient  # noqa: E402
from runtrainer.services import settings_service  # noqa: E402

info = PROVIDERS["zhipu"]
key = settings_service.get_ai_key("zhipu")
print("zhipu key present:", bool(key))
client = DeepSeekClient(key or "", "glm-4-flash", base_url=info["base_url"])
t0 = time.monotonic()
out = client.chat_json(
    "你是测试助手", '只输出一个 JSON 对象：{"ping": "pong"}，不要输出其他内容')
print(f"智谱调通耗时 {time.monotonic() - t0:.1f}s, 返回: {out}")
