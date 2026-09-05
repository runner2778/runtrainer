"""看 structure_json 实际结构 + 分类字段。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtrainer.config import DATA_DIR  # noqa: E402

c = sqlite3.connect(Path(DATA_DIR) / "runtrainer.db")
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT name, structure_json FROM activities
                    WHERE source='garmin' AND structure_json IS NOT NULL LIMIT 5""").fetchall()
for r in rows:
    sj = json.loads(r["structure_json"])
    print(r["name"], "->", type(sj).__name__,
          json.dumps(sj, ensure_ascii=False)[:500], "\n---")
# 分类字段在哪里：用不同提取方式统计
for expr in ("$.kind", "$[0].kind", "$.summary.kind", "$.type"):
    n = c.execute("""SELECT COUNT(*) FROM activities
                     WHERE structure_json IS NOT NULL
                       AND json_extract(structure_json, ?) IS NOT NULL""",
                  (expr,)).fetchone()[0]
    print(expr, "non-null:", n)
c.close()
