import { tryCall } from '../api.js';
import { baseAxis, chartColors, disposeChart, initChart, resizeIn, tooltip } from '../charts.js';

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
  <div class="card"><h3>同配速不同时期平均心率对照</h3>
    <p class="muted">配速按 30s/km 分档；同一档位下，越近的时期心率越低 = 有氧能力进步</p>
    <p class="pacehr-summary" x-show="paceBins.summary && paceBins.summary.note"
       x-text="'📊 ' + paceBins.summary.note"></p>
    <p class="muted">虚线 = 最初时期，实线加粗 = 最近时期</p>
    <div class="chart" id="health-pacehr-chart"></div>
  </div>
  <div class="card"><h3>配速-心率明细</h3>
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
    paceBins: { bins: [], periods: [] },

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
      const start = this.daysAgo(parseInt(this.range));
      const { ok, data, error } = await tryCall('list_health', start);
      if (!ok) { this.$dispatch('toast', { text: '读取健康数据失败: ' + error }); return; }
      this.rows = data || [];
      // 配速-心率对照取一年窗口：数据越多时期对照越有说服力
      const ph = await tryCall('list_weekly_pace_hr', 365);
      this.paceHr = ph.ok ? (ph.data || []) : [];
      const pb = await tryCall('list_pace_bin_hr', 365);
      this.paceBins = pb.ok ? (pb.data || { bins: [], periods: [] }) : { bins: [], periods: [] };
      await this.$nextTick();
      this.renderCharts();
    },
    daysAgo(n) {
      const d = new Date(); d.setDate(d.getDate() - n);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },
    hrvLabel(s) { return { balanced: '平衡', unbalanced: '不平衡', low: '偏低' }[s] || s; },
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
      // 同配速不同时期心率对照：x=配速 30s 梯度档（左快右慢，档位全部
      // 展示保证梯度均匀），每条线 = 一个时期，最新时期在最前（最直观）。
      // 同一档位看线的高低差即是有氧能力变化。
      const bins = this.paceBins.bins || [];
      const periods = this.paceBins.periods || [];
      const paceBinOpt = mk({
        // 底部图例需额外留白，否则图例与 x 轴配速档标签重叠
        grid: { left: 40, right: 12, top: 24, bottom: 46 },
        xAxis: {
          ...baseAxis(colors), type: 'category',
          data: bins.map((b) => this.fmtPaceAxis(b.start_s)),
          // 30s 梯度档全部展示，隐藏部分标签会让梯度看起来忽疏忽密
          axisLabel: { ...baseAxis(colors).axisLabel, interval: 0 },
        },
        yAxis: { ...baseAxis(colors), type: 'value', name: '平均心率 (bpm)' },
        legend: { bottom: 0, textStyle: { color: colors.muted, fontSize: 10 }, itemGap: 10 },
        tooltip: tooltip(colors, {
          trigger: 'item',
          formatter: (p) => {
            const pr = periods[p.seriesIndex];
            const b = bins[p.dataIndex];
            if (!pr || !b) return '';
            const hr = pr.hr[p.dataIndex];
            if (hr == null) return '';
            const latest = periods[0].hr[p.dataIndex];
            const lines = [`<b>${pr.start} ~ ${pr.end}</b><br/>配速 ${this.fmtPaceAxis(b.start_s)}–`
              + `${this.fmtPaceAxis(b.end_s)} /km<br/>平均心率 ${hr.toFixed(0)} bpm<br/>`
              + `跑量 ${pr.distance_km[p.dataIndex]} km · ${pr.runs[p.dataIndex]} 次`];
            if (p.seriesIndex > 0 && latest != null) {
              const d = hr - latest;
              lines.push(`较最近时期 <b>${d >= 0 ? '+' : ''}${d.toFixed(0)}</b> bpm`);
            }
            return lines.join('<br/>');
          },
        }),
        series: periods.map((pr, i) => {
          const isNewest = i === 0;   // 最新在前：加粗高亮 + 面积底
          const isOldest = i === periods.length - 1;  // 最初：灰色虚线
          const col = colors.period[i % colors.period.length];
          return {
            type: 'line', name: pr.label, data: pr.hr,
            connectNulls: false,
            lineStyle: isOldest
              ? { color: colors.muted, width: 2, type: 'dashed' }
              : { color: col, width: isNewest ? 4 : 2 },
            itemStyle: { color: isOldest ? colors.muted : col },
            // 只有最新时期画点：多条线在同一档位的符号会叠成一团
            showSymbol: isNewest, symbolSize: isNewest ? 7 : 0,
            z: isNewest ? 5 : 1,
            areaStyle: isNewest ? { opacity: 0.10, color: col } : undefined,
          };
        }),
      });
      const els = ['health-hrv-chart', 'health-rhr-chart', 'health-sleep-chart',
                   'health-stress-chart', 'health-pacehr-chart'];
      els.forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        disposeChart(el);
        initChart(el, [hrvChart, rhrChart, sleepChart, stressChart, paceBinOpt][i]);
      });
    },
    fmtPace(s) {
      if (!s) return '—';
      // 先四舍五入再拆分，避免 59.6s 进位出「4:60」
      const t = Math.round(s), m = Math.floor(t / 60), sec = t % 60;
      return `${m}:${String(sec).padStart(2, '0')}/km`;
    },
    fmtPaceAxis(v) {
      if (v == null) return '';
      const t = Math.round(v), m = Math.floor(t / 60), sec = t % 60;
      return `${m}:${String(sec).padStart(2, '0')}`;
    },
  }));
}
