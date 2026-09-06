import { tryCall } from '../api.js';
import { baseAxis, chartColors, disposeChart, initChart, resizeIn, tooltip } from '../charts.js';

const KIND_LABELS = { E: '轻松跑', M: '马拉松配速', T: '阈值跑', I: '间歇跑', R: '重复跑', LR: '长距离', RECOVERY: '恢复', TUNEUP: '测试赛', RACE: '比赛', STRENGTH: '力量训练' };
// 课型 → 强度区间（与水平预估六区配速表同源：vdot.PACE_ZONES 的区间与 % 带）
const KIND_ZONES = {
  E: { label: '轻松区', band: '59–74%' },
  M: { label: '有氧/马配区', band: '74–82%' },
  T: { label: '乳酸阈值区', band: '82–92%' },
  I: { label: '最大摄氧量区', band: '92–100%' },
  R: { label: '无氧冲刺区', band: '100–105%' },
  LR: { label: '有氧区（长距离）', band: '59–82%' },
  RECOVERY: { label: '恢复区', band: '50–59%' },
  TUNEUP: { label: '测试赛强度', band: '' },
  RACE: { label: '比赛强度', band: '' },
};

const HTML = `
<template x-if="d">
<div>
  <div class="banner warn" x-show="!d.has_plan">
    还没有训练计划：先去 <a href="#/goal">训练目标</a> 设置比赛目标并生成课表，仪表盘就会显示比赛倒计时与训练负荷。
  </div>

  <div class="flex mb8" x-show="d.sync && d.sync.last_sync_ts">
    <span class="muted" x-text="'🔄 上次同步 ' + fmtTs(d.sync.last_sync_ts)"></span>
    <span class="muted" x-show="syncStatsText(d.sync.last_stats)" x-text="'· ' + syncStatsText(d.sync.last_stats)"></span>
    <span class="st-critical" x-show="d.sync.error" x-text="'⚠️ 最近一次同步失败：' + d.sync.error"></span>
  </div>

  <div class="grid cols-3">
    <div class="card">
      <h2>今日恢复度</h2>
      <div class="flex">
        <span class="ready-big" :class="readyCls(d.readiness.status)" x-text="d.readiness.label"></span>
        <span class="muted" x-text="'· ' + d.readiness.date"></span>
      </div>
      <div class="flex mt8">
        <template x-for="it in d.readiness.items" :key="it.key">
          <span class="ready-pill" :class="pillCls(it.status)">
            <span class="dot" :style="'background:' + dotColor(it.status)"></span>
            <span x-text="it.label + ' ' + it.value"></span>
          </span>
        </template>
        <span class="muted" x-show="!d.readiness.items.length">暂无健康数据，同步 Garmin 后显示</span>
      </div>
      <p class="muted mt8" x-text="d.readiness.note"></p>
    </div>

    <div class="card" x-show="d.has_plan">
      <h2>比赛倒计时</h2>
      <div class="hero">
        <span class="big" x-text="d.race.days_left"></span><span class="unit"> 天</span>
      </div>
      <p class="muted" x-text="d.race.name + ' · ' + d.race.race_date"></p>
      <div class="progress mt8"><div class="progress-fill" :style="'width:' + d.race.progress_pct + '%'"></div></div>
      <p class="muted mt8" x-text="'第 ' + d.race.current_week + ' / ' + d.race.total_weeks + ' 周 · VDOT ' + d.race.vdot"></p>
    </div>

    <div class="card" x-show="d.has_plan">
      <h2>本周负荷</h2>
      <div class="hero">
        <span class="big" x-text="fmtKm(d.week_load.done_km)"></span>
        <span class="unit" x-text="' / ' + d.week_load.planned_km + ' km'"></span>
      </div>
      <div class="progress mt8"><div class="progress-fill" :style="'width:' + (d.week_load.pct || 0) + '%'"></div></div>
      <p class="muted mt8" x-text="'已完成 ' + d.week_load.done_n + ' 次 / 计划 ' + d.week_load.planned_n + ' 次 · 课表完成 ' + d.week_load.done_plan + ' 节 · 上周 ' + fmtKm(d.kpis.last_week_km) + ' km'"></p>
    </div>
  </div>

  <div class="grid cols-4" x-show="d.has_plan">
    <div class="card stat-tile">
      <div class="t-label">近 7 天执行率</div>
      <div class="t-val" x-text="d.kpis.compliance_7d && d.kpis.compliance_7d.ratio != null ? Math.round(d.kpis.compliance_7d.ratio * 100) + '%' : '—'"></div>
      <div class="t-hint" x-show="d.kpis.compliance_7d" x-text="'完成 ' + d.kpis.compliance_7d.done_km + ' / 计划 ' + d.kpis.compliance_7d.planned_km + ' km'"></div>
    </div>
    <div class="card stat-tile">
      <div class="t-label">ACWR（急/慢性负荷比）</div>
      <div class="t-val" x-text="d.kpis.acwr != null ? d.kpis.acwr : '—'"></div>
      <div class="t-hint" x-show="d.kpis.acwr != null">
        <span class="badge" :class="d.kpis.acwr_status === 'ok' ? 'st-ok' : 'st-warn'"
              x-text="d.kpis.acwr_status === 'ok' ? '0.8–1.3 健康区间' : '超出健康区间'"></span>
      </div>
    </div>
    <div class="card stat-tile">
      <div class="t-label">训练单调性（7 天）</div>
      <div class="t-val" x-text="d.kpis.monotony != null ? d.kpis.monotony : '—'"></div>
      <div class="t-hint" x-show="d.kpis.monotony != null" x-text="d.kpis.monotony > 2 ? '偏高：每日负荷起伏过小' : '正常'"></div>
    </div>
    <div class="card stat-tile">
      <div class="t-label">训练应变（7 天）</div>
      <div class="t-val" x-text="d.kpis.strain != null ? d.kpis.strain : '—'"></div>
      <div class="t-hint">跑量 × 单调性</div>
    </div>
  </div>

  <div class="card" x-show="d.has_plan">
    <h2>今日训练</h2>
    <template x-for="w in d.today_workouts" :key="w.id">
      <div class="flex today-row">
        <span class="badge" :class="'kind-' + w.kind" x-text="KIND_LABELS[w.kind] || w.kind"></span>
        <span class="badge zn-chip" :class="'znk-' + w.kind" x-show="zoneOf(w.kind)"
              :title="'对应强度区间（配速上限按课表 VDOT）'"
              x-text="zoneOf(w.kind)"></span>
        <span class="muted" x-show="w.slot > 1" x-text="'第 ' + w.slot + ' 练'"></span>
        <span x-text="w.title"></span>
        <span class="spacer"></span>
        <span class="muted" x-text="workoutLine(w)"></span>
        <span class="badge soft" x-text="statusLabel(w.status)"></span>
      </div>
    </template>
    <p class="muted" x-show="!d.today_workouts.length">今天休息 🎉 没有训练安排</p>
  </div>

  <!-- 成绩水平预估：近 30 天数据综合（比赛/手表 VO2max/配速-心率/间歇），
       每次同步后 syncRefresh → load 自动重算；与目标页 180 天基线不同，
       这里回答「现在能跑多少」 -->
  <div class="card" x-show="d.ability_30d">
    <div class="flex mb8" style="flex-wrap:wrap;gap:8px">
      <h2 style="margin:0">成绩水平预估</h2>
      <span class="muted" x-text="'近 ' + (d.ability_30d.window_days || 30) + ' 天 · ' + abilityCaption()"></span>
    </div>
    <template x-if="d.ability_30d.vdot">
      <div>
        <div class="grid cols-2">
          <div>
            <div class="hero">
              <span class="big" x-text="d.ability_30d.vdot"></span><span class="unit"> VDOT</span>
            </div>
            <p class="muted">按当前水平推算的等效成绩（非训练目标）</p>
            <div class="flex mt8" style="flex-wrap:wrap;gap:6px">
              <template x-for="(sec, label) in d.ability_30d.predictions" :key="label">
                <span class="badge soft" x-text="label + ' ' + fmtRace(sec)"></span>
              </template>
            </div>
            <p class="muted mt8" x-show="d.ability_30d.plan_vdot" x-text="planVdotNote()"></p>
          </div>
          <div>
            <p class="muted">依据</p>
            <ul class="mt8" style="padding-left:18px">
              <template x-for="ev in d.ability_30d.evidence" :key="ev.source">
                <li><span x-text="evName(ev.source)"></span>：<span x-text="ev.detail"></span><span class="muted" x-text="'（VDOT ' + ev.vdot + '）'"></span></li>
              </template>
            </ul>
          </div>
        </div>
        <!-- 当前水平各区间对应配速（恢复→无氧冲刺）：课表/日历里的区间安排
             都按这张表标注；PB 刷新后随整卡自动重算 -->
        <details class="mt8" x-show="(d.ability_30d.zones || []).length" :open="true">
          <summary class="muted small">当前水平各区间配速（计划与日历按此标注区间）</summary>
          <div class="zones-table mt8">
            <template x-for="z in d.ability_30d.zones" :key="z.key">
              <div class="z-row">
                <span class="rail" :class="'zn-' + z.key"></span>
                <b x-text="z.label"></b>
                <!-- 无档位字母（恢复跑）时仍保留占位格，避免 x-show 移除元素打乱行内网格 -->
                <span class="zn-mark" :class="z.mark ? 'kind-' + z.mark : ''" x-text="z.mark || ''"></span>
                <span class="muted" x-text="z.band"></span>
                <span class="spacer"></span>
                <span class="num muted" x-text="z.use"></span>
                <span class="num pace" x-text="fmtPace(z.pace_slow_s_km) + ' – ' + fmtPace(z.pace_fast_s_km)"></span>
              </div>
            </template>
          </div>
        </details>
        <!-- 近一年最佳成绩 + 训练保持度：数据窗口 30 天时不够「现在能跑多少」
             的参考，这里补上全年硬证据 -->
        <div class="mt8">
          <div class="flex" style="flex-wrap:wrap;gap:6px;align-items:center">
            <span class="muted">近一年最佳</span>
            <template x-for="b in d.ability_30d.year_bests" :key="b.distance">
              <span class="badge soft" :title="yearBestTitle(b)"
                    x-text="b.distance + ' ' + fmtRace(b.best_seconds)"></span>
            </template>
            <span class="muted" x-show="!(d.ability_30d.year_bests || []).length">尚无达标记录</span>
          </div>
          <p class="muted mt8" x-text="keepUpNote()"></p>
        </div>
      </div>
    </template>
    <p class="muted mt8" x-show="!d.ability_30d.vdot" x-text="d.ability_30d.note || '数据不足，暂无法预估'"></p>
  </div>

  <div class="card">
    <h3>近 8 周跑量：计划 vs 实际</h3>
    <div class="chart" id="dash-weekly-chart"></div>
    <table class="mt8">
      <thead><tr>
        <th>周（起始）</th><th class="num">计划 km</th><th class="num">实际 km</th><th class="num">完成率</th>
      </tr></thead>
      <tbody>
        <template x-for="r in d.weekly_series" :key="r.week_start">
          <tr>
            <td class="num" x-text="r.label"></td>
            <td class="num" x-text="r.planned_km"></td>
            <td class="num" x-text="r.done_km"></td>
            <td class="num" x-text="r.planned_km > 0 ? Math.round(r.done_km / r.planned_km * 100) + '%' : '—'"></td>
          </tr>
        </template>
        <tr x-show="!d.weekly_series.length"><td colspan="4" class="muted">暂无跑步数据</td></tr>
      </tbody>
    </table>
  </div>

  <div class="grid cols-2">
    <div class="card">
      <h3>HRV 趋势（30 天，ms）</h3>
      <div class="chart-sm" id="dash-hrv-chart"></div>
      <table class="mt8">
        <thead><tr><th>日期</th><th class="num">HRV</th><th>状态</th></tr></thead>
        <tbody>
          <template x-for="r in d.health_trend" :key="r.date">
            <tr>
              <td class="num" x-text="r.date"></td>
              <td class="num" x-text="r.hrv != null ? r.hrv.toFixed(0) : '—'"></td>
              <td x-show="!r.hrv_status" class="muted">—</td>
              <td x-show="r.hrv_status"><span class="status-pair" :class="hrvClass(r.hrv_status)"><span class="dot" :style="'background:' + hrvColor(r.hrv_status)"></span><span x-text="hrvLabel(r.hrv_status)"></span></span></td>
            </tr>
          </template>
          <tr x-show="!d.health_trend.length"><td colspan="3" class="muted">暂无健康数据</td></tr>
        </tbody>
      </table>
    </div>
    <div class="card">
      <h3>静息心率趋势（30 天，bpm）</h3>
      <div class="chart-sm" id="dash-rhr-chart"></div>
      <table class="mt8">
        <thead><tr><th>日期</th><th class="num">静息心率</th></tr></thead>
        <tbody>
          <template x-for="r in d.health_trend" :key="r.date">
            <tr>
              <td class="num" x-text="r.date"></td>
              <td class="num" x-text="r.resting_hr != null ? r.resting_hr.toFixed(0) : '—'"></td>
            </tr>
          </template>
          <tr x-show="!d.health_trend.length"><td colspan="2" class="muted">暂无健康数据</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card" x-show="d.coach && (d.coach.advice || d.coach.last_chat)">
    <h2>AI 教练</h2>
    <div x-show="d.coach.advice">
      <p x-text="d.coach.advice.summary"></p>
      <ul class="mt8" style="padding-left:18px">
        <template x-for="s in d.coach.advice.key_signals" :key="s">
          <li x-text="s"></li>
        </template>
      </ul>
    </div>
    <p class="muted" x-show="!d.coach.advice && d.coach.last_chat" x-text="(roleLabel(d.coach.last_chat.role) + '：' + d.coach.last_chat.content) + '…'"></p>
    <p class="muted" x-show="d.coach.advice && d.coach.last_chat"
       x-text="'最近对话（' + roleLabel(d.coach.last_chat.role) + '）：' + d.coach.last_chat.content + '…'"></p>
    <div class="mt8"><a class="btn small" href="#/coach">去教练页 →</a></div>
  </div>
</div>
</template>`;

export function initDashboard() {
  const sec = document.getElementById('page-dashboard');
  sec.innerHTML = HTML;

  window.Alpine.data('dashboardPage', () => ({
    d: null,

    async init() {
      await this.load();
    },
    async shown() {
      const el = document.getElementById('page-dashboard');
      resizeIn(el);
      // 切回本页时重新拉取（同步/补录/教练建议后及时变动）；
      // init() 刚加载过则跳过，避免首次挂载双重加载
      if (!this._lastLoad || Date.now() - this._lastLoad > 2000) {
        await this.load();
      }
      // boot 时页面隐藏（容器 0 尺寸）会跳过渲染；首次可见时补渲染（同 health 页）
      if (this._renderDeferred) {
        this._renderDeferred = false;
        this.renderCharts();
        if (this._renderDeferred) {
          setTimeout(() => {
            const probe = document.getElementById('dash-weekly-chart');
            if (this._renderDeferred && probe && probe.offsetWidth) this.shown();
          }, 60);
        }
      }
    },
    async syncRefresh() {
      // 同步完成（新增活动/重建课表/自动分析/健康数据）后强制刷新，
      // 无视 shown() 的 2s 防双载——同步结果必须立即可见
      await this.load();
    },
    async load() {
      this._lastLoad = Date.now();
      const { ok, data, error } = await tryCall('get_dashboard');
      if (!ok) { this.$dispatch('toast', { text: '读取仪表盘失败: ' + error }); return; }
      this.d = data || {};
      await this.$nextTick();
      this.renderCharts();
    },

    fmtKm(v) { return v == null ? '—' : String(Math.round(v * 10) / 10); },
    fmtRace(s) {
      if (s == null) return '—';
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
      const p = (n) => String(n).padStart(2, '0');
      return (h ? h + ':' : '') + p(m) + ':' + p(sec);
    },
    abilityCaption() {
      const a = this.d.ability_30d || {};
      return '每次同步自动重算 · 更新 ' + (a.as_of || '');
    },
    evName(src) {
      return { recent_race: '近期比赛', garmin_vo2max: '手表 VO2max',
               threshold_trend: '配速-心率阈值', cruise_ability: '节奏/巡航跑',
               t_intervals: '阈值型间歇', hrr_pace: 'HRR 有氧配速',
               interval_ability: '间歇能力', year_best: '近一年最佳',
               hr_trend: '配速-心率趋势',
               plan_execution: '课表完成度' }[src] || src;
    },
    zoneOf(kind) {
      const z = KIND_ZONES[kind];
      if (!z) return '';
      return z.band ? z.label + ' · ' + z.band : z.label;
    },
    fmtPace(s) {
      if (s == null) return '—';
      return Math.floor(s / 60) + ':' + String(Math.round(s % 60)).padStart(2, '0');
    },
    yearBestTitle(b) {
      const src = b.source === 'race' ? '比赛/近似全程' : '长跑中切出的最快分段';
      return (b.distance || '') + ' 最佳 ' + this.fmtRace(b.best_seconds)
        + '（' + String(b.date || '').slice(0, 10) + ' · ' + src
        + (b.vdot ? ' · 等效 VDOT ' + b.vdot : '') + '）';
    },
    keepUpNote() {
      const c = (this.d.ability_30d || {}).consistency;
      if (!c || !c.total_weeks || c.run_weeks === undefined) return '';
      if (!c.run_weeks) return '近一年暂无跑步记录，无法评估训练保持度';
      return '训练保持度：近一年 ' + c.run_weeks + '/' + c.total_weeks + ' 周有跑步'
        + '（' + c.run_week_pct + '%）；近 4 周周均 ' + c.recent_4w_avg_km + ' km'
        + '，约为全年周均的 ' + c.recent_vs_year_pct + '%';
    },
    planVdotNote() {
      const a = this.d.ability_30d;
      if (!a || !a.plan_vdot || !a.vdot) return '';
      const diff = a.vdot - a.plan_vdot;
      if (Math.abs(diff) < 0.05) return '当前水平与课表 VDOT ' + a.plan_vdot + ' 一致——正好支撑目标配速';
      return diff > 0
        ? '当前水平高过课表 VDOT ' + a.plan_vdot + ' 约 ' + diff.toFixed(1) + '——按目标配速跑会较轻松'
        : '当前水平比课表 VDOT ' + a.plan_vdot + ' 低约 ' + (-diff).toFixed(1) + '——课表配速偏高，吃力时去教练页反馈调低';
    },
    fmtTs(ts) {
      const d = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    },
    fmtMin(m) {
      if (m == null) return '';
      return m >= 60 ? Math.floor(m / 60) + 'h' + String(Math.round(m % 60)).padStart(2, '0') : Math.round(m) + '′';
    },
    workoutLine(w) {
      const parts = [];
      if (w.distance_km) parts.push(w.distance_km + ' km');
      if (w.duration_min) parts.push(this.fmtMin(w.duration_min));
      return parts.join(' · ');
    },
    statusLabel(st) { return { completed: '已完成', skipped: '已跳过', planned: '待完成' }[st] || st; },
    roleLabel(r) { return r === 'coach' ? '教练' : '你'; },
    readyCls(s) { return { good: 'st-good', ok: 'st-warning', low: 'st-critical' }[s] || 'muted'; },
    pillCls(s) { return { good: 'st-good', ok: 'st-warning', low: 'st-critical' }[s] || ''; },
    dotColor(s) {
      return { good: 'var(--st-good)', ok: 'var(--st-warning)', low: 'var(--st-critical)' }[s] || 'var(--text-muted)';
    },
    hrvLabel(s) { return { balanced: '平衡', unbalanced: '不平衡', low: '偏低' }[s] || s; },
    hrvColor(s) {
      return { balanced: 'var(--st-good)', unbalanced: 'var(--st-warning)', low: 'var(--st-critical)' }[s] || 'var(--text-muted)';
    },
    hrvClass(s) { return { balanced: 'st-good', unbalanced: 'st-warning', low: 'st-critical' }[s] || ''; },
    syncStatsText(st) {
      if (!st) return '';
      const parts = [];
      if (st.activities) parts.push('新增活动 ' + st.activities + ' 条');
      if (st.auto_analysis) parts.push('AI 已分析 ' + st.auto_analysis + ' 条新训练');
      if (st.health_days) parts.push('健康数据 ' + st.health_days + ' 天');
      if (st.plan_rebuilt) parts.push('课表已更新');
      return parts.join(' · ');
    },

    renderCharts() {
      // 容器不可见（页面隐藏中）时 ECharts 以 0 尺寸初始化 → 压缩变小，
      // 推迟渲染等 shown() 补（同 health 页）
      const probe = document.getElementById('dash-weekly-chart');
      if (!probe || !probe.offsetWidth) {
        this._renderDeferred = true;
        return;
      }
      const colors = chartColors();
      const mk = (extra = {}) => ({
        grid: { left: 40, right: 12, top: 24, bottom: 24 },
        yAxis: { ...baseAxis(colors), type: 'value' },
        tooltip: tooltip(colors),
        ...extra,
      });
      // 1) 周跑量双系列柱：实际=accent 红、计划=muted 灰（避免复用 kindE 蓝的
      // 「轻松跑」语义；周跑量不是训练类型，不用类型色）
      const wk = this.d.weekly_series || [];
      const wkOpt = mk({
        grid: { left: 40, right: 12, top: 24, bottom: 46 },
        xAxis: { type: 'category', data: wk.map((r) => r.label), ...baseAxis(colors) },
        legend: { bottom: 0, textStyle: { color: colors.muted, fontSize: 11 } },
        series: [
          { name: '实际', type: 'bar', data: wk.map((r) => r.done_km), itemStyle: { color: colors.accent }, barMaxWidth: 22 },
          { name: '计划', type: 'bar', data: wk.map((r) => r.planned_km), itemStyle: { color: colors.muted }, barMaxWidth: 22 },
        ],
      });
      // 2) HRV 线（kindM 青 + 状态点着色，与健康页同指标同色）
      const ht = this.d.health_trend || [];
      const cat = { type: 'category', data: ht.map((r) => r.date.slice(5)), ...baseAxis(colors) };
      const statusColor = (s) => (s === 'low' ? colors.critical : s === 'unbalanced' ? colors.warning : colors.good);
      const hrvOpt = mk({
        xAxis: cat,
        series: [{
          type: 'line', name: 'HRV',
          data: ht.map((r) => ({ value: r.hrv, itemStyle: { color: statusColor(r.hrv_status) } })),
          lineStyle: { color: colors.kindM, width: 2 },
          itemStyle: { color: colors.kindM },
          symbolSize: 5, connectNulls: false,
        }],
      });
      // 3) 静息心率线（accent 红，与健康页同指标同色）
      const rhrOpt = mk({
        xAxis: cat,
        series: [{
          type: 'line', name: '静息心率',
          data: ht.map((r) => r.resting_hr),
          lineStyle: { color: colors.accent, width: 2 },
          itemStyle: { color: colors.accent },
          symbolSize: 5, areaStyle: { opacity: 0.08, color: colors.accent },
        }],
      });
      const els = ['dash-weekly-chart', 'dash-hrv-chart', 'dash-rhr-chart'];
      els.forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        disposeChart(el);
        initChart(el, [wkOpt, hrvOpt, rhrOpt][i]);
      });
    },
  }));
}
