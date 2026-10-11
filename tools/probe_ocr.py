"""只读/自检探针：截图识别管线（Windows OCR → 字段解析）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.services import ocr_service  # noqa: E402

SAMPLES = [
    ("标准成绩页", """Running
10.52
km
Time
52:31
Avg Pace
4'59" /km
Avg HR
148 bpm
Cadence
176 spm
Stride
1.14 m
Calories
712"""),
    ("OCR 实测噪声（全角/拆空格）", """Running
10 · 52
km
Time
52 ： 31
Avg Pace
km
4 。 59 “ /km
Avg H R
148 bpm
Cadence
176 spm
Stride
1 ． 1 4 m
7 1 2"""),
    ("中文 App 风格", """户外跑步
距离 12.05 公里
用时 1:02:18
平均配速 5'10"
平均心率 152 次/分
步频 174 步/分
步幅 1.06 米
消耗 826 千卡
感受 3
RPE 7"""),
    ("只有用时与心率（力量/室内）", """力量训练
用时 45 分钟
平均心率 118 bpm"""),
]


def main() -> None:
    ok = 0
    for title, text in SAMPLES:
        print("=" * 70)
        print(f"# {title}")
        r = ocr_service.parse_activity_text(text)
        print(f"  fields  = {r['fields']}")
        print(f"  matched = {r['matched']}")
        print(f"  warn    = {r['warnings']}")
        # 往返：build_row 能否吃下
        from runtrainer.domain import activity_edit as ae
        row = ae.build_row({**r["fields"], "name": r["fields"].get("name")})
        print(f"  row     = distance={row['distance_m']} dur={row['duration_s']} "
              f"pace={row['avg_pace_s_km']} stride={row['stride_length_m']} "
              f"feel={row['feel']} rpe={row['rpe']}")
        if r["fields"]:
            ok += 1
    print("=" * 70)
    print(f"解析成功 {ok}/{len(SAMPLES)}")

    # 真·调用 Windows OCR（用先前生成的测试图）
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path.home() / "AppData/Local/Temp/ocr_test.png"
    if img.exists():
        print(f"\n=== 真机 OCR：{img} ===")
        try:
            raw = ocr_service.recognize_text(img)
            print(raw)
            print("--- 解析 ---")
            print(ocr_service.parse_activity_text(raw)["fields"])
        except RuntimeError as e:
            print(f"  失败：{e}")


if __name__ == "__main__":
    main()
