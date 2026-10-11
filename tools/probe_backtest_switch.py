"""只读实验：在同一份回测上试几种「减少保守/提高精度」的候选改法。

用法：python tools/probe_backtest_switch.py <config>
  base            不patch，当前生产口径
  no_tm_race      跑步机活动不算比赛（不进比赛通道/不触发按距离比赛权重）
  label_race_min2 某距离只有 1 场全开比赛时不切 SPEC_WEIGHTS_RACED、也不用该
                  距离自己的通道（单点高杠杆）
  env_race        比赛通道也做环境折算（跑步机/爬升），与训练证据同口径

每个 config 打印回测的真实比赛误差统计行。
"""
from __future__ import annotations

import io
import sys
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtrainer.domain import ability as ab  # noqa: E402
from runtrainer.domain import evidence as evm  # noqa: E402
from runtrainer.domain import vdot as vd  # noqa: E402

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "base"
print(f"### CONFIG = {CONFIG}", flush=True)

_orig_detect = ab.detect_races
_orig_race_channel = ab._race_channel

if CONFIG == "no_tm_race":
    def _detect(activities, est_max=None):
        rows = _orig_detect(activities, est_max)
        return [r for r in rows if not evm.is_treadmill(
            {"name": r.get("name")})]
    ab.detect_races = _detect

elif CONFIG == "label_race_min2":
    def _race_channel(races_graded, as_of, label=None):
        val, detail = _orig_race_channel(races_graded, as_of, label)
        if label is not None:
            n = sum(1 for r in races_graded
                    if r.get("quality") != "submax" and r.get("label") == label)
            if n < 2:
                return None, detail
        return val, detail
    ab._race_channel = _race_channel

elif CONFIG == "env_race":
    def _detect(activities, est_max=None):
        rows = _orig_detect(activities, est_max)
        by_id = {a.get("id"): a for a in activities}
        out = []
        for r in rows:
            a = by_id.get(r.get("id")) or {}
            pace = a.get("avg_pace_s_km")
            adj = evm.env_adjusted_pace(a, pace) if pace else None
            if adj and adj > pace:
                dist = r["distance_m"]
                dur = adj * dist / 1000.0
                r = {**r, "duration_s": round(dur),
                     "actual_vdot": round(vd.estimate_vdot(dist, dur), 1)}
            out.append(r)
        return out
    ab.detect_races = _detect

elif CONFIG == "tm_submax":
    # 跑步机活动（皮带驱动、无风无坡，配速与心率关系被改写）不参与
    # 能力锚定：判为次全力，不进比赛通道
    _orig_grade = evm.grade_race

    def _grade(race, evidence, deltas, max_hr=None, prior_races=None):
        if evm.is_treadmill({"name": race.get("name")}):
            return "submax"
        return _orig_grade(race, evidence, deltas, max_hr, prior_races)
    evm.grade_race = _grade

elif CONFIG.startswith("label_pool"):
    # 某距离自己的比赛通道样本少时，按条数向混合通道部分池化
    k = float(CONFIG.split("k")[1]) if "k" in CONFIG else 2.0

    def _race_channel(races_graded, as_of, label=None):
        val, detail = _orig_race_channel(races_graded, as_of, label)
        if label is None:
            return val, detail
        n = sum(1 for r in races_graded
                if r.get("quality") != "submax" and r.get("label") == label)
        if n == 0 or val is None:
            return val, detail
        pooled, _ = _orig_race_channel(races_graded, as_of, None)
        if pooled is None:
            return val, detail
        blended = round((val * n + pooled * k) / (n + k), 1)
        return blended, detail + [f"向混合通道池化 n={n} k={k} → {blended}"]
    ab._race_channel = _race_channel

elif CONFIG == "no_be":
    # 最快分段通道全部退出混合（它是最低的一条通道，可能系统性拖低中心）
    def _drop_be(weights):
        return {lbl: {k: v for k, v in w.items() if k != "best_effort"}
                for lbl, w in weights.items()}
    ab.SPEC_WEIGHTS = _drop_be(ab.SPEC_WEIGHTS)
    ab.SPEC_WEIGHTS_RACED = _drop_be(ab.SPEC_WEIGHTS_RACED)

elif CONFIG.startswith("no_be_"):
    # 只对指定距离退出最快分段通道，如 no_be_半马
    _only = CONFIG.split("no_be_", 1)[1]

    def _drop_be_one(weights):
        return {lbl: ({k: v for k, v in w.items() if k != "best_effort"}
                      if lbl == _only else dict(w))
                for lbl, w in weights.items()}
    ab.SPEC_WEIGHTS = _drop_be_one(ab.SPEC_WEIGHTS)
    ab.SPEC_WEIGHTS_RACED = _drop_be_one(ab.SPEC_WEIGHTS_RACED)

elif CONFIG == "label_fresh_only":
    # 矛盾加权只认「该距离自己」的近期比赛：删掉退回混合通道时的 freshness
    # 兜底（原先只要任意距离有近期比赛，过期同距离比赛也能被加码）
    _orig_fresh = ab._race_channel_fresh

    def _fresh(races_graded, as_of, label=None):
        if label is None:
            return False
        return _orig_fresh(races_graded, as_of, label)
    ab._race_channel_fresh = _fresh

elif CONFIG == "no_boost":
    # 彻底关掉矛盾加权（对照：boost 整体是加分项还是减分项）
    _orig_gap = ab.CONTRADICT_GAP
    ab.CONTRADICT_GAP = 1e9

elif CONFIG == "label_race_fresh":
    # 某距离自己的比赛通道里没有任何近 90 天比赛时，不拿它当该距离锚
    # （过期比赛只剩 60 天半衰期的加权中位在撑，n=1 时半衰期形同虚设）
    def _race_channel(races_graded, as_of, label=None):
        val, detail = _orig_race_channel(races_graded, as_of, label)
        if label is not None and val is not None:
            if not ab._race_channel_fresh(races_graded, as_of, label):
                return None, detail + ["同距离无近 90 天比赛 → 不作该距离锚"]
        return val, detail
    ab._race_channel = _race_channel

elif CONFIG == "combo":
    _orig_fresh = ab._race_channel_fresh

    def _fresh(races_graded, as_of, label=None):
        return False if label is None else _orig_fresh(races_graded, as_of, label)
    ab._race_channel_fresh = _fresh

    def _race_channel(races_graded, as_of, label=None):
        val, detail = _orig_race_channel(races_graded, as_of, label)
        if label is not None and val is not None:
            if not ab._race_channel_fresh(races_graded, as_of, label):
                return None, detail + ["同距离无近 90 天比赛 → 不作该距离锚"]
        return val, detail
    ab._race_channel = _race_channel

elif CONFIG.startswith("shrink_k"):
    # 个人转换差向群体先验收缩的强度：n/(n+K) 里的 K 越大，越信先验（先验是
    # 按真实比赛标定的，个人 EWMA 会滞后于状态变化）
    k = float(CONFIG.rsplit("k", 1)[1])
    evm.CONV_SHRINK_K = k

elif CONFIG.startswith("gap"):
    # 矛盾加权的斜坡：gap_G_sS → CONTRADICT_GAP=G、CONTRADICT_SPAN=S
    g, sp = CONFIG.split("_")[1].lstrip("g").split("s")
    ab.CONTRADICT_GAP = float(g)
    ab.CONTRADICT_SPAN = float(sp)

src = (ROOT / "tools" / "backtest_predict_v2.py").read_text(encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(compile(src, str(ROOT / "tools" / "backtest_predict_v2.py"), "exec"),
         {"__name__": "__main__", "__file__": str(ROOT / "tools" / "backtest_predict_v2.py")})

out = buf.getvalue()
import os  # noqa: E402
if os.environ.get("BT_FULL"):
    print(out, flush=True)
else:
    for line in out.splitlines():
        if "真实比赛 · 证据管线" in line or "全部检测 · 证据管线" in line \
                or "预测区间（真实比赛）" in line or "After·证据管线" in line \
                or "· 新(真)" in line:
            print(line, flush=True)
