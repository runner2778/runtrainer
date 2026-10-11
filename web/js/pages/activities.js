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
  <button class="btn" @click="recognizeShot()">🖼️ 截图识别</button>
  <button class="btn" @click="openForm()">✏️ 手动录入</button>
  <button class="btn primary" @click="importFiles()">📥 导入 FIT/CSV</button>
</div>
<div class="card">
  <table>
    <thead><tr>
      <th>日期</th><th>名称</th><th class="num">距离(km)</th><th class="num">时长</th>
      <th class="num">配速</th><th class="num">平均心率</th><th class="num">感受/用力</th><th>来源</th>
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
          <td class="num" x-text="subjText(a)"></td>
          <td>
            <span class="badge soft" x-text="a.source"></span>
            <span class="badge" :class="kindClass(a.workout)" x-text="a.workout ? a.workout.label : ''"></span>
          </td>
        </tr>
      </template>
      <tr x-show="!list.length"><td colspan="8" class="muted">暂无活动，点击右上角导入文件</td></tr>
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
      <button class="btn small" @click="openForm(null, detail)">✏️ 编辑</button>
      <button class="btn small"
              x-show="detail && detail.manual"
              :class="_confirmDel ? 'primary' : ''"
              @click="deleteActivity()">
        <span x-text="_confirmDel ? '再点一次确认删除' : '🗑 删除'"></span>
      </button>
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
    <!-- 主观数据：手动录入/截图识别/佳明标签三处同权，都落在这两列 -->
    <div class="grid cols-3 mb8">
      <div><div class="muted">主观感受</div><b x-text="feelText(detail)"></b></div>
      <div><div class="muted">用力程度 RPE</div><b x-text="rpeText(detail)"></b></div>
      <div><div class="muted">爬升</div><b x-text="detail && detail.elevation_gain_m ? detail.elevation_gain_m.toFixed(0) + ' m' : '—'"></b></div>
    </div>
    <p class="mb8" x-show="detail && detail.note">
      <span class="muted">备注：</span><span x-text="detail ? detail.note : ''"></span>
    </p>
    <p class="mb8" x-show="detail && detail.workout && detail.workout.label">
      <span class="badge" :class="kindClass(detail.workout)" x-text="detail.workout.label"></span>
      <span class="muted" x-show="detail.workout && detail.workout.hr_pct != null"
            x-text="zoneStatText()"></span>
    </p>
    <!-- 强度定位条：本次训练平均强度相对 LT1/LT2 的位置（仅连续强度课有意义） -->
    <template x-if="detail && detail.workout && zoneKindOf(detail.workout) && detail.workout.hr_pct != null">
      <div class="mt8">
        <div x-html="zoneStripHtml(detail.workout)"></div>
        <p class="muted mt4" style="font-size:12px" x-text="zoneDotText(detail.workout)"></p>
      </div>
    </template>
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
</template>

<!-- 手动录入 / 编辑表单（同样必须 x-if） -->
<template x-if="form">
<div class="modal-mask" @click.self="closeForm()">
  <div class="modal wide">
    <div class="flex">
      <h3 x-text="form.mode === 'edit' ? '编辑活动' : '手动录入活动'"></h3>
      <div class="spacer"></div>
      <button class="btn small" @click="closeForm()">✕</button>
    </div>

    <div class="flex mb8" x-show="form.mode === 'new'">
      <button class="btn small" @click="recognizeShot()" :disabled="form.busy">
        🖼️ 从截图识别填写
      </button>
      <span class="muted" style="font-size:12px">
        识别结果只做预填，请核对后再保存（OCR 有误差，不会直接入库）
      </span>
    </div>

    <template x-if="form.ocr">
      <div class="ocr-panel mb8">
        <div class="flex">
          <span class="badge soft">识别原文</span>
          <div class="spacer"></div>
          <button class="btn small" @click="form.ocr=null">收起</button>
        </div>
        <pre class="ocr-raw" x-text="form.ocr.raw_text"></pre>
        <template x-if="form.preview">
          <img class="ocr-preview" :src="form.preview" alt="识别截图">
        </template>
        <p class="muted" style="font-size:12px" x-show="ocrMatchedText()"
           x-text="'识别到：' + ocrMatchedText()"></p>
        <p class="ocr-warn" x-show="(form.ocr.warnings || []).length"
           x-text="(form.ocr.warnings || []).join('；')"></p>
      </div>
    </template>

    <div class="form-grid">
      <div class="form-row">
        <label>名称</label>
        <input type="text" x-model="form.f.name" placeholder="如：早上轻松跑">
      </div>
      <div class="form-row">
        <label>开始时间</label>
        <input type="datetime-local" x-model="form.f.start_local">
      </div>
      <div class="form-row">
        <label>距离 (km)</label>
        <input type="text" x-model="form.f.distance_km" placeholder="如 10.5">
      </div>
      <div class="form-row">
        <label>用时</label>
        <input type="text" x-model="form.f.duration" placeholder="H:MM:SS 或 MM:SS 或分钟">
      </div>
      <div class="form-row">
        <label>平均心率 (bpm)</label>
        <input type="text" x-model="form.f.avg_hr">
      </div>
      <div class="form-row">
        <label>最大心率 (bpm)</label>
        <input type="text" x-model="form.f.max_hr">
      </div>
      <div class="form-row">
        <label>步频 (步/分)</label>
        <input type="text" x-model="form.f.avg_cadence">
      </div>
      <div class="form-row">
        <label>步幅 (m)</label>
        <input type="text" x-model="form.f.stride_length_m" placeholder="留空则由配速与步频推算">
      </div>
      <div class="form-row">
        <label>累计爬升 (m)</label>
        <input type="text" x-model="form.f.elevation_gain_m" placeholder="影响水平预估的坡度折算">
      </div>
      <div class="form-row">
        <label>热量 (kcal)</label>
        <input type="text" x-model="form.f.calories">
      </div>
      <div class="form-row">
        <label>主观感受</label>
        <select x-model="form.f.feel">
          <option value="">未填</option>
          <option value="1">1 很轻松</option>
          <option value="2">2 轻松</option>
          <option value="3">3 一般</option>
          <option value="4">4 吃力</option>
          <option value="5">5 非常吃力</option>
        </select>
      </div>
      <div class="form-row">
        <label>用力程度 RPE</label>
        <select x-model="form.f.rpe">
          <option value="">未填</option>
          <template x-for="i in 10" :key="i">
            <option :value="String(i)" x-text="i + rpeHint(i)"></option>
          </template>
        </select>
      </div>
    </div>
    <div class="form-row">
      <label>备注</label>
      <textarea rows="2" x-model="form.f.note" placeholder="天气、路线、身体反应…"></textarea>
    </div>
    <p class="muted" style="font-size:12px">
      用时填「45」按 45 分钟算，「52:36」按 52 分 36 秒算。距离与用时至少填一个；
      两者都填时配速按它们算。主观数据会参与水平预估（标了「很轻松」的强度课影响力会被调低）。
    </p>
    <div class="flex mt8">
      <div class="spacer"></div>
      <button class="btn" @click="closeForm()">取消</button>
      <button class="btn primary" @click="submitForm()" :disabled="form.busy"
              x-text="form.mode === 'edit' ? '保存修改' : '保存活动'"></button>
    </div>
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
    _confirmDel: false,
    // 手动录入/编辑表单：{mode, id, f, ocr, preview, busy}
    form: null,

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
        (r.data.errors.length ? `，失败 ${r.data.errors.length} 条` : '') +
        (r.data.plan_rebuilt ? `；水平已更新，课表配速已按新 VDOT ${r.data.plan_vdot} 同步` : '');
      this.$dispatch('toast', { text: m });
      await this.load();
      // 新活动/新 PB 会改水平预估与课表 → 通知其余页面（仪表盘/日历/目标…）
      // 立即重拉，返回那些页面时数据就是新的
      window.dispatchEvent(new Event('data-changed'));
    },
    async openDetail(id) {
      const { ok, data, error } = await tryCall('get_activity', id);
      if (!ok) { this.$dispatch('toast', { text: error }); return; }
      this.detail = data;
      this._confirmDel = false;
      await this.$nextTick();
      this.renderDetailCharts();
    },

    // ---- 主观数据（手动录入 / 截图识别 / 佳明标签三处同权）----
    /** 列表里的「感受/用力」短标签；两者都没填时显示占位符 */
    subjText(a) {
      const parts = [];
      if (a && a.feel) parts.push('感受 ' + a.feel + '/5');
      if (a && a.rpe) parts.push('RPE ' + a.rpe + '/10');
      return parts.length ? parts.join(' · ') : '—';
    },
    feelText(d) {
      if (!d || !d.feel) return '—';
      const names = { 1: '很轻松', 2: '轻松', 3: '一般', 4: '吃力', 5: '非常吃力' };
      return `${d.feel}/5（${names[d.feel] || '—'}）`;
    },
    rpeText(d) {
      if (!d || !d.rpe) return '—';
      return `${d.rpe}/10`;
    },
    /** RPE 档位提示：低档会被预估系统降权，所以直接标在选项上 */
    rpeHint(i) {
      if (i <= 3) return '（很轻松·预估降权）';
      if (i <= 5) return '（轻松·预估降权）';
      if (i <= 8) return '（正常）';
      return '（拼尽全力）';
    },

    // ---- 手动录入 / 编辑表单 ----
    openForm(prefill, fromDetail) {
      const f = fromDetail ? this._formFromRecord(fromDetail)
                           : this._blankForm();
      if (prefill) Object.assign(f, prefill);
      this.form = { mode: fromDetail ? 'edit' : 'new',
                    id: fromDetail ? fromDetail.id : null,
                    f, ocr: null, preview: null, busy: false };
    },
    closeForm() { this.form = null; },
    _blankForm() {
      return { name: '', start_local: this._localInput(), distance_km: '',
               duration: '', avg_hr: '', max_hr: '', avg_cadence: '',
               stride_length_m: '', elevation_gain_m: '', calories: '',
               feel: '', rpe: '', note: '' };
    },
    _formFromRecord(a) {
      const s = (v) => (v == null || v === '' ? '' : String(v));
      return {
        name: a.name || '',
        start_local: this._localInput(a.start_ts),
        distance_km: a.distance_m ? String(+(a.distance_m / 1000).toFixed(3)) : '',
        duration: a.duration_s ? this.fmtClock(a.duration_s) : '',
        avg_hr: a.avg_hr ? String(Math.round(a.avg_hr)) : '',
        max_hr: a.max_hr ? String(Math.round(a.max_hr)) : '',
        avg_cadence: a.avg_cadence ? String(Math.round(a.avg_cadence)) : '',
        stride_length_m: s(a.stride_length_m),
        elevation_gain_m: a.elevation_gain_m ? String(Math.round(a.elevation_gain_m)) : '',
        calories: s(a.calories),
        feel: a.feel ? String(a.feel) : '',
        rpe: a.rpe ? String(a.rpe) : '',
        note: a.note || '',
      };
    },
    /** 时间戳 → datetime-local 需要的本地「YYYY-MM-DDTHH:MM」 */
    _localInput(ts) {
      const d = ts ? new Date(ts * 1000) : new Date();
      const p = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
        + `T${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    /** 「45」按分钟、「52:36」按分秒、「1:25:30」按时分秒；解析不出返回 null */
    parseDuration(t) {
      const s = String(t == null ? '' : t).trim();
      if (!s) return null;
      const parts = s.split(':').map((x) => x.trim());
      if (parts.some((x) => x === '' || !Number.isFinite(Number(x)))) return null;
      const n = parts.map(Number);
      if (n.length === 1) return n[0] > 0 ? Math.round(n[0] * 60) : null;
      if (n.length === 2) return Math.round(n[0] * 60 + n[1]);
      if (n.length === 3) return Math.round(n[0] * 3600 + n[1] * 60 + n[2]);
      return null;
    },
    _numOrNull(v) {
      const s = String(v == null ? '' : v).trim();
      if (!s) return null;
      const n = Number(s);
      return Number.isFinite(n) ? n : null;
    },
    /** 表单 → 入库字段。留空的一律不带，避免编辑时把已有值抹成 null */
    buildFields() {
      const f = this.form.f;
      const km = this._numOrNull(f.distance_km);
      const out = {
        start_local: (f.start_local || '').trim(),
        distance_m: km != null ? km * 1000 : null,
        duration_s: this.parseDuration(f.duration),
        avg_hr: this._numOrNull(f.avg_hr),
        max_hr: this._numOrNull(f.max_hr),
        avg_cadence: this._numOrNull(f.avg_cadence),
        stride_length_m: this._numOrNull(f.stride_length_m),
        elevation_gain_m: this._numOrNull(f.elevation_gain_m),
        calories: this._numOrNull(f.calories),
        feel: this._numOrNull(f.feel),
        rpe: this._numOrNull(f.rpe),
        note: (f.note || '').trim(),
      };
      if ((f.name || '').trim()) out.name = f.name.trim();
      return out;
    },
    async submitForm() {
      if (!this.form || this.form.busy) return;
      const fields = this.buildFields();
      if (fields.distance_m == null && fields.duration_s == null) {
        this.$dispatch('toast', { text: '距离和用时至少要填一个' });
        return;
      }
      this.form.busy = true;
      try {
        if (this.form.mode === 'edit') {
          const id = this.form.id;
          // 编辑走部分更新：null 会被当成「清空」，所以只送真正填了的字段
          delete fields.start_local;
          for (const k of Object.keys(fields)) {
            if (fields[k] == null && k !== 'note') delete fields[k];
          }
          const { ok, error } = await tryCall('update_activity_fields', id, fields);
          if (!ok) { this.$dispatch('toast', { text: '保存失败: ' + error }); return; }
          this.closeForm();
          await this.load();
          if (this.detail && this.detail.id === id) await this.openDetail(id);
          this.$dispatch('toast', { text: '已保存修改' });
          window.dispatchEvent(new Event('data-changed'));
          return;
        }
        const { ok, data, error } = await tryCall('add_manual_activity', fields);
        if (!ok) { this.$dispatch('toast', { text: '录入失败: ' + error }); return; }
        this.closeForm();
        await this.load();
        const ps = data && data.pace_sync;
        let m = '已录入活动';
        if (ps && ps.changed) m += `；水平已更新，课表配速同步 ${ps.repaced} 处`;
        this.$dispatch('toast', { text: m });
        window.dispatchEvent(new Event('data-changed'));
      } finally {
        if (this.form) this.form.busy = false;
      }
    },

    // ---- 截图识别：只做预填，不直接入库 ----
    async recognizeShot() {
      if (!this.form || this.form.busy) return;
      this.form.busy = true;
      try {
        const { ok, data, error } = await tryCall('recognize_activity_screenshot', null);
        if (!ok) { this.$dispatch('toast', { text: '识别失败: ' + error, ms: 5000 }); return; }
        if (!data || data.cancelled) return;
        this.form.ocr = { raw_text: data.raw_text || '',
                          warnings: data.warnings || [],
                          matched: data.matched || {} };
        this.form.preview = data.preview_data_url || null;
        this.fillFromOcr(data.fields || {});
        const n = Object.keys(data.matched || {}).length;
        this.$dispatch('toast', { text: `识别到 ${n} 项，请核对后保存` });
      } finally {
        this.form.busy = false;
      }
    },
    /** 只覆盖识别到的项：用户先手填过、OCR 没读到的字段保持不变 */
    fillFromOcr(fl) {
      const f = this.form.f;
      const set = (k, v) => { if (v != null && v !== '') f[k] = String(v); };
      if (fl.distance_m != null) f.distance_km = String(+(fl.distance_m / 1000).toFixed(3));
      if (fl.duration_s != null) f.duration = this.fmtClock(fl.duration_s);
      set('avg_hr', fl.avg_hr != null ? Math.round(fl.avg_hr) : null);
      set('avg_cadence', fl.avg_cadence != null ? Math.round(fl.avg_cadence) : null);
      set('stride_length_m', fl.stride_length_m);
      set('calories', fl.calories);
      set('feel', fl.feel);
      set('rpe', fl.rpe);
      if (fl.name && !(f.name || '').trim()) f.name = fl.name;
    },
    ocrMatchedText() {
      const m = this.form && this.form.ocr ? this.form.ocr.matched : null;
      if (!m) return '';
      const names = { distance_m: '距离', duration_s: '用时', avg_pace_s_km: '配速',
        avg_hr: '心率', avg_cadence: '步频', stride_length_m: '步幅',
        calories: '热量', feel: '主观感受', rpe: '用力程度', name: '名称' };
      return Object.keys(m).filter((k) => names[k])
        .map((k) => `${names[k]}「${m[k]}」`).join('、');
    },

    async deleteActivity() {
      if (!this.detail) return;
      if (!this._confirmDel) { this._confirmDel = true; return; }
      const id = this.detail.id;
      const { ok, error } = await tryCall('delete_activity_record', id);
      if (!ok) { this.$dispatch('toast', { text: '删除失败: ' + error }); return; }
      this.detail = null;
      this._confirmDel = false;
      await this.load();
      this.$dispatch('toast', { text: '已删除该活动' });
      window.dispatchEvent(new Event('data-changed'));
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
    // ---- 强度定位（LT1/LT2 条带，第十四批）----
    zoneKindOf(w) {
      return w && ['recovery', 'easy', 'aerobic', 'high_aerobic', 'tempo', 'anaerobic'].includes(w.kind);
    },
    _zoneCol(k) {
      return { recovery: 'var(--kind-recovery)', easy: 'var(--kind-e)', aerobic: 'var(--kind-m)',
        high_aerobic: 'var(--kind-hi)', tempo: 'var(--kind-t)', anaerobic: 'var(--kind-i)' }[k] || 'var(--bg-hover)';
    },
    metricName() {
      const w = this.detail.workout;
      return w && w.hr_metric === 'hrr' ? '储备心率 %HRR' : '%HRmax';
    },
    zoneStatText() {
      const w = this.detail.workout;
      if (!w || w.hr_pct == null) return '';
      const bpm = this.detail && this.detail.avg_hr ? this.detail.avg_hr.toFixed(0) + ' bpm = ' : '';
      return '（平均心率 ' + bpm + (w.hr_pct * 100).toFixed(0) + '% ' + this.metricName() + '）';
    },
    zoneStripHtml(w) {
      // 横向条带：各区带按宽度比例着色，LT1/LT2 竖虚线 + 本次强度圆点定位
      const names = { recovery: '恢复', easy: '低有氧', aerobic: '中有氧',
        high_aerobic: '高有氧', tempo: '阈值', anaerobic: '无氧' };
      let segs = '', keys = '', acc = 0;
      for (const z of (w.zones || [])) {
        const wdt = Math.max(0.02, z.hi - z.lo);
        segs += `<div class="istrip-seg" style="flex:${wdt.toFixed(4)};background:${this._zoneCol(z.key)}"`
          + ` title="${z.name} ${Math.round(z.lo * 100)}–${Math.round(Math.min(z.hi, 1) * 100)}%"></div>`;
        const prev = acc; acc += wdt;
        keys += `<span class="istrip-ck" style="background:${this._zoneCol(z.key)}"></span>${names[z.key] || z.name}`;
        if (w.lt1 && prev <= w.lt1 + 0.001 && acc >= w.lt1) keys += `<span class="istrip-lt">LT1</span>`;
        if (w.lt2 && prev <= w.lt2 + 0.001 && acc >= w.lt2) keys += `<span class="istrip-lt">LT2</span>`;
      }
      const p = Math.max(0, Math.min(100, (w.hr_pct || 0) * 100));
      const mk = (v, t) => `<div class="istrip-mk" style="left:${(v * 100).toFixed(1)}%" title="${t}"></div>`;
      const marksHtml = mk(w.lt1, 'LT1 巡航阈（一区上沿）') + mk(w.lt2, 'LT2 乳酸阈（二区上沿）');
      return `<div class="istrip">${segs}${marksHtml}`
        + `<div class="istrip-dot" style="left:calc(${p.toFixed(1)}% - 6px)" title="本次 ${p.toFixed(0)}%"></div></div>`
        + `<div class="istrip-keys">${keys}</div>`;
    },
    zoneDotText(w) {
      const p = w.hr_pct;
      if (p == null) return '';
      const pct = (p * 100).toFixed(0) + '% ' + this.metricName();
      if (p < w.lt1) return `本次平均强度 ${pct}，位于 LT1 以下——纯有氧基础区`;
      if (p < w.lt2) return `本次平均强度 ${pct}，落在 LT1 与 LT2 之间——高强度有氧（节奏建设）区`;
      if (p < 0.96) return `本次平均强度 ${pct}，已在 LT2（乳酸阈）之上——阈值强度，质量课区间`;
      return `本次平均强度 ${pct}，远超 LT2——无氧强度区，注意累积疲劳`;
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
