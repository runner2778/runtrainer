import { tryCall } from '../api.js';

const HTML = `
<div class="flex mb8" style="flex-wrap:wrap;gap:8px;align-items:center">
  <h2 style="margin:0">丹尼尔斯 VDOT 训练表</h2>
  <span class="badge soft" x-show="d" x-text="d ? ('档位：' + d.vdot_label) : ''"></span>
  <div class="spacer"></div>
  <div class="vdt-ctl">
    <label class="muted">VDOT</label>
    <input type="number" min="30" max="85" step="0.5" x-model.number="vdotInput"
           @keydown.enter="load(vdotInput)">
    <button class="btn small" @click="load(vdotInput)" :disabled="loading">查看该档</button>
    <button class="btn small" @click="load()" :disabled="loading">用当前预估</button>
    <button class="btn small" x-show="d && d.plan_vdot" :disabled="loading"
            @click="load(d.plan_vdot)"
            x-text="'用课表（' + (d ? d.plan_vdot : '') + '）'"></button>
  </div>
</div>

<template x-if="d">
<div>
  <div class="card">
    <div class="flex mb8" style="flex-wrap:wrap;gap:16px;align-items:flex-end">
      <div class="hero">
        <span class="big" x-text="fmtVdot(d.vdot)"></span><span class="unit"> VDOT</span>
      </div>
      <div class="flex" style="flex-wrap:wrap;gap:6px">
        <template x-for="(t, k) in d.equivalent" :key="k">
          <span class="badge soft" x-text="k + ' ' + fmtTime(t)"></span>
        </template>
      </div>
      <div class="spacer"></div>
      <div class="muted small" x-show="d.max_hr">
        心率换算：静息 <span x-text="d.rest_hr != null ? d.rest_hr : '—'"></span>
        · 最大 <span x-text="d.max_hr"></span> bpm（Karvonen %HRR）
      </div>
    </div>
    <p class="muted small" style="margin:0">
      配速与课表 / 日历 / 仪表盘同源——同一套 %VDOT 锚点，改一处全站跟着变。
      心率带按「用途」对应（恢复跑配恢复带…），个体差异与疲劳、天气会让心率整档漂移，只作参考。
    </p>
  </div>

  <div class="card">
    <h3>各训练区间（VDOT <span x-text="fmtVdot(d.vdot)"></span>）</h3>
    <div class="vdt-z">
      <div class="vdt-zr">
        <span class="rail" style="background:transparent"></span>
        <span class="muted">区间</span>
        <span class="muted">%VDOT</span>
        <span class="muted">储备心率区间</span>
        <span class="muted">训练配速（/km）</span>
        <span class="muted">用途</span>
        <span class="muted">典型课</span>
      </div>
      <template x-for="z in d.zones" :key="z.key">
        <div class="vdt-zr">
          <span class="rail" :class="'zn-' + z.key"></span>
          <span>
            <span class="zn-mark" :class="z.mark ? 'kind-' + z.mark : ''" x-text="z.mark || ''"></span>
            <b x-text="z.label"></b>
          </span>
          <span class="num muted" x-text="(z.pct_lo*100).toFixed(0) + '–' + (z.pct_hi*100).toFixed(0) + '%'"></span>
          <span class="vdt-hr" x-html="hrHtml(z.hr)"></span>
          <span class="num" x-html="paceHtml(z)"></span>
          <span class="num muted" x-text="z.use"></span>
          <span class="vdt-chips">
            <template x-for="s in z.sessions" :key="s.key">
              <span class="vdt-chip" :title="s.detail" x-text="s.name"></span>
            </template>
            <span class="muted" x-show="!z.sessions.length">—</span>
          </span>
        </div>
      </template>
    </div>
    <p class="muted small mt8" style="margin-bottom:0">
      典型课取自本应用课表模板（鼠标悬停看完整说明）；每区的锚点配速即课表/日历标注区间时用的那套数值。
    </p>
  </div>

  <div class="card">
    <div class="flex mb8" style="flex-wrap:wrap;gap:8px;align-items:baseline">
      <h3 style="margin:0">
        完整 VDOT 表（<span x-text="d.matrix_min"></span>–<span x-text="d.matrix_max"></span>）× 区间配速
      </h3>
      <span class="muted small">点任意行切到该档；当前档（取最接近的整数档）已高亮</span>
    </div>
    <div class="vdt-scroll">
      <table class="vdt-mx">
        <thead><tr>
          <th>VDOT</th>
          <th class="num">E 轻松</th>
          <th class="num">M 有氧</th>
          <th class="num">T1 巡航阈</th>
          <th class="num">T 阈值</th>
          <th class="num">I 间歇</th>
          <th class="num">R 重复</th>
          <!-- 等效成绩列由后端 rows[].races 的键决定（与页首等效成绩、向导同源） -->
          <template x-for="k in raceCols()" :key="k">
            <th class="num" :title="'等效成绩 · ' + k" x-text="k"></th>
          </template>
        </tr></thead>
        <tbody>
          <template x-for="r in d.matrix" :key="r.vdot">
            <tr :class="{cur: r.vdot === curRow()}" @click="load(r.vdot)"
                :title="'切到 VDOT ' + r.vdot">
              <td class="num"><b x-text="r.vdot"></b></td>
              <td class="num" x-text="fmtPace(r.E[0]) + ' – ' + fmtPace(r.E[1])"></td>
              <td class="num" x-text="fmtPace(r.M)"></td>
              <td class="num" x-text="fmtPace(r.T1)"></td>
              <td class="num" x-text="fmtPace(r.T)"></td>
              <td class="num" x-text="fmtPace(r.I)"></td>
              <td class="num" x-text="fmtPace(r.R)"></td>
              <template x-for="k in raceCols()" :key="k">
                <td class="num muted" x-text="fmtTime(r.races[k])"></td>
              </template>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <p class="muted small mt8" style="margin-bottom:0">
      配速为秒/公里的 m:ss（不含单位），等效成绩为常用比赛距离的预估成绩。
      表中档位为整数档；若你的预估是小数（如 54.2），高亮的是最接近的整数档，上方区间明细仍按你选的小数档计算。
    </p>
  </div>
</div>
</template>

<template x-if="!d">
  <div class="card muted" x-text="loading ? '加载中…' : '暂无数据'"></div>
</template>`;

export function initVdot() {
  const sec = document.getElementById('page-vdot');
  sec.innerHTML = HTML;

  window.Alpine.data('vdotPage', () => ({
    d: null,
    loading: false,
    vdotInput: null,
    pinned: false,   // 用户是否显式选了档位（同步刷新时保住这个档，不被拉回预估）

    async init() {
      await this.load();
    },
    async shown() {
      // 切回本页重拉（数据层现算无缓存）；刚从 init/同步 拉过就跳过，避免双载
      if (!this._lastLoad || Date.now() - this._lastLoad > 2000) await this.load();
    },
    async syncRefresh() {
      // 同步完成（新活动/重建课表）→ 重拉；用户显式选过档就保住它，否则跟随最新预估
      await (this.pinned && this.d ? this.load(this.d.vdot) : this.load());
    },

    /** v 省略 = 跟随当前预估（后端解析默认档）；给了 v = 显式指定档位 */
    async load(v) {
      const explicit = v !== undefined && v !== null && v !== '';
      this.loading = true;
      try {
        if (explicit && !(Number(v) > 0)) {
          this.$dispatch('toast', { text: 'VDOT 需为正数' });
          return;
        }
        const r = explicit ? await tryCall('get_vdot_table', Number(v))
                           : await tryCall('get_vdot_table');
        if (!r.ok) { this.$dispatch('toast', { text: '读取 VDOT 表失败: ' + r.error }); return; }
        this.d = r.data;
        this.pinned = explicit;
        this.vdotInput = r.data.vdot;
        this._lastLoad = Date.now();
      } finally {
        this.loading = false;
      }
    },

    /** 矩阵里该高亮哪一行：当前档最接近的整数档 */
    curRow() {
      return this.d ? Math.round(this.d.vdot) : null;
    },
    /** 等效成绩的列名，取自后端（加/减距离只改 domain 那处，前端不写死） */
    raceCols() {
      const rows = this.d && this.d.matrix;
      return rows && rows.length ? Object.keys(rows[0].races) : [];
    },
    fmtVdot(v) {
      if (v == null) return '—';
      return Number.isInteger(v) ? String(v) : v.toFixed(1);
    },
    /** 储备心率带：%HRR 区间（lo=0 时写作 <hi%，符合该带「低于」的语义）+ bpm */
    hrHtml(hr) {
      if (!hr) return '<span class="muted">—</span>';
      const lo = Math.round(hr.lo * 100), hi = Math.round(hr.hi * 100);
      const pct = lo === 0 ? '&lt;' + hi : lo + '–' + hi;
      const bpm = hr.bpm_lo != null ? ' <span class="muted">· ' + hr.bpm_lo + '–' + hr.bpm_hi + ' bpm</span>' : '';
      return '<b>' + pct + '%</b>' + bpm;
    },
    /** 训练配速带 + 落在该区的锚点配速（课表/日历标注用的精确值） */
    paceHtml(z) {
      let s = '<span class="pace">' + this.fmtPace(z.pace_slow_s_km) + ' – '
        + this.fmtPace(z.pace_fast_s_km) + '</span>';
      if (z.anchors && z.anchors.length) {
        s += '<br><span class="vdt-anchor">' + z.anchors.map((a) =>
          a.mark + ' ' + this.fmtPace(a.pace_s_km)).join(' · ') + '</span>';
      }
      return s;
    },
    /** 秒 → m:ss（配速用，紧凑不加单位） */
    fmtPace(s) {
      if (s == null) return '—';
      const t = Math.round(s), m = Math.floor(t / 60), sec = t % 60;
      return m + ':' + String(sec).padStart(2, '0');
    },
    /** 秒 → m:ss / h:mm:ss（成绩用） */
    fmtTime(s) {
      if (s == null) return '—';
      const t = Math.round(s);
      const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
      return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0')
               : m + ':' + String(sec).padStart(2, '0');
    },
  }));
}
