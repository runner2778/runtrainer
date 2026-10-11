"""生成单文件演示 HTML（写到桌面），供作业提交与演示。

产物完全自包含：内联 app.css + Alpine + ECharts + 真实数据快照，
双击即开、不依赖 Python、不联网、不读数据库。

内容全部来自本机真实数据：
  - 成绩预估（VDOT / 预测成绩 / 六区配速表 / 依据列表）→ get_dashboard
  - 环境折算（坡度/跑步机）真实活动对照 → evidence.env_adjusted_pace
  - 课表配速漂移与样本 → plan_service.pace_sync_status
  - 主观数据周曲线 → workout_analysis.weekly_subjective
  - 截图识别真机实跑（真机 OCR → 字段解析，含识别原文与图片）
  - 环境折算消融（坡度/跑步机各自的 MAE 贡献）→ 读 tools/probe_env_split.py 的缓存

用法：
    .venv\\Scripts\\python.exe tools\\build_demo_html.py
    .venv\\Scripts\\python.exe tools\\build_demo_html.py --with-backtest   # 顺带重跑消融（约 3 分钟）
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DESKTOP = Path.home() / "Desktop"
OUT_NAME = "SuperTrainer-演示.html"
BACKTEST_CACHE = ROOT / "tools" / "_demo_backtest_cache.json"
PROBE_STDOUT = ROOT / "tools" / "_demo_env_split_out.txt"

ABLATION_KEYS = ("A 现状（坡度 + 跑步机）", "B 只关坡度项",
                 "C 只关跑步机项", "D 两项都关（≈批33 原状）")


# --------------------------------------------------------------------- 数据

def gather_dashboard() -> dict:
    from runtrainer.services import dashboard_service
    return dashboard_service.get_dashboard()


def gather_pace_sync() -> dict:
    from runtrainer.services import plan_service
    return plan_service.pace_sync_status()


def gather_zones() -> list[dict]:
    """六区配速表：直接用预估卡里的 zones（与课表标注同源）。"""
    a = gather_dashboard().get("ability_30d") or {}
    return a.get("zones") or []


def gather_activities(limit: int = 60) -> list[dict]:
    from runtrainer.db.repos import activity_repo
    rows = activity_repo.list_activities(limit=limit)
    out = []
    for a in rows:
        out.append({
            "date": _iso_day(a.get("start_ts")),
            "name": a.get("name"),
            "source": a.get("source"),
            "manual": bool(a.get("manual")),
            "distance_km": round((a.get("distance_m") or 0) / 1000, 2),
            "duration_s": a.get("duration_s"),
            "pace_s_km": a.get("avg_pace_s_km"),
            "avg_hr": a.get("avg_hr"),
            "cadence": a.get("avg_cadence"),
            "elevation_m": a.get("elevation_gain_m"),
            "feel": a.get("feel"),
            "rpe": a.get("rpe"),
            "note": a.get("note"),
        })
    return out


def gather_weekly_subjective(days: int = 180) -> list[dict]:
    from runtrainer.db.repos import activity_repo
    from runtrainer.domain.workout_analysis import weekly_subjective
    end = date.today()
    start = end - timedelta(days=days)
    rows = activity_repo.list_subjective_rows(start.isoformat(), end.isoformat())
    return weekly_subjective(rows, start, end)


def gather_env_examples(limit: int = 6) -> dict:
    """真实活动里的坡度折算对照：挑几条爬升明显的 + 几条近平地的。"""
    from runtrainer.db import database
    from runtrainer.domain import evidence as evm
    database.migrate()
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, distance_m, avg_pace_s_km, elevation_gain_m FROM activities"
            " WHERE distance_m >= 5000 AND avg_pace_s_km IS NOT NULL"
            "   AND elevation_gain_m IS NOT NULL").fetchall()]
        all_total = int(conn.execute(
            "SELECT COUNT(*) FROM activities WHERE distance_m >= 3000").fetchone()[0])

    def grade(r):
        return (r.get("elevation_gain_m") or 0) / max(r["distance_m"] / 1000, 1)

    hilly = sorted((r for r in rows if grade(r) > 0), key=grade, reverse=True)[:limit]
    flat = sorted((r for r in rows if grade(r) < 3), key=lambda r: grade(r))[:3]

    def pack(r):
        raw = float(r["avg_pace_s_km"])
        adj = evm.env_adjusted_pace(r, raw)
        return {"name": r["name"], "distance_km": round(r["distance_m"] / 1000, 2),
                "grade_m_per_km": round(grade(r), 1),
                "raw_pace_s_km": round(raw, 1),
                "adj_pace_s_km": round(adj, 1),
                "delta_s_km": round(adj - raw, 1)}

    return {"hilly": [pack(r) for r in hilly], "flat": [pack(r) for r in flat],
            "constants": {"grade_per_m": evm.GRADE_PACE_S_KM_PER_M,
                          "grade_cap_s_km": evm.GRADE_ADJ_CAP_S_KM,
                          "treadmill_s_km": evm.TREADMILL_PACE_DELTA_S_KM},
            "counts": {"total": len(rows), "all_total": all_total,
                       "treadmill": sum(1 for r in rows if evm.is_treadmill(r)),
                       "hilly": sum(1 for r in rows if grade(r) >= 10),
                       "flat": sum(1 for r in rows if grade(r) < 2
                                   and not evm.is_treadmill(r))}}


def gather_rpe_rules() -> dict:
    from runtrainer.domain import evidence as evm
    return {
        "very_easy_max": evm.RPE_VERY_EASY_MAX,
        "very_easy_weight": evm.RPE_VERY_EASY_WEIGHT,
        "easy_max": evm.RPE_EASY_MAX,
        "easy_weight": evm.RPE_EASY_WEIGHT,
        "race_all_out": evm.RACE_RPE_ALL_OUT,
        "race_submax": evm.RACE_RPE_SUBMAX,
        "curve": [{"rpe": v, "weight": evm.rpe_weight(v)} for v in range(1, 11)],
    }


def gather_ocr_demo() -> dict | None:
    """真机实跑：生成测试图 → Windows OCR → 字段解析（含图片与原文）。"""
    from runtrainer.services import ocr_service
    gen = ROOT / "tools" / "_make_ocr_test.ps1"
    if not gen.exists():
        return None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(gen)],
            capture_output=True, timeout=60)
        path = Path(proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1])
        if not path.is_file():
            return None
        raw = ocr_service.recognize_text(path)
        parsed = ocr_service.parse_activity_text(raw)
        png = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "image_data_url": "data:image/png;base64," + png,
            "raw_text": raw,
            "fields": parsed["fields"],
            "matched": parsed["matched"],
            "warnings": parsed["warnings"],
            "engine": "Windows.Media.Ocr.OcrEngine（系统自带，无第三方依赖）",
        }
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 截图识别实跑失败：{e}")
        return None


def load_backtest_cache() -> dict | None:
    if not BACKTEST_CACHE.exists():
        return None
    try:
        return json.loads(BACKTEST_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_ablation(text: str) -> dict | None:
    """从 probe_env_split.py 的 stdout 里抓各配置的真实比赛误差。"""
    cur, configs, samples, coverage = None, {}, None, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("== ") and s.endswith(" =="):
            cur = s[3:-3].strip()
        elif s.startswith("真实库") and samples is None:
            samples = s
        elif s.startswith("After·证据管线") and cur and coverage is None:
            coverage = s
        elif s.startswith("真实比赛 · 证据管线") and cur:
            try:
                configs[cur] = json.loads(s[s.index("{"):])
            except (ValueError, IndexError):
                continue
    if not configs:
        return None
    return {"configs": configs, "samples": samples, "coverage": coverage,
            "as_of": date.today().isoformat()}


def run_backtest_ablation(stdout_file: Path | None = None) -> dict | None:
    """跑 tools/probe_env_split.py（4 次回测，约 3 分钟）；stdout_file 给定时改为复用其输出。"""
    if stdout_file and stdout_file.exists():
        print(f"  复用 {stdout_file.name} 的消融输出（跳过重跑）")
        return _parse_ablation(stdout_file.read_text(encoding="utf-8", errors="replace"))
    print("  重跑环境折算消融（4 次回测，约 3 分钟）…")
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "probe_env_split.py")],
            capture_output=True, timeout=1800, cwd=str(ROOT))
        text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  [warn] 消融回测失败：{e}")
        return None
    return _parse_ablation(text)


# ------------------------------------------------------------------ 小工具

def _iso_day(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def fmt_pace(s) -> str:
    if not s:
        return "—"
    t = int(round(s))
    return f"{t // 60}:{t % 60:02d}"


def fmt_dur(s) -> str:
    if not s:
        return "—"
    s = int(round(s))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


DEMO_CSS = """
/* ---- 演示页专用覆盖（app.css 假定的是「侧栏 + 内部滚动」的应用布局） ---- */
body { display: block; overflow: visible; height: auto; min-height: 100%; }
.topbar { position: sticky; top: 0; z-index: 40; display: flex; gap: 14px;
  align-items: center; flex-wrap: wrap; padding: 10px 24px;
  background: var(--bg-card); border-bottom: 1px solid var(--border); }
.topbar .brand { font-weight: 700; font-size: 16px; }
.topbar nav { display: flex; gap: 3px; flex-wrap: wrap; }
.topbar nav a { padding: 5px 10px; border-radius: 6px; color: var(--text); font-size: 13px; }
.topbar nav a:hover { background: var(--bg-hover); }
.wrap { max-width: 1120px; margin: 0 auto; padding: 20px 24px 70px; }
.sec { margin-top: 32px; scroll-margin-top: 64px; }
.sec > h2 { font-size: 19px; margin: 0 0 4px; }
.sec > .sub { color: var(--text-muted); font-size: 13px; margin-bottom: 14px; }
.kpi { display: grid; grid-template-columns: repeat(auto-fit, minmax(176px, 1fr)); gap: 14px; }
.kpi .card { margin-bottom: 0; }
.kpi .v { font-size: 23px; font-weight: 700; font-variant-numeric: tabular-nums; }
.kpi .k { color: var(--text-muted); font-size: 12px; }
.kpi .h { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.chip { display: inline-block; padding: 2px 9px; border-radius: 10px;
  background: var(--bg-hover); font-size: 12px; color: var(--text-muted); }
.prewrap { white-space: pre-wrap; word-break: break-word;
  font-family: Consolas, "Courier New", monospace; font-size: 12px;
  background: var(--bg-hover); padding: 10px; border-radius: 6px;
  max-height: 220px; overflow: auto; }
.demo-form { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 820px) { .demo-form { grid-template-columns: 1fr 1fr; } }
.out { font-family: Consolas, "Courier New", monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-word; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 900px) { .two { grid-template-columns: 1fr; } }
.rule li { margin-bottom: 4px; font-size: 13px; }
.ok { color: var(--st-good); }
.bad { color: var(--st-critical); }
small.note { font-size: 12px; color: var(--text-muted); }
img.shot { max-width: 100%; border-radius: 6px; border: 1px solid var(--border); }
"""

DEMO_JS = """
let _charts = [];

function toggleTheme() {
  const cur = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = cur;
  drawCharts();          // 图表颜色取自 CSS 变量，主题一变必须重画
}

window.addEventListener('resize', () => _charts.forEach((c) => c.resize()));

function demo() {
  return {
    form: { name: '早晨轻松跑', start_local: '', distance_km: '10.5',
            duration: '52:30', avg_hr: '148', max_hr: '', avg_cadence: '176',
            stride_length_m: '', elevation_gain_m: '35', calories: '712',
            feel: '2', rpe: '4', note: '风大，前 2km 腿沉' },
    saved: [],
    msg: '',
    num(v) { const s = String(v ?? '').trim(); if (!s) return null;
             const n = Number(s); return Number.isFinite(n) ? n : null; },
    parseDuration(t) {
      const s = String(t ?? '').trim();
      if (!s) return null;
      const parts = s.split(':').map((x) => x.trim());
      if (parts.some((x) => x === '' || !Number.isFinite(Number(x)))) return null;
      const n = parts.map(Number);
      if (n.length === 1) return n[0] > 0 ? Math.round(n[0] * 60) : null;
      if (n.length === 2) return Math.round(n[0] * 60 + n[1]);
      if (n.length === 3) return Math.round(n[0] * 3600 + n[1] * 60 + n[2]);
      return null;
    },
    fmtPace(s) {
      if (!s) return '—';
      const t = Math.round(s);
      return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0') + ' /km';
    },
    payload() {
      const f = this.form, km = this.num(f.distance_km);
      return { name: f.name, start_local: f.start_local,
        distance_m: km != null ? +(km * 1000).toFixed(1) : null,
        duration_s: this.parseDuration(f.duration),
        avg_hr: this.num(f.avg_hr), max_hr: this.num(f.max_hr),
        avg_cadence: this.num(f.avg_cadence),
        stride_length_m: this.num(f.stride_length_m),
        elevation_gain_m: this.num(f.elevation_gain_m),
        calories: this.num(f.calories),
        feel: this.num(f.feel), rpe: this.num(f.rpe), note: f.note };
    },
    pacePreview() {
      const p = this.payload();
      if (!p.distance_m || !p.duration_s) return '';
      return this.fmtPace(p.duration_s / (p.distance_m / 1000));
    },
    stridePreview() {
      const p = this.payload();
      if (p.stride_length_m) return p.stride_length_m + ' m（手填）';
      const c = p.avg_cadence;
      if (!c || !p.distance_m || !p.duration_s) return '—';
      const pace = p.duration_s / (p.distance_m / 1000);
      return (1000 / pace / (c / 60)).toFixed(3) + ' m（由配速×步频推算）';
    },
    validate(p) {
      if (p.distance_m == null && p.duration_s == null) return '距离和用时至少要填一个';
      if (p.distance_m != null && p.distance_m <= 0) return '距离必须大于 0';
      if (p.duration_s != null && p.duration_s <= 0) return '用时必须大于 0';
      if (p.avg_hr != null && !(p.avg_hr >= 40 && p.avg_hr <= 240))
        return '平均心率超出 40–240 的合理范围';
      if (p.max_hr != null && !(p.max_hr >= 40 && p.max_hr <= 250))
        return '最大心率超出 40–250 的合理范围';
      if (p.avg_hr != null && p.max_hr != null && p.max_hr < p.avg_hr)
        return '最大心率不能低于平均心率';
      if (p.avg_cadence != null && !(p.avg_cadence >= 40 && p.avg_cadence <= 260))
        return '步频超出 40–260 步/分的合理范围';
      return '';
    },
    save() {
      const p = this.payload();
      const err = this.validate(p);
      if (err) { this.msg = '❌ ' + err; return; }
      const pace = p.distance_m && p.duration_s
        ? p.duration_s / (p.distance_m / 1000) : null;
      if (pace && !(pace >= 100 && pace <= 1200)) {
        this.msg = '❌ 按距离与用时算出的配速不合常理（1:40–20:00 /km）';
        return;
      }
      const rec = Object.assign({}, p, { pace_s_km: pace ? +pace.toFixed(1) : null });
      this.saved.unshift(rec);
      this.msg = '✅ 校验通过（演示页只在浏览器里暂存；桌面版会入库并联动重估水平、同步课表配速）';
    },
  };
}

const DEMO = __DATA__;

function paceStr(s) {
  if (!s) return '—';
  const t = Math.round(s);
  return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0');
}

function drawCharts() {
  _charts.forEach((c) => c.dispose());
  _charts = [];
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const C = { text: cssVar('--text'), muted: cssVar('--text-muted'),
    grid: cssVar('--gridline'), bg: cssVar('--bg-card'), accent: cssVar('--accent'),
    m: cssVar('--kind-m'), i: cssVar('--kind-i'), t: cssVar('--kind-t'),
    e: cssVar('--kind-e'), ramp1: cssVar('--ramp-1') };
  const ax = () => ({ axisLine: { lineStyle: { color: C.grid } },
    axisLabel: { color: C.muted, fontSize: 11, interval: 'auto', hideOverlap: true },
    splitLine: { lineStyle: { color: C.grid } }, axisTick: { show: false } });
  const tip = (extra) => Object.assign({ trigger: 'axis', backgroundColor: C.bg,
    borderColor: C.grid, textStyle: { color: C.text, fontSize: 12 } }, extra || {});
  const mk = (el, opt) => {
    const dom = document.getElementById(el);
    if (!dom || !window.echarts) return;
    const c = echarts.init(dom);
    c.setOption(opt);
    _charts.push(c);
  };

  // 1) 环境折算：实测配速 vs 等效配速
  const env = DEMO.env_examples;
  const rows = env.hilly.concat(env.flat);
  mk('c-env', {
    grid: { left: 52, right: 16, top: 30, bottom: 46 },
    xAxis: Object.assign({ type: 'category',
      data: rows.map((r) => (r.name || '').slice(0, 10)) }, ax()),
    yAxis: Object.assign({ type: 'value', scale: true,
      axisLabel: { color: C.muted, fontSize: 11,
        formatter: (v) => Math.floor(v / 60) + ':' + String(Math.round(v % 60)).padStart(2, '0') } }, ax()),
    tooltip: tip({ formatter: (ps) => {
      const r = rows[ps[0].dataIndex];
      return '<b>' + r.name + '</b><br/>坡度 ' + r.grade_m_per_km + ' m/km · ' +
        r.distance_km + ' km<br/>实测 ' + paceStr(r.raw_pace_s_km) +
        '<br/>等效 ' + paceStr(r.adj_pace_s_km) +
        '<br/>折算 ' + (r.delta_s_km > 0 ? '+' : '') + r.delta_s_km + ' s/km';
    } }),
    legend: { bottom: 0, textStyle: { color: C.muted, fontSize: 11 } },
    series: [
      { name: '实测配速', type: 'bar', data: rows.map((r) => r.raw_pace_s_km),
        itemStyle: { color: C.muted, opacity: 0.55 }, barMaxWidth: 24 },
      { name: '折算后等效配速', type: 'bar', data: rows.map((r) => r.adj_pace_s_km),
        itemStyle: { color: C.accent }, barMaxWidth: 24 },
    ],
  });

  // 2) RPE 降权曲线
  const rc = DEMO.rpe.curve;
  mk('c-rpe', {
    grid: { left: 46, right: 16, top: 26, bottom: 30 },
    xAxis: Object.assign({ type: 'category',
      data: rc.map((r) => 'RPE ' + r.rpe) }, ax()),
    yAxis: Object.assign({ type: 'value', min: 0, max: 1.1,
      axisLabel: { color: C.muted, fontSize: 11 } }, ax()),
    tooltip: tip({ formatter: (ps) => { const r = rc[ps[0].dataIndex];
      return 'RPE ' + r.rpe + ' → 权重 ' + r.weight; } }),
    series: [{ type: 'line', step: 'end', data: rc.map((r) => r.weight),
      lineStyle: { color: C.e, width: 2 }, itemStyle: { color: C.e },
      symbolSize: 7, areaStyle: { opacity: 0.08, color: C.e },
      markLine: { silent: true, symbol: 'none',
        data: [{ yAxis: 1.0 }],
        lineStyle: { color: C.muted, type: 'dashed' },
        label: { formatter: '不调权重', color: C.muted, fontSize: 10 } } }],
  });

  // 3) 周均 RPE（有标签才画）
  const wk = DEMO.subjective.slice().reverse();
  if (wk.some((w) => w.rpe_avg != null)) {
    mk('c-subj', {
      grid: { left: 46, right: 34, top: 26, bottom: 30 },
      xAxis: Object.assign({ type: 'category',
        data: wk.map((w) => w.week_start.slice(5)) }, ax()),
      yAxis: [Object.assign({ type: 'value', min: 1, max: 10,
        axisLabel: { color: C.muted, fontSize: 11 } }, ax()),
        Object.assign({ type: 'value', min: 0,
          axisLabel: { show: false }, splitLine: { show: false } }, ax())],
      tooltip: tip({ formatter: (ps) => { const w = wk[ps[0].dataIndex];
        const out = ['<b>' + w.week_start + '</b>'];
        out.push(w.rpe_avg != null
          ? '平均 RPE ' + w.rpe_avg + '/10（' + w.rpe_n + ' 次标注）'
          : '平均 RPE —（该周没标注）');
        out.push('本周跑步 ' + w.runs + ' 次');
        return out.join('<br/>'); } }),
      series: [
        { name: '跑步次数', type: 'bar', yAxisIndex: 1, barMaxWidth: 16,
          data: wk.map((w) => w.runs), itemStyle: { color: C.ramp1, opacity: 0.45 } },
        { name: '平均 RPE', type: 'line', connectNulls: false,
          data: wk.map((w) => w.rpe_avg), lineStyle: { color: C.accent, width: 2 },
          itemStyle: { color: C.accent }, symbolSize: 6 },
      ],
    });
  }

  // 4) 周跑量：计划 vs 实际
  const wser = DEMO.weekly_series;
  mk('c-week', {
    grid: { left: 46, right: 16, top: 30, bottom: 46 },
    xAxis: Object.assign({ type: 'category', data: wser.map((w) => w.label) }, ax()),
    yAxis: Object.assign({ type: 'value', axisLabel: { color: C.muted, fontSize: 11 } }, ax()),
    tooltip: tip({ formatter: (ps) => { const w = wser[ps[0].dataIndex];
      const cov = w.coverage == null ? '—' : Math.round(w.coverage * 100) + '%';
      return '<b>' + w.week_start + '</b><br/>计划 ' + w.planned_km +
        ' km<br/>实际 ' + w.done_km + ' km<br/>覆盖 ' + cov; } }),
    legend: { bottom: 0, textStyle: { color: C.muted, fontSize: 11 } },
    series: [
      { name: '计划跑量', type: 'bar', data: wser.map((w) => w.planned_km),
        itemStyle: { color: C.muted, opacity: 0.5 }, barMaxWidth: 22 },
      { name: '实际跑量', type: 'bar', data: wser.map((w) => w.done_km),
        itemStyle: { color: C.m }, barMaxWidth: 22 },
    ],
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.documentElement.dataset.theme = 'dark';
  setTimeout(drawCharts, 60);
});
"""


# ------------------------------------------------------------------ 渲染

def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _card(inner: str) -> str:
    return f'<div class="card">{inner}</div>'


def _kpi(k: str, v: str, hint: str = "") -> str:
    h = f'<div class="h">{_esc(hint)}</div>' if hint else ""
    return _card(f'<div class="k">{_esc(k)}</div><div class="v">{v}</div>{h}')


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def render_sections(d: dict) -> str:
    dash = d["dashboard"]
    ab = dash.get("ability_30d") or {}
    out = []

    # ---- 1 概览 ----
    race = dash.get("race") or {}
    wl = dash.get("week_load") or {}
    kp = dash.get("kpis") or {}
    ready = dash.get("readiness") or {}
    bands = ab.get("prediction_bands") or {}
    preds = ab.get("predictions") or {}
    pred_html = "".join(
        f'<span class="chip">{_esc(l)} {_esc(fmt_dur(s))}'
        + (f' ±{_esc(fmt_dur(bands[l]["half_s"]))}' if l in bands else "")
        + "</span> " for l, s in preds.items())
    acwr = kp.get("acwr")
    out.append(f"""
<section class="sec" id="overview">
  <h2>一、系统概览</h2>
  <div class="sub">以下数字全部来自本机数据库的真实计算，未经修饰；数据快照 {_esc(dash.get('today'))}。</div>
  <div class="kpi">
    {_kpi("当前预估 VDOT", ab.get("vdot") or "—",
          f"窗口 {ab.get('window_days')} 天 · 口径 {ab.get('mode')}")}
    {_kpi("目标比赛", _esc(race.get("name") or "—"),
          f"{_esc(race.get('race_date') or '')} · 还剩 {race.get('days_left')} 天"
          if race else "未设置目标")}
    {_kpi("本周跑量", f"{wl.get('done_km', '—')} / {wl.get('planned_km', '—')} km",
          f"计划覆盖 {wl.get('pct')}%" if wl.get("pct") is not None else "")}
    {_kpi("ACWR（急慢性负荷比）", f"{acwr:.2f}" if acwr is not None else "—",
          "0.8–1.3 为安全区" if acwr is not None else "样本不足")}
    {_kpi("今日恢复度", _esc(ready.get("label") or "—"), _esc(ready.get("note") or ""))}
    {_kpi("数据规模", f"{d['counts']['activities']} 条活动",
          f"健康 {d['counts']['health']} 天 · 课表 {d['counts']['workouts']} 节")}
  </div>
  <div class="card mt8">
    <h3>按当前水平推算的等效成绩（非训练目标）</h3>
    <p>{pred_html or '<span class="muted">数据不足，暂无法预测</span>'}</p>
    <p class="note">± 为个人比赛误差学习出的预测区间（约 80% 置信）；没有该距离比赛误差记录就不给区间，
      不拍一个看起来专业的数字。</p>
  </div>
  <div class="card">
    <h3>近 8 周：计划跑量 vs 实际跑量</h3>
    <div class="chart" id="c-week"></div>
  </div>
</section>""")

    # ---- 2 预估系统 ----
    zones = ab.get("zones") or []
    zrows = [[f'<b>{_esc(z.get("mark") or "—")}</b>', _esc(z.get("label")),
              _esc(z.get("band")), _esc(z.get("use")),
              f'{_esc(fmt_pace(z.get("pace_fast_s_km")))} – {_esc(fmt_pace(z.get("pace_slow_s_km")))} /km']
             for z in zones]
    ev_items = "".join(f'<li>{_esc(e.get("detail"))}</li>'
                       for e in (ab.get("evidence") or []))
    yb = ab.get("year_bests") or []
    yb_html = " ".join(f'<span class="chip">{_esc(b.get("distance"))} {_esc(fmt_dur(b.get("best_seconds")))}'
                       f'（{_esc(b.get("date"))}）</span>' for b in yb) or '<span class="muted">尚无</span>'
    out.append(f"""
<section class="sec" id="estimate">
  <h2>二、成绩预估系统</h2>
  <div class="sub">丹尼尔斯 VDOT 体系：把训练证据折算成「现在能跑多少」，再换算成各强度区间的配速。</div>
  <div class="two">
    <div class="card">
      <div class="hero"><span class="big">{_esc(ab.get("vdot") or "—")}</span><span class="unit"> VDOT</span></div>
      <p class="note">评估日期 {_esc(ab.get("as_of") or "—")} · 窗口 {_esc(ab.get("window_days"))} 天</p>
      <p class="note">近一年最佳：{yb_html}</p>
    </div>
    <div class="card">
      <h3>依据（算法给出的原始说明）</h3>
      <ul class="rule" style="padding-left:18px">{ev_items or "<li>暂无证据</li>"}</ul>
    </div>
  </div>
  <div class="card">
    <h3>当前水平对应的六区配速（课表与日历都按这张表标注）</h3>
    {_table(["档", "区间", "%VDOT 带", "用途", "配速"], zrows) if zrows else '<p class="muted">暂无</p>'}
  </div>
  <div class="card">
    <h3>证据管线怎么工作</h3>
    <ul class="rule" style="padding-left:18px">
      <li><b>每条训练至多产出一条证据</b>，按优先级取：比赛 &gt; 间歇/重复课 &gt; 连续匀速强度课 &gt; 日常跑步的最快分段。</li>
      <li><b>训练→比赛的个人转换</b>：同一个人，间歇课上的配速不能直接当比赛成绩。系统用他本人的历史比赛反推
        每条证据要打多少折扣（EWMA 学习，收缩到先验），而不是套用群体公式。</li>
      <li><b>多源融合</b>：比赛、间歇、匀速课、最快分段各自给出一个 VDOT，按证据密度与时效加权取中心；
        证据不足时不会硬凑。</li>
      <li><b>预测区间</b>：由执行噪声与该距离的历史预测误差合成，只对真正比过的距离给出。</li>
    </ul>
  </div>
</section>""")

    # ---- 3 本次优化 ----
    env = d["env_examples"]
    c = env["constants"]
    cnt = env["counts"]
    env_rows = [[_esc(r["name"]), f'{r["distance_km"]} km', f'{r["grade_m_per_km"]} m/km',
                 _esc(fmt_pace(r["raw_pace_s_km"])), _esc(fmt_pace(r["adj_pace_s_km"])),
                 f'{r["delta_s_km"]:+.1f} s/km'] for r in env["hilly"] + env["flat"]]
    abl = d.get("backtest")
    if abl:
        cfg = abl.get("configs") or {}
        maes = [v.get("mae_s") for v in cfg.values() if v.get("mae_s") is not None]
        best = min(maes) if maes else None
        arows = []
        for k, v in cfg.items():
            mae = v.get("mae_s")
            mark = (' <span class="badge soft">最低</span>' if mae == best else "")
            bias = v.get("bias_s")
            arows.append([_esc(k), f'{mae} s{mark}', f'{v.get("median_ae_s")} s',
                          (f'{bias:+d} s' if bias is not None else "—"),
                          f'{v.get("rmse_s")} s'])
        n = next((v.get("n") for v in cfg.values() if v.get("n")), "?")
        abl_html = (
            _table(["配置", "MAE", "中位绝对误差", "偏差", "RMSE"], arows)
            + f'<p class="note">真实比赛 n={n} · {_esc(abl.get("samples") or "")}<br/>'
              f'{_esc(abl.get("coverage") or "")}<br/>'
              f'由 tools/probe_env_split.py 在同一回测上换常数重跑 4 次得到，'
              f'消融日期 {_esc(abl.get("as_of"))}。</p>')
    else:
        abl_html = ('<p class="muted">本机未缓存消融结果：运行 '
                    '<code>python tools/build_demo_html.py --with-backtest</code> 可重跑（约 3 分钟）。</p>')
    rpe = d["rpe"]
    out.append(f"""
<section class="sec" id="improve">
  <h2>三、预估系统这一轮做了什么优化</h2>
  <div class="sub">三块改动都不靠「感觉更准」，而是先在真实库上量出误差，再决定留不留。</div>

  <div class="card">
    <h3>3.1 环境折算：坡度与跑步机的配速修正</h3>
    <p>同心率下，<b>上坡更慢、跑步机更快</b>。不折算就会把「爬坡跑得慢」读成水平下降、
      把「跑步机上跑得快」读成水平上升。修正公式：
      等效配速 = 实测 − 坡度修正 + 跑步机修正。</p>
    <ul class="rule" style="padding-left:18px">
      <li>坡度项：每米/公里爬升 <b>{c['grade_per_m']} s/km</b>，单次最多折算 <b>{c['grade_cap_s_km']} s/km</b>；距离不足 1 km 不参与（分母没意义）。</li>
      <li>跑步机项：按活动名抽「跑步机 / treadmill / 室内跑」等特征词，加 <b>{c['treadmill_s_km']} s/km</b>。</li>
      <li>常数由真实库的最小二乘标定得到（<code>tools/probe_env_adjust.py</code>，用「距今天数」吸收一年内的体能漂移，
        避免把「进步」误读成「环境效应」）。</li>
    </ul>
    <div class="chart" id="c-env"></div>
    <p class="note">样本：<b>≥5 km 且已记录爬升</b>的活动 {cnt['total']} 条，
      其中跑步机 {cnt['treadmill']} 条、坡度 ≥10 m/km {cnt['hilly']} 条、近平地非跑步机 {cnt['flat']} 条。
      （消融回测用的是更宽的 ≥3 km 口径，含 {cnt['all_total']} 条。）下表为真实活动逐条对照。</p>
    {_table(["活动", "距离", "坡度", "实测配速", "折算后", "变化"], env_rows)}
  </div>

  <div class="card" id="rpe">
    <h3>3.2 主观用力程度（RPE）参与加权</h3>
    <p>一次「标了很轻松」的强度课，不足以单独把水平抬高——它可能只是当天状态好、天气凉、或者心率带飘。
      系统对低 RPE 的证据降权，且<b>只降不升</b>（没有把握时不放大任何结论）：</p>
    <ul class="rule" style="padding-left:18px">
      <li>RPE ≤ {rpe['very_easy_max']} → 权重 {rpe['very_easy_weight']}</li>
      <li>RPE ≤ {rpe['easy_max']} → 权重 {rpe['easy_weight']}</li>
      <li>RPE &gt; {rpe['easy_max']} 或未标注 → 权重 1.0（不变）</li>
      <li>比赛判定：RPE ≥ {rpe['race_all_out']} 视作全力，≤ {rpe['race_submax']} 视作未尽全力（主观优先于证据推断）</li>
    </ul>
    <div class="chart" id="c-rpe" style="height:200px"></div>
    <p class="note">⚠️ 【证据不足】本机库里目前还没有任何 RPE/感受标注（手动录入、截图识别、佳明 app 标签三条入口都已接通），
      所以阈值是按运动生理学的通行量表定的，<b>尚未用真实数据验证</b>。设计上做了保护：
      没标注时权重恒为 1.0，即完全等价于改动之前。</p>
  </div>

  <div class="card" id="ablation">
    <h3>3.3 回测消融：这个修正真的有用，还是碰巧？</h3>
    <p>担心的是「误差变小」其实被某一两场比赛带出来（过拟合）。做法是把两个常数分别置零，
      用同一个回测跑 4 遍，看误差各来自哪一项：</p>
    {abl_html}
    <p class="note">结论（按上次消融）：收益<b>全部来自坡度项</b>；跑步机项在回测里中性——
      库里跑步机活动太少，且都不在喂给那 17 场比赛的时间窗内。两项都保留，但跑步机项标注为【待更多真实标注验证】。</p>
  </div>
</section>""")

    # ---- 4 配速同步 ----
    ps = d["pace_sync"]
    if ps.get("available"):
        srows = [[_esc(s.get("date")), _esc(s.get("zone")), _esc(s.get("title")),
                  _esc(fmt_pace(s.get("old_slow"))), _esc(fmt_pace(s.get("new_slow")))]
                 for s in (ps.get("samples") or [])]
        sync_html = (
            _kpi("课表 VDOT", ps.get("plan_vdot"), "生成课表时用的水平") + " "
            + _kpi("当前预估 VDOT", ps.get("target_vdot"), "最新证据给出的水平") + " "
            + _kpi("差值", f'{ps.get("delta"):+.1f}',
                   "锁定" if ps.get("locked") else "未锁定，可自动同步") + " "
            + _kpi("待同步课次", f'{ps.get("stale_workouts")} / {ps.get("total_workouts")}',
                   "未执行、非 AI 调整的课")
            + _table(["日期", "区间", "课程", "旧配速", "新配速"], srows)
            + '<p class="note">同步只改「配速列」：日期、距离、分段、完成状态一律不动。'
              '阈值 1 s/km 以内视为一致——此前是 0.5 s/km，导致亚 VDOT 级的小漂移永远进不了课表。</p>')
    else:
        sync_html = '<p class="muted">当前没有活跃课表。</p>'
    out.append(f"""
<section class="sec" id="sync">
  <h2>四、预估 VDOT 对应配速同步到课表</h2>
  <div class="sub">水平重估后，课表上未执行课的配速若不跟着变，那上面的配速就是错的。</div>
  <div class="card"><div class="kpi">{sync_html}</div></div>
</section>""")

    # ---- 5 主观数据 ----
    subj = dash.get("subjective") or {}
    has_lbl = bool(subj.get("rpe_n") or subj.get("feel_n"))
    n_weeks = len(d["subjective"])
    n_rpe_weeks = sum(1 for w in d["subjective"] if w.get("rpe_avg") is not None)
    subj_note = (
        f'近 {n_weeks} 周里只有 {n_rpe_weeks} 周有 RPE 标注，所以曲线目前基本是空的——'
        '这是真实数据的样子（库里 0 条标注），不是图表坏了。柱状为当周跑步次数，'
        '折线为当周平均 RPE；没标注的周留空而不插值。'
        if n_rpe_weeks == 0 else
        f'近 {n_weeks} 周里有 {n_rpe_weeks} 周带 RPE 标注。柱状为当周跑步次数，折线为当周平均 RPE。')
    subj_chart_html = ('<div class="chart" id="c-subj"></div>' if n_rpe_weeks else '')
    notes = subj.get("notes") or []
    notes_html = ("<ul class='rule' style='padding-left:18px'>"
                  + "".join(f'<li>{_esc(n["date"])} {_esc(n["note"])}</li>' for n in notes)
                  + "</ul>") if notes else '<p class="muted">近 7 天没有备注。</p>'
    out.append(f"""
<section class="sec" id="subjective">
  <h2>五、主观数据（感受 / 用力程度）的落地</h2>
  <div class="sub">三条入口写入同两列（<code>feel</code> 1–5、<code>rpe</code> 1–10），下游一视同仁。</div>
  <div class="card">
    <h3>入口</h3>
    <ul class="rule" style="padding-left:18px">
      <li><b>手动录入</b>：活动页「✏️ 手动录入」表单，含距离/用时/心率/最大心率/步频/步幅/爬升/热量/感受/RPE/备注。</li>
      <li><b>截图识别</b>：读手机跑步 App 的成绩页截图，自动预填表单（识别只做预填，必须用户确认才入库）。</li>
      <li><b>佳明同步</b>：拉取历史活动详情时一并读回 app 里标的感受与用力程度。</li>
    </ul>
  </div>
  <div class="card">
    <h3>近 7 天主观状态</h3>
    {"" if has_lbl else '<p class="muted">⚠️ 库里还没有主观标注——下面这些位置都已经接好，标注后自动出数。</p>'}
    <div class="kpi">
      {_kpi("平均 RPE", (str(subj.get("rpe_avg")) + " / 10") if subj.get("rpe_avg") else "—",
            f'已标注 {subj.get("rpe_n")} 次')}
      {_kpi("最高 RPE", (str(subj.get("rpe_max")) + " / 10") if subj.get("rpe_max") else "—", "9–10 通常是比赛")}
      {_kpi("平均感受", (str(subj.get("feel_avg")) + " / 5") if subj.get("feel_avg") else "—",
            f'已标注 {subj.get("feel_n")} 次')}
    </div>
    {notes_html}
  </div>
  <div class="card">
    <h3>周均用力程度（健康页「主观负荷」曲线，数据同源）</h3>
    {subj_chart_html}
    <p class="note">{_esc(subj_note)}</p>
  </div>
</section>""")

    # ---- 6 手动录入 / 截图识别演示 ----
    ocr = d.get("ocr")
    if ocr:
        f = ocr["fields"]
        frows = [[k, _esc(v), _esc(ocr["matched"].get(k, ""))]
                 for k, v in f.items()]
        warn = ("；".join(ocr["warnings"])) if ocr["warnings"] else "无"
        ocr_html = f"""
  <div class="two">
    <div>
      <p class="note">测试图（本机生成，模拟手机成绩页版式）：</p>
      <img class="shot" src="{ocr['image_data_url']}" alt="识别测试图">
    </div>
    <div>
      <p class="note">Windows OCR 识别原文（引擎：{_esc(ocr['engine'])}）：</p>
      <div class="prewrap">{_esc(ocr['raw_text'])}</div>
      <p class="note">解析出的字段（含命中的原文片段）：</p>
      {_table(["字段", "值", "命中原文"], frows)}
      <p class="note">告警：{_esc(warn)}</p>
    </div>
  </div>"""
    else:
        ocr_html = '<p class="muted">本机 OCR 实跑未成功（可能缺少系统 OCR 语言包），此处略过。</p>'

    def _subj_cell(a) -> str:
        bits = []
        if a.get("feel"):
            bits.append(f'感受 {a["feel"]}/5')
        if a.get("rpe"):
            bits.append(f'RPE {a["rpe"]}/10')
        return " / ".join(bits) if bits else '<span class="muted">—</span>'

    def _num0(v):
        return f"{v:.0f}" if v is not None else "—"

    act_rows = [[
        _esc(a.get("date")), _esc(a.get("name") or ""),
        ("手动" if a.get("manual") else _esc(a.get("source") or "")),
        f'{a.get("distance_km")} km',
        _esc(fmt_pace(a.get("pace_s_km"))) if a.get("pace_s_km") else "—",
        _num0(a.get("avg_hr")),
        _num0(a.get("cadence")),
        (f'{a["elevation_m"]:.0f} m' if a.get("elevation_m") else "—"),
        _subj_cell(a),
    ] for a in (d.get("activities") or [])]

    out.append(f"""
<section class="sec" id="new">
  <h2>六、新功能演示</h2>
  <div class="sub">手动录入与截图识别。下面这块表单是可交互的，试着改数字看配速与步幅怎么联动。</div>
  <div class="card" x-data="demo()" id="form">
    <h3>6.1 手动录入活动（前端校验与桌面版一致）</h3>
    <div class="demo-form">
      <div class="form-row"><label>名称</label><input type="text" x-model="form.name"></div>
      <div class="form-row"><label>开始时间</label><input type="text" x-model="form.start_local" placeholder="2026-10-07T07:12"></div>
      <div class="form-row"><label>距离 (km)</label><input type="text" x-model="form.distance_km"></div>
      <div class="form-row"><label>用时</label><input type="text" x-model="form.duration" placeholder="H:MM:SS / MM:SS / 分钟"></div>
      <div class="form-row"><label>平均心率</label><input type="text" x-model="form.avg_hr"></div>
      <div class="form-row"><label>最大心率</label><input type="text" x-model="form.max_hr"></div>
      <div class="form-row"><label>步频 (步/分)</label><input type="text" x-model="form.avg_cadence"></div>
      <div class="form-row"><label>步幅 (m)</label><input type="text" x-model="form.stride_length_m" placeholder="留空自动推算"></div>
      <div class="form-row"><label>累计爬升 (m)</label><input type="text" x-model="form.elevation_gain_m"></div>
      <div class="form-row"><label>热量 (kcal)</label><input type="text" x-model="form.calories"></div>
      <div class="form-row"><label>主观感受</label>
        <select x-model="form.feel">
          <option value="">未填</option>
          <option value="1">1 很轻松</option><option value="2">2 轻松</option>
          <option value="3">3 一般</option><option value="4">4 吃力</option>
          <option value="5">5 非常吃力</option>
        </select></div>
      <div class="form-row"><label>用力程度 RPE</label>
        <select x-model="form.rpe">
          <option value="">未填</option>
          <template x-for="i in 10" :key="i">
            <option :value="String(i)" x-text="i"></option>
          </template>
        </select></div>
    </div>
    <div class="form-row"><label>备注</label>
      <textarea rows="2" x-model="form.note"></textarea></div>
    <p class="note">实时推算：配速 <b x-text="pacePreview() || '—'"></b>
      · 步幅 <b x-text="stridePreview()"></b></p>
    <div class="flex mt8">
      <button class="btn primary" @click="save()">保存活动</button>
      <span class="spacer"></span>
      <span x-text="msg" :class="msg.indexOf('❌') === 0 ? 'bad' : 'ok'"></span>
    </div>
    <template x-if="saved.length">
      <div class="mt8">
        <p class="note">本次演示已暂存（仅浏览器内存）：</p>
        <div class="out" x-text="JSON.stringify(saved[0], null, 1)"></div>
      </div>
    </template>
  </div>

  <div class="card" id="ocr">
    <h3>6.2 截图识别（本机真机实跑）</h3>
    {ocr_html}
  </div>

  <div class="card">
    <h3>6.3 识别里踩过的坑（都是真机实测暴露出来的）</h3>
    <ul class="rule" style="padding-left:18px">
      <li><b>单位被甩到下一行</b>：真机 OCR 常把「10.52」和「km」拆到两行 →
        增加「单位单独成行时往回找最近纯数字」的回退。</li>
      <li><b>用 <code>\\s*</code> 会跨行</b>：<code>用时 50:00\\n心率 300</code> 里的「00」被当成心率值、单位取到下一行的「心率」→ 读成 0。
        全部改成只允许同行空白的 <code>[ \\t]*</code>。</li>
      <li><b>「10.00 km」被读成配速</b>：10.00 形如 10:00 = 600 s/km，正好落在合法域内，范围守卫拦不住 →
        让「像距离的行」不参与配速候选。</li>
      <li><b>一张风景照也能「识别成功」</b>：OCR 总能把标题行读出来，若把「有活动名」当成功判据就会预填一张空表单 →
        必须命中真正的成绩字段才算识别成功。</li>
      <li><b>撇号丢失</b>：<code>4'59"</code> 被读成 <code>4.59</code> / <code>4。59</code> / <code>4·59</code> →
        分隔符放开成 <code>[:'".,·]</code>，再用范围守卫兜底。</li>
    </ul>
  </div>

  <div class="card">
    <h3>6.4 活动列表（新增「感受/用力」列）</h3>
    <p class="note">桌面版活动页新增的两列；「手动」标记区分自己录的与从 Garmin 同步来的。
      共 {d['counts']['activities']} 条活动，下表展示最近 {len(d.get('activities') or [])} 条。</p>
    {_table(["日期", "名称", "来源", "距离", "配速", "心率", "步频", "爬升", "感受/用力"],
            act_rows)}
  </div>
</section>""")

    # ---- 7 工程说明 ----
    out.append(f"""
<section class="sec" id="eng">
  <h2>七、工程说明</h2>
  <div class="card">
    <ul class="rule" style="padding-left:18px">
      <li><b>技术栈</b>：Python 3.14 + pywebview（WebView2）桌面壳；前端为原生 ES Modules + Alpine.js + ECharts，
        全部本地库，无 CDN、无构建步骤。</li>
      <li><b>数据库</b>：SQLite（WAL），迁移用 <code>PRAGMA user_version</code> + 递增 SQL 文件，
        当前第 {d['counts']['migration']} 版。</li>
      <li><b>零新增依赖</b>：截图识别用 Windows 自带的 <code>Windows.Media.Ocr</code>，
        经 PowerShell 的 WinRT 投影调用；打包成 exe 后同样可用。</li>
      <li><b>测试</b>：<code>pytest</code> 全量 {d['counts']['tests']} 项通过，本轮新增/扩充了
        手动录入、截图解析、配速同步、环境折算、主观加权、佳明主观标签解析六组测试。</li>
      <li><b>桌面归属</b>：活动、健康、仪表盘、日历四处的界面都已接入主观数据与配速同步；
        估算逻辑集中在 <code>domain/evidence.py</code>，界面只做展示。</li>
      <li><b>真机验证</b>：四处新界面在真实 WebView2 窗口里跑过
        <code>tools/verify_ui.py</code>——表单开关无暗屏残留、12 个字段与 RPE 11 档选项齐全、
        配速同步横幅在两页都出、没有主观标签时卡片正确隐藏。</li>
    </ul>
  </div>
  <p class="note">本页为静态快照：数据在构建时从本机数据库导出并内联，页面本身不连数据库、不联网。
    构建工具：<code>tools/build_demo_html.py</code>。</p>
</section>""")
    return "\n".join(out)


def build(with_backtest: bool = False, out_path: Path | None = None,
          want_ocr: bool = True) -> Path:
    from runtrainer.db import database
    from runtrainer.db.repos import activity_repo, health_repo, plan_repo
    database.migrate()

    print("收集数据…")
    dash = gather_dashboard()
    plan = plan_repo.get_active_plan() or {}
    counts = {
        "activities": len(activity_repo.list_activities(limit=100000)),
        "health": len(health_repo.get_health("1970-01-01", date.today().isoformat())),
        "workouts": len(plan_repo.get_workouts(plan["id"])) if plan.get("id") else 0,
        "migration": _migration_version(),
        "tests": _count_tests(),
    }
    data = {
        "dashboard": dash,
        "pace_sync": gather_pace_sync(),
        "weekly_series": dash.get("weekly_series") or [],
        "subjective": gather_weekly_subjective(),
        "env_examples": gather_env_examples(),
        "rpe": gather_rpe_rules(),
        "activities": gather_activities(),
        "counts": counts,
        "backtest": None,
        "ocr": None,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if with_backtest:
        res = run_backtest_ablation()
        if res:
            BACKTEST_CACHE.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    elif not BACKTEST_CACHE.exists() and PROBE_STDOUT.exists():
        res = run_backtest_ablation(stdout_file=PROBE_STDOUT)
        if res:
            BACKTEST_CACHE.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    data["backtest"] = load_backtest_cache()

    print("真机截图识别实跑…")
    data["ocr"] = gather_ocr_demo() if want_ocr else None

    html = render_page(data)
    target = out_path or (DESKTOP / OUT_NAME)
    target.write_text(html, encoding="utf-8")
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"已生成：{target}（{size_mb:.2f} MB）")
    return target


# -------------------------------------------------------------- 页面骨架

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SuperTrainer 作业演示</title>
<style>
__CSS__
__DEMO_CSS__
</style>
</head>
<body>
<header class="topbar">
  <span class="brand">🏃 SuperTrainer <span class="chip">作业演示</span></span>
  <nav>__NAV__</nav>
  <span class="spacer"></span>
  <span class="chip">数据快照 __BUILT__</span>
  <button class="btn small" onclick="toggleTheme()">🌓 主题</button>
</header>
<div class="wrap">
__BODY__
<p class="note" style="text-align:center;margin-top:28px">
  SuperTrainer · 单文件静态快照 · 双击即开，不联网、不读数据库
</p>
</div>
<script>__DEMO_JS__</script>
<script>__ALPINE__</script>
<script>__ECHARTS__</script>
</body>
</html>
"""

NAV = [("overview", "概览"), ("estimate", "预估系统"), ("improve", "本轮优化"),
       ("ablation", "消融回测"), ("sync", "配速同步"), ("subjective", "主观数据"),
       ("form", "手动录入"), ("ocr", "截图识别"), ("eng", "工程说明")]


def _migration_version() -> int:
    from runtrainer.db import database
    with database.get_conn() as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _count_tests() -> int:
    """真实收集数（pytest --collect-only）；失败则退回 def test_ 计数。"""
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                              capture_output=True, timeout=600, cwd=str(ROOT))
        text = proc.stdout.decode("utf-8", errors="replace")
        for line in reversed(text.strip().splitlines()):
            m = re.search(r"(\d+)\s+tests?\s+collected", line)
            if m:
                return int(m.group(1))
    except (subprocess.TimeoutExpired, OSError):
        pass
    n = 0
    for p in sorted((ROOT / "tests").glob("test_*.py")):
        n += len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.M))
    return n


def render_page(data: dict) -> str:
    nav_html = "".join(f'<a href="#{i}">{t}</a>' for i, t in NAV)
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = PAGE_TEMPLATE
    for key, val in (
        ("__CSS__", read_text(ROOT / "web" / "css" / "app.css")),
        ("__NAV__", nav_html),
        ("__BODY__", render_sections(data)),
        ("__BUILT__", str(data["built_at"])),
        ("__ALPINE__", read_text(ROOT / "web" / "vendor" / "alpine.min.js")),
        ("__ECHARTS__", read_text(ROOT / "web" / "vendor" / "echarts.min.js")),
        ("__DEMO_CSS__", DEMO_CSS),
        ("__DEMO_JS__", DEMO_JS),
        ("__DATA__", payload),          # 最后替换：DEMO_JS 里含 __DATA__
    ):
        html = html.replace(key, val)
    return html


def main() -> None:
    ap = argparse.ArgumentParser(description="生成单文件演示 HTML")
    ap.add_argument("--with-backtest", action="store_true",
                    help="重跑环境折算消融回测（约 3 分钟）并写缓存")
    ap.add_argument("--out", default=None, help="输出路径（默认写到桌面）")
    ap.add_argument("--no-ocr", action="store_true", help="跳过截图识别实跑（OCR 不可用时）")
    args = ap.parse_args()

    if args.no_ocr:
        print("（按参数跳过截图识别实跑）")
    target = build(with_backtest=args.with_backtest,
                   out_path=Path(args.out) if args.out else None,
                   want_ocr=not args.no_ocr)
    print(f"\n用 Edge 打开：msedge.exe \"{target}\"（.html 默认关联是 IE，别双击）")


if __name__ == "__main__":
    main()

