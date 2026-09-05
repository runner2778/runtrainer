"""AI 抽查存疑跑步记录：课程名信号与按数据分类矛盾的课发给 DeepSeek 复核。

真实 API 一次调用，只读不写。输出逐条复核意见，供人工决定是否修正数据。
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.config import DATA_DIR  # noqa: E402
from runtrainer.ai.deepseek_client import DeepSeekClient  # noqa: E402
from runtrainer.domain.workout_analysis import classify_workout  # noqa: E402
from runtrainer.services import settings_service  # noqa: E402
from runtrainer.utils import jsonutil  # noqa: E402

MAX_HR, REST_HR = 201, 42
NAME_SIGNAL = {"乳酸阈值": "tempo", "阈值": "tempo", "冲刺": "interval",
               "间歇": "interval", "恢复": "recovery", "基础训练": "easy",
               "长距离": "easy"}

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT name, start_ts, duration_s, distance_m, avg_pace_s_km,
                    avg_hr, max_hr, structure_json FROM activities
                    WHERE source='garmin'""").fetchall()

cand = []
for r in rows:
    sig = next((k for kw, k in NAME_SIGNAL.items() if kw in (r["name"] or "")), None)
    if not sig:
        continue
    segs = jsonutil.loads(r["structure_json"]) or []
    w = classify_workout(segs, r["duration_s"], r["distance_m"], r["avg_hr"],
                         MAX_HR, REST_HR)
    # 只挑矛盾硬核的：tempo/interval 信号 vs 有氧以下分类（模糊的
    # recovery/easy 边界不进 AI——规则已按 HRR 处理，AI 复核价值低）
    if w["kind"] == sig:
        continue
    if sig in ("tempo", "interval") and w["kind"] in ("easy", "recovery", "aerobic", "unknown"):
        cand.append(r)

print(f"存疑候选 {len(cand)} 条，抽取最近 12 条发 DeepSeek 复核\n")
cand = cand[:12]
lines = []
for r in cand:
    segs = jsonutil.loads(r["structure_json"]) or []
    w = classify_workout(segs, r["duration_s"], r["distance_m"], r["avg_hr"],
                         MAX_HR, REST_HR)
    d = datetime.fromtimestamp(r["start_ts"]).strftime("%Y-%m-%d")
    km = round((r["distance_m"] or 0) / 1000, 1)
    pace = round(r["avg_pace_s_km"]) if r["avg_pace_s_km"] else None
    pmin = f"{pace // 60}:{pace % 60:02d}/km" if pace else "—"
    struct = "、".join(f"{s['type']}×{round(s['distance_m'])}m" for s in segs[:8]) or "无分段"
    lines.append(
        f"- {d} 课名「{r['name']}」 {km}km 均配速 {pmin} 均心率 {r['avg_hr']}"
        f" 结构[{struct}] → 软件分类「{w['label']}」")
user_prompt = "\n".join(lines)

system = (
    "你是跑步训练数据审核员。用户在 Garmin 手表上选的课程名有时与实际执行不符"
    "（比如选「冲刺训练」但当天只慢跑，或选「乳酸阈值」但心率远没到阈值）。"
    "软件按实际数据（配速/心率/分段结构）做了确定性分类。"
    "请逐条判断：软件分类是否符合该课实际执行内容？"
    "只输出 JSON：{\"items\": [{\"date\": \"YYYY-MM-DD\", \"verdict\": \"合理|存疑\","
    " \"reason\": \"一句话理由\"}]}，不要输出其他内容。"
    f"背景：跑者 max_hr≈{MAX_HR}、静息≈{REST_HR}，心率区按储备心率划分"
    f"（恢复<60%、轻松60-72%、有氧72-82%、节奏82-92%）。"
)

key = settings_service.get_deepseek_key()
if not key:
    print("未配置 DeepSeek key，跳过 AI 复核")
    sys.exit(0)
client = DeepSeekClient(key)
out = client.chat_json(system, user_prompt)
for it in out.get("items", []):
    print(f"{it.get('date')}  [{it.get('verdict')}] {it.get('reason')}")
print("\n（复核意见仅供参考；确定性规则分类保持不变）")
