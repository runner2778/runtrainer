import { tryCall } from '../api.js';
import { baseAxis, chartColors, disposeChart, initChart, resizeIn, tooltip } from '../charts.js';

// 与后端 workout_analysis.HR_TREND_FLAT_BPM 一致：|Δ| < 3bpm 视为「没变」，
// 前端配色/文案与后端结论句因此不会互相矛盾。
const HR_TREND_FLAT_BPM = 3;

const HTML = `
<div class="flex mb8">
  <div class="flex">
    <label class="muted">时间范围</label>
    <select x-model="range" @change="load()" style="width:auto">
      <option value="7">近 7 天</option>
      <option value="30">近 30 天</option>
      <option value="90">近 90 天</option>
      <option value="120">近 120 天</option>
      <option value="365">近一年</option>
    </select>
  </div>
  <div class="spacer"></div>
  <span class="muted" x-text="'共 ' + rows.length + ' 天'"></span>
</div>

<div class="grid cols-2">
  <div class="card"><h3>HRV（夜间平均，ms）</h3><div class="chart" id="health-hrv-chart"></div></div>
  <div class="card"><h3>静息心率（bpm）</h3><div class="chart" id="health-rhr-chart"></div></div>
  <div class="card"><h3>睡眠结构（小时）</h3><div class="chart" id="health-sleep-chart"></div></div>
  <div class="card"><h3>压力（0-100）</h3><div class="chart" id="health-stress-chart"></div></div>
  <!-- 主观用力程度：手动录入、截图识别、佳明 app 标签三处同权汇总到这一张周曲线。
       与配速-心率卡配合看——同心率下 RPE 抬升通常意味着疲劳累积或高温。 -->
  <div class="card"><h3>主观用力程度（周均 RPE）</h3>
    <p class="muted">1 极轻松 → 10 拼尽全力。同一配速下周均 RPE 抬升，常见于疲劳累积或高温；
      只有标过标签的周才有线（断点即没标），柱高是该周跑步次数（右轴）。</p>
    <template x-if="subjHasData()">
      <div>
        <div class="chart" id="health-subj-chart"></div>
        <p class="muted">虚线 = 同期主观感受（1-5），已按 ×2 映射到同一纵轴便于与 RPE 对照，鼠标悬停看原值。</p>
      </div>
    </template>
    <p class="muted" x-show="!subjHasData()" x-text="subjEmptyNote()"></p>
  </div>
  <div class="card"><h3>同配速平均心率 · 近 16 周</h3>
    <div class="flex mb8">
      <label class="muted">配速档</label>
      <select x-model.number="paceBinSel" @change="renderPaceBinChart()" style="width:auto"
              :disabled="!paceWeekly.bins.length">
        <template x-for="b in paceWeekly.bins" :key="b.start_s">
          <option :value="b.start_s"
                  x-text="b.label + '/km（' + b.segs + ' 段 · ' + b.distance_km + ' km）'"></option>
        </template>
      </select>
      <div class="spacer"></div>
      <span class="muted" x-text="paceBinCoverage()"></span>
    </div>
    <p class="muted">同一配速档下，越近的周心率越低 = 有氧能力进步</p>
    <p class="pacehr-summary" x-show="selPaceBin()" :style="paceBinTrendStyle()"
       x-text="'📊 ' + (selPaceBin() ? selPaceBin().note : '')"></p>
    <p class="muted" x-show="!paceWeekly.bins.length"
       x-text="'⚠️ ' + paceBinEmptyNote()"></p>
    <p class="muted" x-show="paceWeekly.bins.length">
      粗线 = 滚动 4 周均值（含当周及之前 3 周，距离加权，稀疏周也不断）；圆点 = 当周实测（该档 ≥2 段才有点）；浅柱 = 当周该档段数（右轴）</p>
    <p class="muted" x-show="paceWeekly.bins.length">
      色带 = 最初 4 周 ↔ 最近 4 周均值之间的落差，带高就是心率变化量；绿=心率降（进步）、黄=心率升（退步）</p>
    <p class="muted" x-show="paceWeekly.bins.length">
      按每段（间歇课的快跑段/匀速跑整段）的配速对齐心率，不取整场平均——3:20 这类快档只有分段才看得到</p>
    <div class="chart" id="health-pacehr-chart"></div>
  </div>
  <div class="card"><h3>配速-心率明细</h3>
    <p class="muted">配速与心率均按距离加权平均——长跑权重大，不会被短慢跑带偏</p>
    <table>
      <thead><tr>
        <th>周（起始）</th><th class="num">平均配速</th><th class="num">平均心率</th>
        <th class="num">跑量(km)</th><th class="num">次数</th>
      </tr></thead>
      <tbody>
        <template x-for="r in paceHr" :key="r.week_start">
          <tr>
            <td class="num" x-text="r.week_start"></td>
            <td class="num" x-text="fmtPace(r.avg_pace_s_km)"></td>
            <td class="num" x-text="r.avg_hr != null ? r.avg_hr.toFixed(0) : '—'"></td>
            <td class="num" x-text="r.distance_km"></td>
            <td class="num" x-text="r.runs"></td>
          </tr>
        </template>
        <tr x-show="!paceHr.length"><td colspan="5" class="muted">暂无跑步数据</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="card">
  <h3>数据明细</h3>
  <table>
    <thead><tr>
      <th>日期</th><th class="num">睡眠(h)</th><th class="num">睡眠评分</th>
      <th class="num">HRV(ms)</th><th>HRV 状态</th><th class="num">静息心率</th><th class="num">压力</th>
    </tr></thead>
    <tbody>
      <template x-for="r in rows" :key="r.date">
        <tr>
          <td class="num" x-text="r.date"></td>
          <td class="num" x-text="r.sleep_duration_s ? (r.sleep_duration_s/3600).toFixed(1) : '—'"></td>
          <td class="num" x-text="r.sleep_score != null ? r.sleep_score.toFixed(0) : '—'"></td>
          <td class="num" x-text="r.hrv_avg_ms != null ? r.hrv_avg_ms.toFixed(0) : '—'"></td>
          <td x-show="!r.hrv_status" class="muted">—</td>
          <td x-show="r.hrv_status">
            <span class="status-pair" :class="hrvClass(r.hrv_status)">
              <span class="dot" :style="'background:' + hrvColor(r.hrv_status)"></span>
              <span x-text="hrvLabel(r.hrv_status)"></span>
            </span>
          </td>
          <td class="num" x-text="r.resting_hr != null ? r.resting_hr.toFixed(0) : '—'"></td>
          <td class="num" x-text="r.stress_avg != null ? r.stress_avg.toFixed(0) : '—'"></td>
        </tr>
      </template>
      <tr x-show="!rows.length"><td colspan="7" class="muted">暂无健康数据</td></tr>
    </tbody>
  </table>
</div>`;

export function initHealth() {
  const sec = document.getElementById('page-health');
  sec.innerHTML = HTML;

  window.Alpine.data('healthPage', () => ({
    range: '120',
    rows: [],
    paceHr: [],
    paceWeekly: { bins: [], weeks: [] },
    paceBinSel: null,
    // 周均 RPE/主观感受：[{week_start, runs, rpe_n, rpe_avg, feel_n, feel_avg}]
    subjWeekly: [],

    async init() {
      await this.load();
    },
    async shown() {
      const el = document.getElementById('page-health');
      resizeIn(el);
      // 切回本页时重新拉取数据（同步/补录后应用内数据及时变动）；
      // init() 刚加载过则跳过，避免首次挂载双重加载
      if (!this._lastLoad || Date.now() - this._lastLoad > 2000) {
        await this.load();
      }
      // boot 时页面隐藏（容器 0 尺寸）会跳过渲染；首次可见时补渲染，
      // 与 ResizeObserver 双保险，杜绝曲线图偶尔压缩变小
      if (this._renderDeferred) {
        this._renderDeferred = false;
        this.renderCharts();
        if (this._renderDeferred) {
          // WebView2 中 Alpine x-show 的可见性有时晚于路由通知（实测
          // setTimeout(0) 回调里容器仍 display:none）→ 等容器可测后重试一次
          setTimeout(() => {
            const probe = document.getElementById('health-hrv-chart');
            if (this._renderDeferred && probe && probe.offsetWidth) this.shown();
          }, 60);
        }
      }
    },
    async load() {
      this._lastLoad = Date.now();
      const start = this.daysAgo(parseInt(this.range));      const { ok, data, error } = await tryCall('list_health', start);
      if (!ok) { this.$dispatch('toast', { text: '读取健康数据失败: ' + error }); return; }
      this.rows = data || [];
      // 配速-心率与时间范围选择器联动（不再固定一年）：选 30 天就只看
      // 30 天——同步刚结束或切范围后明细/曲线都即时重算
      const days = parseInt(this.range) || 120;
      const ph = await tryCall('list_weekly_pace_hr', days);
      this.paceHr = ph.ok ? (ph.data || []) : [];
      // 同配速心率卡固定看近 16 个自然周（与上方时间范围解耦）：周刻度
      // 才是这个功能要的精度，跟着 7 天/30 天摆动没有意义
      const pb = await tryCall('list_pace_bin_weekly', 16);
      this.paceWeekly = pb.ok ? (pb.data || { bins: [], weeks: [] })
        : { bins: [], weeks: [] };
      // 主观用力程度周曲线：跟着上方时间范围走（标签密度低，给足窗口）
      const sw = await tryCall('list_weekly_subjective', days);
      this.subjWeekly = sw.ok ? (sw.data || []) : [];
      const starts = (this.paceWeekly.bins || []).map((b) => b.start_s);
      // 保留用户已选的档；档位消失（跑量变化）或首次加载才回落到默认档
      if (!starts.includes(this.paceBinSel)) {
        this.paceBinSel = this.paceWeekly.default_start_s != null
          ? this.paceWeekly.default_start_s : (starts.length ? starts[0] : null);
      }
      await this.$nextTick();
      this.renderCharts();
    },
    // 同步完成（可能是周期自动同步）→ 强制重拉，心率对照表随新数据重绘
    async syncRefresh() { await this.load(); },
    daysAgo(n) {
      const d = new Date(); d.setDate(d.getDate() - n);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },
    hrvLabel(s) { return { balanced: '平衡', unbalanced: '不平衡', low: '偏低' }[s] || s; },
    selPaceBin() {
      return (this.paceWeekly.bins || [])
        .find((b) => b.start_s === this.paceBinSel) || null;
    },
    paceBinCoverage() {
      const b = this.selPaceBin();
      if (!b) return '';
      const total = (this.paceWeekly.weeks || []).length;
      const n = b.hr_roll.filter((v) => v != null).length;
      const m = b.hr.filter((v) => v != null).length;
      return `${n}/${total} 周有趋势点 · ${m} 周有实测`;
    },
    // 这一档的走向：'down' 心率降（进步）/ 'up' 升（退步）/ 'flat' 无明显变化。
    // 阈值与后端 HR_TREND_FLAT_BPM 一致（3 bpm），前后端结论句不会打架。
    paceBinTrend(bin) {
      const b = bin || this.selPaceBin();
      if (!b || b.drop == null) return 'flat';
      if (b.drop <= -HR_TREND_FLAT_BPM) return 'down';
      if (b.drop >= HR_TREND_FLAT_BPM) return 'up';
      return 'flat';
    },
    paceBinTrendColor(bin) {
      const t = this.paceBinTrend(bin);
      if (t === 'down') return 'var(--st-good)';      // 同配速心率更低 = 进步
      if (t === 'up') return 'var(--st-warning)';
      return 'var(--accent)';
    },
    // 结论句跟着走向换色：绿色一眼就是「好消息」，不用去读数字
    paceBinTrendStyle() {
      const t = this.paceBinTrend();
      return t === 'down' ? 'color: var(--st-good)'
        : t === 'up' ? 'color: var(--st-warning)' : '';
    },
    paceBinEmptyNote() {
      return '近 16 周该配速档的样本太稀（4 周窗口内凑不满 2 段）'
        + '——先在同一个配速多练几次，这里才画得出趋势';
    },
    hrvColor(s) { return { balanced: 'var(--st-good)', unbalanced: 'var(--st-warning)', low: 'var(--st-critical)' }[s] || 'var(--text-muted)'; },
    hrvClass(s) { return { balanced: 'st-good', unbalanced: 'st-warning', low: 'st-critical' }[s] || ''; },
    renderCharts() {
      // 容器不可见（页面隐藏中）时 ECharts 以 0 尺寸初始化 → 压缩变小。
      // 此时推迟渲染，等 shown() 在页面可见后补渲染。
      const probe = document.getElementById('health-hrv-chart');
      if (!probe || !probe.offsetWidth) {
        this._renderDeferred = true;
        return;
      }
      const colors = chartColors();
      const rows = this.rows;
      const dates = rows.map((r) => r.date.slice(5));
      const hrv = rows.map((r) => r.hrv_avg_ms);
      const rhr = rows.map((r) => r.resting_hr);
      const stress = rows.map((r) => r.stress_avg);
      const hrvStatus = rows.map((r) => r.hrv_status);
      const cat = { type: 'category', data: dates, ...baseAxis(colors) };
      const mk = (extra = {}) => ({
        grid: { left: 40, right: 12, top: 24, bottom: 24 },
        xAxis: cat,
        yAxis: { ...baseAxis(colors), type: 'value' },
        tooltip: tooltip(colors),
        ...extra,
      });
      const hrvChart = mk({
        series: [{
          type: 'line', data: hrv, name: 'HRV',
          lineStyle: { color: colors.kindM, width: 2 },
          itemStyle: { color: colors.kindM },
          symbolSize: 5,
        }],
      });
      // HRV 状态点着色（good/warning/critical 仅用于状态，不用作序列色）
      const statusColor = (s) => (s === 'low' ? colors.critical : s === 'unbalanced' ? colors.warning : colors.good);
      hrvChart.series[0].data = rows.map((r) => ({
        value: r.hrv_avg_ms,
        itemStyle: { color: statusColor(r.hrv_status) },
      }));
      const rhrChart = mk({
        series: [{
          type: 'line', data: rhr, name: '静息心率',
          lineStyle: { color: colors.accent, width: 2 },
          itemStyle: { color: colors.accent },
          symbolSize: 5, areaStyle: { opacity: 0.08, color: colors.accent },
        }],
      });
      const sleepChart = mk({
        // 底部图例需额外留白，否则图例与 x 轴日期标签重叠
        grid: { left: 40, right: 12, top: 24, bottom: 46 },
        legend: { bottom: 0, textStyle: { color: colors.muted, fontSize: 11 } },
        series: [
          { name: '深度', type: 'bar', stack: 's', data: rows.map((r) => r.deep_s ? +(r.deep_s / 3600).toFixed(2) : 0), itemStyle: { color: colors.ramp[3] } },
          { name: '浅睡', type: 'bar', stack: 's', data: rows.map((r) => r.light_s ? +(r.light_s / 3600).toFixed(2) : 0), itemStyle: { color: colors.ramp[1] } },
          { name: 'REM', type: 'bar', stack: 's', data: rows.map((r) => r.rem_s ? +(r.rem_s / 3600).toFixed(2) : 0), itemStyle: { color: colors.ramp[0] } },
          { name: '清醒', type: 'bar', stack: 's', data: rows.map((r) => r.awake_s ? +(r.awake_s / 3600).toFixed(2) : 0), itemStyle: { color: colors.muted } },
        ],
      });
      const stressChart = mk({
        series: [{
          type: 'line', data: stress, name: '压力',
          lineStyle: { color: colors.kindT, width: 2 },
          itemStyle: { color: colors.kindT },
          symbolSize: 5,
        }],
      });
      // 同配速心率：选定一个配速档 → 该档近 16 个自然周的心率折线
      // （档宽 15s，比旧的多时期 30s 对照更细、刻度更密）
      const paceBinOpt = this.paceBinOption(colors);
      const subjOpt = this.subjOption(colors);
      const els = ['health-hrv-chart', 'health-rhr-chart', 'health-sleep-chart',
                   'health-stress-chart', 'health-pacehr-chart', 'health-subj-chart'];
      els.forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        disposeChart(el);
        initChart(el, [hrvChart, rhrChart, sleepChart, stressChart, paceBinOpt,
                       subjOpt][i]);
      });
    },

    // ---- 主观用力程度（周均）----
    subjHasData() {
      return (this.subjWeekly || []).some((w) => w.rpe_avg != null
        || w.feel_avg != null);
    },
    subjEmptyNote() {
      return '⚠️ 还没有主观标签：在「活动」页录入时填「主观感受 / 用力程度」，'
        + '或先在设置页同步佳明——佳明 app 里标过的感受会被一并读回。';
    },
    /** 周均 RPE 折线 + 周均感受（折半映射到 RPE 轴）+ 跑步次数浅柱（右轴）。
        三条叠一张图是为了直接读「这次是不是比上次更费劲」——分开画就得来回对。 */
    subjOption(colors) {
      // 后端按周降序返回，图表要自左向右 → 反转成时间正序
      const wks = (this.subjWeekly || []).slice().reverse();
      const ax = { type: 'category', data: wks.map((w) => w.week_start.slice(5)),
                   ...baseAxis(colors) };
      const maxRuns = Math.max(2, ...wks.map((w) => w.runs || 0));
      return {
        grid: { left: 40, right: 20, top: 24, bottom: 24 },
        xAxis: ax,
        yAxis: [
          { ...baseAxis(colors), type: 'value', min: 1, max: 10, name: 'RPE',
            nameTextStyle: { color: colors.muted, fontSize: 11 } },
          { ...baseAxis(colors), type: 'value', min: 0, max: maxRuns,
            splitLine: { show: false }, axisLabel: { show: false } },
        ],
        tooltip: tooltip(colors, {
          formatter: (params) => {
            const ps = Array.isArray(params) ? params : [params];
            const first = ps[0] || {};
            const w = wks[first.dataIndex] || {};
            const lines = [`<b>${w.week_start || ''}</b>`];
            if (w.rpe_avg != null) {
              lines.push(`平均 RPE ${w.rpe_avg} / 10（${w.rpe_n} 次标注）`);
            } else lines.push('平均 RPE —（该周没标）');
            if (w.feel_avg != null) {
              lines.push(`平均感受 ${w.feel_avg} / 5（${w.feel_n} 次标注）`);
            }
            lines.push(`本周跑步 ${w.runs || 0} 次`);
            return lines.join('<br/>');
          },
        }),
        series: [
          { name: '跑步次数', type: 'bar', yAxisIndex: 1, barMaxWidth: 18,
            data: wks.map((w) => w.runs || 0),
            itemStyle: { color: colors.ramp[1], opacity: 0.45 } },
          { name: '平均 RPE', type: 'line', yAxisIndex: 0, connectNulls: false,
            data: wks.map((w) => w.rpe_avg),
            lineStyle: { color: colors.accent, width: 2 },
            itemStyle: { color: colors.accent }, symbolSize: 6 },
          { name: '平均感受', type: 'line', yAxisIndex: 0, connectNulls: false,
            // 感受 1-5 乘 2 落到 1-10 轴：同图对照，悬停提示里给原始值
            data: wks.map((w) => (w.feel_avg == null ? null
              : +(w.feel_avg * 2).toFixed(1))),
            lineStyle: { color: colors.kindM, width: 1.5, type: 'dashed' },
            itemStyle: { color: colors.kindM }, symbolSize: 5 },
        ],
      };
    },
    // 选定配速档的近 16 周心率趋势。三层信息叠加，解决「图不直观、有断点、
    // 有跨度没数据」：
    //   浅柱（右轴）= 当周该档实际段数 —— 哪周真练了一眼可见
    //   小圆点     = 当周实测心率（≥2 段才出点，稀疏但真实）
    //   粗实线     = 4 周滚动均值（该周及之前 3 周距离加权）—— 主曲线，不断点
    // 再叠一条**首末 4 周均值之间的色带**：带高即变化量，带色即方向（绿=进步），
    // 比两条细横线直观——「好没好」不靠读数字，靠看出来。
    paceBinOption(colors) {
      const wks = this.paceWeekly.weeks || [];
      const roll = this.paceWeekly.roll_weeks || 4;
      const bin = this.selPaceBin();
      const ax = baseAxis(colors);
      const maxSegs = bin ? Math.max(2, ...bin.week_segs) : 2;
      const trendColor = this.paceBinTrendColor(bin);
      // 「没变」时色带用中性灰：红色带会被读成「退步」，而这里只是没变化
      const bandColor = this.paceBinTrend(bin) === 'flat' ? colors.muted : trendColor;
      const out = {
        grid: { left: 46, right: 34, top: 34, bottom: 30 },
        legend: {
          top: 0, itemWidth: 12, itemHeight: 8, itemGap: 10,
          textStyle: { color: colors.muted, fontSize: 10 },
          data: ['滚动4周均值', '当周实测', '当周段数'],
        },
        xAxis: {
          ...ax, type: 'category', data: wks.map((w) => w.label),
          // 16 个周标签在窄卡片里会重叠 → 隔一个显示一个
          axisLabel: { ...ax.axisLabel, interval: wks.length > 10 ? 1 : 0 },
        },
        // scale: 心率只在 120–155 波动，从 0 起画会把变化压平看不出来
        yAxis: [
          { ...ax, type: 'value', name: '平均心率 (bpm)', nameTextStyle: { color: colors.muted, fontSize: 10 }, scale: true },
          { ...ax, type: 'value', name: '段数', nameTextStyle: { color: colors.muted, fontSize: 10 },
            min: 0, max: maxSegs, minInterval: 1, splitLine: { show: false } },
        ],
        tooltip: tooltip(colors, {
          formatter: (ps) => {
            if (!ps || !ps.length || !bin) return '';
            const i = ps[0].dataIndex;
            const w = wks[i];
            const segs = bin.week_segs[i] || 0;
            const lines = [
              `<b>${w.start} ~ ${w.end}</b>${w.partial ? '（本周进行中）' : ''}`,
            ];
            if (bin.hr[i] != null) {
              lines.push(`当周实测 <b>${bin.hr[i].toFixed(0)}</b> bpm（${segs} 段）`);
            } else {
              lines.push(segs ? `当周 ${segs} 段（不足 2 段，不出实测点）` : '当周该档没跑');
            }
            if (bin.hr_roll[i] != null) {
              lines.push(`${roll} 周滚动 <b>${bin.hr_roll[i].toFixed(0)}</b> bpm`);
            }
            // 只在末端 4 周提示与最初 4 周的差——中间点比这个没有意义
            if (bin.early_hr != null && bin.hr_roll[i] != null
                && i >= wks.length - 4) {
              const d = bin.hr_roll[i] - bin.early_hr;
              lines.push(`较最初 4 周 <b>${d >= 0 ? '+' : ''}${d.toFixed(0)}</b> bpm`);
            }
            return lines.join('<br/>');
          },
        }),
        series: [
          { // 当周段数：浅柱垫底，右轴。跨周空白一眼可见，这是「不直观」的根子
            type: 'bar', name: '当周段数', yAxisIndex: 1, z: 1,
            data: bin ? bin.week_segs : [],
            barMaxWidth: 14,
            itemStyle: { color: colors.muted, opacity: 0.18 },
            emphasis: { itemStyle: { opacity: 0.38 } },
          },
          { // 当周实测：只画点不连线（点少时连线会伪造出并不存在的趋势）。
            // 空心圆 + 趋势色描边：从属于主曲线，不再像「脏点」
            type: 'line', name: '当周实测', z: 3,
            data: bin ? bin.hr : [],
            connectNulls: false,
            lineStyle: { width: 0 },
            symbol: 'circle', symbolSize: 6,
            itemStyle: { color: colors.bg, borderColor: colors.muted,
                         borderWidth: 1.5, opacity: 0.9 },
          },
          { // 主曲线：4 周滚动距离加权均值，稀疏周也能连成一条
            type: 'line', name: '滚动4周均值', z: 4,
            data: bin ? bin.hr_roll : [],
            connectNulls: false,   // 窗口内不足 2 段仍断，但不假装连续
            lineStyle: { color: trendColor, width: 2.5 },
            itemStyle: { color: trendColor },
            symbolSize: 6,
            areaStyle: { opacity: 0.07, color: trendColor },
          },
        ],
      };
      // 首/末各 4 周的距离加权均值：色带填在两线之间，带高 = 变化量，
      // 带色 = 方向；两条边线保留（虚线 = 最初，实线 = 最近）作为标尺。
      if (bin && bin.early_hr != null && bin.recent_hr != null) {
        const d = bin.drop;
        const deltaTxt = d == null ? ''
          : `${d <= -1 ? '↓' : d >= 1 ? '↑' : '≈'} ${Math.abs(d).toFixed(0)} bpm`;
        out.series[2].markArea = {
          silent: true,
          itemStyle: { color: bandColor, opacity: 0.12 },
          label: {
            show: !!deltaTxt, position: 'insideTopRight',
            color: bandColor, fontSize: 11, fontWeight: 'bold',
            formatter: deltaTxt,
          },
          data: [[{ yAxis: bin.early_hr }, { yAxis: bin.recent_hr }]],
        };
        out.series[2].markLine = {
          silent: true, symbol: 'none',
          label: { position: 'insideEndTop', color: colors.muted, fontSize: 10,
                   formatter: (p) => p.name },
          data: [
            { yAxis: bin.early_hr, name: `最初4周 ${bin.early_hr.toFixed(0)}`,
              lineStyle: { color: colors.muted, type: 'dashed', width: 1.5 } },
            { yAxis: bin.recent_hr, name: `最近4周 ${bin.recent_hr.toFixed(0)}`,
              lineStyle: { color: trendColor, type: 'solid', width: 1.5 } },
          ],
        };
      }
      return out;
    },
    // 切换配速档时只重画这一张图，不必把同页 5 张图全部重建
    renderPaceBinChart() {
      const el = document.getElementById('health-pacehr-chart');
      if (!el || !el.offsetWidth) return;   // 页面隐藏中：shown() 会补渲染
      disposeChart(el);
      initChart(el, this.paceBinOption(chartColors()));
    },
    fmtPace(s) {
      if (!s) return '—';
      // 先四舍五入再拆分，避免 59.6s 进位出「4:60」
      const t = Math.round(s), m = Math.floor(t / 60), sec = t % 60;
      return `${m}:${String(sec).padStart(2, '0')}/km`;
    },
  }));
}
