"""只读实验：用真实库回归出「跑步机」与「爬升」的配速修正系数。

模型（最小二乘，纯标准库实现）：
    pace_s_km ~ b0 + b1*avg_hr + b2*grade_m_per_km + b3*is_treadmill + b4*days_ago
用 days_ago 吸收一年内的体能漂移（否则「同心率更快」可能只是进步）。
输出系数与 t 值，供 evidence.py 定常量。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtrainer.db import database  # noqa: E402
from runtrainer.db.repos import activity_repo  # noqa: E402

database.migrate()

TM_HINTS = ("treadmill", "跑步机", "indoor", "室内", "跑步機", "操场跑步机")
# 室内/操场无法用 GPS 爬升，爬升项对它们无意义 → 单独看

def ols(X: list[list[float]], y: list[float]) -> tuple[list[float], list[float]]:
    """返回 (beta, se)。正规方程 + 高斯消元，n 不大够用。"""
    n, k = len(X), len(X[0])
    # A = X^T X, b = X^T y
    A = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    bb = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    # 求逆
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(A)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            raise ValueError("singular")
        pv = M[c][c]
        M[c] = [v / pv for v in M[c]]
        for r in range(k):
            if r != c and abs(M[r][c]) > 1e-15:
                f = M[r][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    inv = [row[k:] for row in M]
    beta = [sum(inv[a][b] * bb[b] for b in range(k)) for a in range(k)]
    resid = [y[i] - sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    dof = n - k
    s2 = sum(r * r for r in resid) / dof
    se = [math.sqrt(s2 * inv[a][a]) for a in range(k)]
    r2 = 1 - sum(r * r for r in resid) / sum((v - sum(y) / n) ** 2 for v in y)
    return beta, se + [r2]


def main() -> None:
    today = max(a["start_ts"] for a in activity_repo.list_activities(limit=5000))
    rows = []
    for a in activity_repo.list_activities(limit=5000):
        p, hr, d = a.get("avg_pace_s_km"), a.get("avg_hr"), a.get("distance_m") or 0
        if not p or not hr or hr < 95 or d < 3000 or p > 660:
            continue
        name = (a.get("name") or "").lower()
        tm = 1.0 if any(h in name for h in TM_HINTS) else 0.0
        grade = (a.get("elevation_gain_m") or 0) / (d / 1000.0)
        days = (today - a["start_ts"]) / 86400.0
        rows.append((p, hr, grade, tm, days, a))

    print(f"样本 {len(rows)} 条（≥3km、有 HR、配速合理）")

    # M1 全样本：跑步机系数
    X, y = [], []
    for p, hr, grade, tm, days, _ in rows:
        X.append([1.0, hr, grade, tm, days])
        y.append(p)
    beta, se = ols(X, y)
    names = ["const", "avg_hr", "grade_m_per_km", "is_treadmill", "days_ago"]
    print("\n=== M1 全样本 OLS（因变量 = avg_pace_s_km）===")
    for i, nm in enumerate(names):
        t = beta[i] / se[i]
        print(f"  {nm:>16s}  b={beta[i]:+9.3f}  se={se[i]:6.3f}  t={t:+7.2f}")
    print(f"  R²={se[-1]:.3f}  n={len(y)}")

    # M2 只户外（排除跑步机）：爬升系数更干净
    X2, y2 = [], []
    for p, hr, grade, tm, days, _ in rows:
        if tm:
            continue
        X2.append([1.0, hr, grade, days])
        y2.append(p)
    beta2, se2 = ols(X2, y2)
    names2 = ["const", "avg_hr", "grade_m_per_km", "days_ago"]
    print("\n=== M2 仅户外 OLS ===")
    for i, nm in enumerate(names2):
        t = beta2[i] / se2[i]
        print(f"  {nm:>16s}  b={beta2[i]:+9.3f}  se={se2[i]:6.3f}  t={t:+7.2f}")
    print(f"  R²={se2[-1]:.3f}  n={len(y2)}")

    # M3 只户外 + 爬升分档残差（看线性是否成立）
    print("\n=== M3 爬升分档残差（M2 拟合后，按爬升档看均值残差）===")
    from collections import defaultdict
    buckets: dict[int, list[float]] = defaultdict(list)
    for p, hr, grade, tm, days, _ in rows:
        if tm:
            continue
        pred = beta2[0] + beta2[1] * hr + beta2[2] * grade + beta2[3] * days
        buckets[min(int(grade // 5) * 5, 40)].append(p - pred)
    for k in sorted(buckets):
        v = buckets[k]
        print(f"  爬升 {k:2d}-{k + 5:2d} m/km  n={len(v):3d}  平均残差 {sum(v) / len(v):+7.1f}s/km")

    # M4 跑步机残差（M2 拟合后，跑步机样本的残差）——独立验证 H1
    tm_res = []
    for p, hr, grade, tm, days, _ in rows:
        if not tm:
            continue
        pred = beta2[0] + beta2[1] * hr + beta2[2] * grade + beta2[3] * days
        tm_res.append(p - pred)
    if tm_res:
        tm_res.sort()
        print(f"\n=== M4 跑步机在户外模型下的残差 n={len(tm_res)} ===")
        print(f"  中位 {tm_res[len(tm_res) // 2]:+.1f} s/km  均值 "
              f"{sum(tm_res) / len(tm_res):+.1f}")
        print("  （负 = 同心率跑步机更快 → 其配速证据需按此加时）")


if __name__ == "__main__":
    main()
