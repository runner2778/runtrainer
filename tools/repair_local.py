"""本地修复：从 activity_samples 重建 has_samples/structure_json（不联网）。

背景：列表同步 upsert 曾用 None 覆盖详情回填字段（已修 activity_repo.upsert_activity
保留已有非空值）。采样行仍在库里，本工具直接重建结构与标记。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer import config
from runtrainer.db import database
from runtrainer.db.repos import activity_repo
from runtrainer.domain.workout_analysis import analyze_structure

config.ensure_dirs()
database.migrate()

acts = activity_repo.list_activities(limit=10000)
fixed = skipped = 0
for a in acts:
    samples = activity_repo.get_samples(a["id"])
    n_samp = len(samples or [])
    want_has = 1 if n_samp else 0
    if int(a.get("has_samples") or 0) == want_has and a.get("structure_json"):
        skipped += 1
        continue
    laps = json.loads(a.get("laps_json") or "[]")
    structure = None
    if n_samp:
        try:
            structure = analyze_structure(
                laps, a.get("duration_s"), a.get("distance_m"), samples=samples)
        except Exception as e:  # noqa: BLE001
            print(f"  活动 {a['id']} 结构分析失败: {e}")
    a["has_samples"] = want_has
    a["structure_json"] = json.dumps(structure, ensure_ascii=False) if structure else None
    activity_repo.upsert_activity(a)
    fixed += 1
print(f"修复 {fixed} 条，完好跳过 {skipped} 条")
print(f"has_samples=1: {sum(1 for a in activity_repo.list_activities(limit=10000) if a['has_samples'])}")
print(f"structure 非空: {sum(1 for a in activity_repo.list_activities(limit=10000) if a['structure_json'])}")
