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
        <div class="muted">秒级采样曲线（配速 / 心率，分图展示避免双轴；滚轮缩放、拖拽平移可看细节，暂停时段断线）</div>
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
    async syncRefresh() {
      // 同步刚结束：列表必须立即重拉（本页无 _lastLoad 防抖，load 直跑）；
      // 若详情正打开，重拉详情让 Garmin 回填后的样本/曲线也即时更新
      await this.load();
      if (this.detail) await this.openDetail(this.detail.id);
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
      if (samples.length <= 5) return;  // 与模板渲染条件一致
      const MAX_PTS = 2000;   // 每图抽稀上限：长活动秒级样本可达数千，全量点渲染卡顿
      const MIN_SPEED = 0.5;  // <0.5m/s（>33:20/km）视作静止/暂停/GPS 漂移 → 断线而非假快/Infinity
      // X 轴与数据统一为分钟。旧 bug：X 数据是秒、坐标域 min/max 却除以 60 当分钟，
      // ECharts 把越界点全部裁剪 → 曲线只剩开头几十秒
      const toMin = (s) => (s == null ? null : s / 60);
      const paceXY = this.downsample(samples.map((s) => [
        toMin(s.t_offset_s),
        s.speed_mps != null && s.speed_mps > MIN_SPEED ? 1000 / s.speed_mps : null,
      ]), MAX_PTS);
      const hrXY = this.downsample(samples.map((s) => [toMin(s.t_offset_s), s.hr ?? null]), MAX_PTS);

      const xAxis = { ...baseAxis(colors), type: 'value', name: '分钟' };
      xAxis.axisLabel = { ...xAxis.axisLabel, formatter: (v) => this.fmtClock(v * 60) };
      const paceY = { ...baseAxis(colors), type: 'value', scale: true, inverse: true };  // 快在上
      paceY.axisLabel = { ...paceY.axisLabel, formatter: (v) => this.fmtClock(v) };
      const hrY = { ...baseAxis(colors), type: 'value', scale: true };
      const mk = (yAxis, name, color, fmtVal) => ({
        grid: { left: 54, right: 16, top: 26, bottom: 24 },
        xAxis,
        yAxis,
        tooltip: tooltip(colors, {
          formatter: (params) => {
            const p = params && params[0];
            if (!p || !p.value || p.value[1] == null) return '';  // 断点（暂停）无提示
            return `<b>${this.fmtClock(p.value[0] * 60)}</b><br/>${p.marker}${name} ${fmtVal(p.value[1])}`;
          },
        }),
        // 滚轮缩放 + 拖拽平移：秒级采样全幅铺开，细节由用户缩放看（不裁剪丢数据）
        dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
        series: [{
          type: 'line', name,
          showSymbol: false, connectNulls: false,  // 暂停/静止段断线，不连线
          lineStyle: { width: 2, color }, itemStyle: { color },
          data: [],
        }],
      });
      const paceChart = mk(paceY, '配速', colors.accent, (v) => this.fmtPace(v));
      paceChart.series[0].data = paceXY;
      const hrChart = mk(hrY, '心率', colors.kindI, (v) => `${Math.round(v)} bpm`);
      hrChart.series[0].data = hrXY;
      const el1 = document.getElementById('act-pace-chart');
      const el2 = document.getElementById('act-hr-chart');
      if (el1 && el2) {
        disposeChart(el1);
        disposeChart(el2);
        initChart(el1, paceChart);
        initChart(el2, hrChart);
      }
    },
    /** 分块 LTTB 抽稀：按「有效值连续段」分配点数预算——null 断点两侧独立抽稀，
        暂停间隙不会被抹成连线；每段保留首尾点与形态。 */
    downsample(xy, maxPts) {
      if (xy.length <= maxPts) return xy;
      const runs = [];
      let cur = [];
      for (const p of xy) {
        if (p[1] == null) {
          if (cur.length) { runs.push(cur); cur = []; }
        } else cur.push(p);
      }
      if (cur.length) runs.push(cur);
      const total = runs.reduce((n, r) => n + r.length, 0);
      const out = [];
      for (const r of runs) {
        const budget = Math.min(r.length, Math.max(2, Math.round(maxPts * r.length / total)));
        out.push(...this.lttb(r, budget));
      }
      return out;
    },
    /** LTTB（largest-triangle-three-buckets）：按候选点与相邻平均点构成的三角形
        面积挑点，保极值形态，复杂度 O(n)（抽稀只发生一次，不做逐段递归）。 */
    lttb(pts, m) {
      const n = pts.length;
      if (m >= n) return pts;
      if (m < 3) return [pts[0], pts[n - 1]];
      const out = [pts[0]];
      const step = (n - 2) / (m - 2);
      let a = 0;
      for (let i = 0; i < m - 2; i++) {
        const ra = Math.floor(i * step) + 1;
        const rb = Math.min(Math.floor((i + 1) * step) + 1, n - 1);
        const sa = Math.floor((i + 1) * step) + 1;
        const sb = Math.min(Math.floor((i + 2) * step) + 1, n);
        let ax = 0, ay = 0, k = 0;
        for (let j = sa; j < sb; j++) { ax += pts[j][0]; ay += pts[j][1]; k++; }
        if (!k) continue;
        ax /= k; ay /= k;
        const x0 = pts[a][0], y0 = pts[a][1];
        let best = -1, bi = -1;
        for (let j = ra; j < rb; j++) {
          const area = Math.abs((x0 - pts[j][0]) * (ay - y0) - (x0 - ax) * (pts[j][1] - y0));
          if (area > best) { best = area; bi = j; }
        }
        if (bi >= 0) { out.push(pts[bi]); a = bi; }
      }
      out.push(pts[n - 1]);
      return out;
    },
    /** 秒 → mm:ss / h:mm:ss（0 也输出 "0:00"，与 fmtTime 的 '—' 占位语义区分） */
    fmtClock(s) {
      s = Math.round(s);
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
               : `${m}:${String(sec).padStart(2, '0')}`;
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
