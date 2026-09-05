import { call, tryCall } from '../api.js';
import { baseAxis, chartColors, disposeChart, initChart, resizeIn, tooltip } from '../charts.js';

const HTML = `
<div class="flex mb8">
  <div class="flex">
    <label class="muted">时间范围</label>
    <select x-model="range" @change="load()" style="width:auto">
      <option value="7">近 7 天</option>
      <option value="30">近 30 天</option>
      <option value="90">近 90 天</option>
      <option value="0">全部</option>
    </select>
    <label class="muted">来源</label>
    <select x-model="sourceFilter" @change="load()" style="width:auto">
      <option value="">全部</option>
      <option value="fit">FIT 导入</option>
      <option value="csv">CSV 导入</option>
      <option value="garmin">Garmin 同步</option>
      <option value="demo">演示数据</option>
      <option value="manual">手动补录</option>
    </select>
  </div>
  <div class="spacer"></div>
  <button class="btn primary" @click="importFiles()">📥 导入 FIT/CSV</button>
</div>
<div class="card">
  <table>
    <thead><tr>
      <th>日期</th><th>名称</th><th class="num">距离(km)</th><th class="num">时长</th>
      <th class="num">配速</th><th class="num">平均心率</th><th>来源</th>
    </tr></thead>
    <tbody>
      <template x-for="a in list" :key="a.id">
        <tr style="cursor:pointer" @click="openDetail(a.id)">
          <td class="num" x-text="fmtDate(a.start_ts)"></td>
          <td x-text="a.name"></td>
          <td class="num" x-text="a.distance_m ? (a.distance_m/1000).toFixed(2) : '—'"></td>
          <td class="num" x-text="fmtTime(a.duration_s)"></td>
          <td class="num" x-text="fmtPace(a.avg_pace_s_km)"></td>
          <td class="num" x-text="a.avg_hr ? a.avg_hr.toFixed(0) : '—'"></td>
          <td>
            <span class="badge soft" x-text="a.source"></span>
            <span class="badge" :class="kindClass(a.workout)" x-text="a.workout ? a.workout.label : ''"></span>
          </td>
        </tr>
      </template>
      <tr x-show="!list.length"><td colspan="7" class="muted">暂无活动，点击右上角导入文件</td></tr>
    </tbody>
  </table>
</div>

<!-- 弹窗遮罩必须用 x-if：本环境 WebView2 的 x-show 隐藏机制有缺陷，
     关闭后遮罩可能残留在 DOM 上导致暗屏卡死 -->
<template x-if="detail">
<div class="modal-mask" @click.self="detail=null">
  <div class="modal">
    <div class="flex">
      <h3 x-text="detail ? detail.name : ''"></h3>
      <div class="spacer"></div>
      <button class="btn small" @click="detail=null">✕</button>
    </div>
    <div class="grid cols-4 mb8">
      <div><div class="muted">距离</div><b x-text="detail ? (detail.distance_m/1000).toFixed(2) + ' km' : ''"></b></div>
      <div><div class="muted">时长</div><b x-text="detail ? fmtTime(detail.duration_s) : ''"></b></div>
      <div><div class="muted">平均配速</div><b x-text="detail ? fmtPace(detail.avg_pace_s_km) : ''"></b></div>
      <div><div class="muted">平均心率</div><b x-text="detail && detail.avg_hr ? detail.avg_hr.toFixed(0) : '—'"></b></div>
    </div>
    <div class="grid cols-4 mb8">
      <div><div class="muted">步频</div><b x-text="detail && detail.avg_cadence ? detail.avg_cadence.toFixed(0) + ' spm' : '—'"></b></div>
      <div><div class="muted">步幅</div><b x-text="detail && detail.stride_length_m ? detail.stride_length_m.toFixed(2) + ' m' : '—'"></b></div>
      <div><div class="muted">有氧训练效果</div><b x-text="detail && detail.aerobic_te != null ? detail.aerobic_te.toFixed(1) : '—'"></b></div>
      <div><div class="muted">训练负荷</div><b x-text="detail && detail.exercise_load != null ? detail.exercise_load.toFixed(0) : '—'"></b></div>
    </div>
    <p class="mb8" x-show="detail && detail.workout && detail.workout.label">
      <span class="badge" :class="kindClass(detail.workout)" x-text="detail.workout.label"></span>
      <span class="muted" x-show="detail.workout && detail.workout.hr_pct != null"
            x-text="'（平均心率 ' + (detail.workout.hr_pct * 100).toFixed(0) + '% HRmax）'"></span>
    </p>
    <template x-if="detail && detail.structure && detail.structure.length > 1">
      <table class="mb8">
        <thead><tr><th>分段</th><th class="num">距离(m)</th><th class="num">用时</th><th class="num">配速</th><th class="num">平均心率</th></tr></thead>
        <tbody>
          <template x-for="(s, i) in detail.structure" :key="i">
            <tr>
              <td x-text="segLabel(detail, i)"></td>
              <td class="num" x-text="s.distance_m || '—'"></td>
              <td class="num" x-text="s.duration_s ? fmtTime(s.duration_s) : '—'"></td>
              <td class="num" x-text="s.pace_s_km ? fmtPace(s.pace_s_km) : '—'"></td>
              <td class="num" x-text="s.avg_hr ? s.avg_hr.toFixed(0) : '—'"></td>
            </tr>
          </template>
        </tbody>
      </table>
    </template>
    <template x-if="detail && detail.samples && detail.samples.length > 5">
      <div>
        <div class="grid cols-2">
          <div class="chart-sm" id="act-pace-chart"></div>
          <div class="chart-sm" id="act-hr-chart"></div>
        </div>
        <div class="muted">每 5 秒采样曲线（配速 / 心率，分图展示避免双轴）</div>
      </div>
    </template>
    <template x-if="detail && detail.laps && detail.laps.length > 1">
      <table class="mt8">
        <thead><tr><th>圈</th><th class="num">距离(m)</th><th class="num">用时</th><th class="num">平均心率</th></tr></thead>
        <tbody>
          <template x-for="(lap, i) in detail.laps" :key="i">
            <tr>
              <td x-text="i+1"></td>
              <td class="num" x-text="lap.distance_m ? lap.distance_m.toFixed(0) : '—'"></td>
              <td class="num" x-text="fmtTime(lap.elapsed_s)"></td>
              <td class="num" x-text="lap.avg_hr ? lap.avg_hr.toFixed(0) : '—'"></td>
            </tr>
          </template>
        </tbody>
      </table>
    </template>
  </div>
</div>
</template>`;

export function initActivities() {
  const sec = document.getElementById('page-activities');
  sec.innerHTML = HTML;

  window.Alpine.data('activitiesPage', () => ({
    range: '30',
    sourceFilter: '',
    list: [],
    detail: null,

    async init() {
      await this.load();
    },
    shown() {
      resizeIn(document.getElementById('page-activities'));
      if (this.detail) this.renderDetailCharts();
    },
    async load() {
      const start = this.range === '0' ? null : this.daysAgo(parseInt(this.range));
      const { ok, data, error } = await tryCall('list_activities', start, null,
        this.sourceFilter || null, 500, 0);
      if (!ok) { this.$dispatch('toast', { text: '读取活动失败: ' + error }); return; }
      this.list = data || [];
    },
    daysAgo(n) {
      const d = new Date(); d.setDate(d.getDate() - n);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },
    async importFiles() {
      const { ok, data, error } = await tryCall('open_file_dialog');
      if (!ok || !data || !data.length) return;
      const r = await tryCall('import_files', data);
      if (!r.ok) { this.$dispatch('toast', { text: '导入失败: ' + r.error }); return; }
      const m = `导入完成：新增 ${r.data.imported} 条，跳过 ${r.data.skipped} 条` +
        (r.data.errors.length ? `，失败 ${r.data.errors.length} 条` : '');
      this.$dispatch('toast', { text: m });
      await this.load();
    },
    async openDetail(id) {
      const { ok, data, error } = await tryCall('get_activity', id);
      if (!ok) { this.$dispatch('toast', { text: error }); return; }
      this.detail = data;
      await this.$nextTick();
      this.renderDetailCharts();
    },
    renderDetailCharts() {
      if (!this.detail) return;
      const colors = chartColors();
      const samples = this.detail.samples || [];
      const t = samples.map((s) => s.t_offset_s);
      const pace = samples.map((s) => (s.speed_mps ? 1000 / s.speed_mps : null));
      const hr = samples.map((s) => s.hr);
      const min = Math.floor((Math.min(...t.filter((v) => v != null)) || 0) / 60);
      const max = Math.ceil((Math.max(...t.filter((v) => v != null)) || 0) / 60);
      const mk = () => ({
        grid: { left: 44, right: 12, top: 24, bottom: 24 },
        xAxis: { ...baseAxis(colors), type: 'value', min, max, name: '分钟' },
        yAxis: { ...baseAxis(colors), type: 'value' },
        tooltip: tooltip(colors),
        series: [{ type: 'line', showSymbol: false, lineStyle: { width: 2 }, data: [] }],
      });
      const paceChart = mk();
      paceChart.series[0].name = '配速 s/km';
      paceChart.series[0].data = t.map((x, i) => [x, pace[i]]);
      paceChart.series[0].lineStyle.color = colors.accent;
      paceChart.series[0].itemStyle = { color: colors.accent };
      const hrChart = mk();
      hrChart.series[0].name = '心率 bpm';
      hrChart.series[0].data = t.map((x, i) => [x, hr[i]]);
      hrChart.series[0].lineStyle.color = colors.kindI;
      hrChart.series[0].itemStyle = { color: colors.kindI };
      const el1 = document.getElementById('act-pace-chart');
      const el2 = document.getElementById('act-hr-chart');
      if (el1 && el2) {
        disposeChart(el1);
        disposeChart(el2);
        initChart(el1, paceChart);
        initChart(el2, hrChart);
      }
    },
    fmtDate(ts) {
      const d = new Date(ts * 1000);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },
    kindClass(w) {
      return w && w.kind ? `kind-${w.kind}` : '';
    },
    segLabel(detail, i) {
      const labels = { sprint: '冲刺段', fast: '快跑段', walk: '休息·走路', jog: '休息·慢跑',
        stand: '休息·静止', warmup: '热身/冷身' };
      const k = detail && detail.workout && detail.workout.seg_kinds ? detail.workout.seg_kinds[i] : null;
      if (k && labels[k]) return labels[k];
      const s = detail && detail.structure ? detail.structure[i] : null;
      return s ? ({ work: '跑段', rest: '休息', recovery: '热身/冷身' }[s.type] || s.type) : '';
    },
    fmtTime(s) {
      if (!s) return '—';
      s = Math.round(s);
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
               : `${m}:${String(sec).padStart(2, '0')}`;
    },
    fmtPace(p) {
      if (!p) return '—';
      // 先四舍五入再拆分，避免 59.6s 进位出「4:60」
      const t = Math.round(p), m = Math.floor(t / 60), sec = t % 60;
      return `${m}:${String(sec).padStart(2, '0')}/km`;
    },
  }));
}
