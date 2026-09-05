import { tryCall } from '../api.js';

const KIND_LABELS = { E: '轻松跑', M: '马拉松配速', T: '阈值跑', I: '间歇跑', R: '重复跑', LR: '长距离', RECOVERY: '恢复', TUNEUP: '测试赛', RACE: '比赛', STRENGTH: '力量训练' };
const PHASE_LABELS = { base: '基础期', early: '早期强度', transition: '过渡期', final: '最终强度', taper: '减量期' };
const ZONE_LABELS = { E: '轻松配速', M: '马拉松配速', T: '阈值配速', I: '间歇配速', R: '重复配速', race: '比赛配速', strength: '力量训练' };
const DOW = ['一', '二', '三', '四', '五', '六', '日'];

function fmtPace(s) {
  if (!s) return '--';
  const t = Math.round(s), m = Math.floor(t / 60), sec = t % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}
function fmtDur(min) { return min ? `${Math.round(min)} 分钟` : ''; }
function pad2(n) { return String(n).padStart(2, '0'); }

const HTML = `
<div class="card" x-show="!plan && !loading">
  <h2>日历</h2>
  <p class="muted">还没有训练计划。<a href="#/goal">去创建训练目标 →</a></p>
</div>
<template x-if="plan">
  <div>
    <div class="card">
      <div class="flex between">
        <h2>日历 · <span x-text="planTitle"></span></h2>
        <div class="flex">
          <button class="btn" @click="moveMonth(-1)">‹</button>
          <b class="mv-center" x-text="monthLabel"></b>
          <button class="btn" @click="moveMonth(1)">›</button>
          <button class="btn" @click="goToday()">今天</button>
        </div>
      </div>
      <div class="flex mt8 legend">
        <template x-for="(label, k) in KIND_LABELS" :key="k">
          <span class="legend-item"><span class="badge" :class="'kind-' + k" x-text="k"></span>
            <span class="muted" x-text="label"></span></span>
        </template>
      </div>
      <p class="muted">阶段：<template x-for="(wks, p) in plan.phase_weeks" :key="p">
        <span class="mr8"><span x-text="PHASE_LABELS[p]"></span> <span x-text="wks"></span> 周</span>
      </template></p>
    </div>

    <div class="card" x-show="progress">
      <h3>计划进度 · 时期</h3>
      <div class="phase-line mt8">
        <template x-for="ph in progress.phases" :key="ph.phase">
          <div class="phase-seg" :class="'ph-' + ph.phase + (ph.current ? ' cur' : '')"
               :style="'flex:' + ph.weeks" :title="PHASE_LABELS[ph.phase] + ' ' + ph.weeks + ' 周'"
               x-text="(ph.current ? '📍 ' : '') + PHASE_LABELS[ph.phase] + ' ' + ph.weeks + 'w'"></div>
        </template>
      </div>
      <div class="grid cols-4 mt8">
        <div class="stat"><b x-text="'第 ' + (Math.floor(progress.weeks_elapsed) + 1) + ' / ' + progress.total_weeks + ' 周'"></b>
          <span>距比赛 <b x-text="raceDaysLeft"></b> 天（<span x-text="progress.race_date"></span>）</span></div>
        <div class="stat"><b x-text="progress.workouts_done + ' / ' + progress.workouts_past"></b>
          <span>已过日期的课完成</span></div>
        <div class="stat"><b><span x-text="progress.compliance_4w === null ? '--' : Math.round(progress.compliance_4w * 100) + '%'"></span></b>
          <span>计划窗口内执行率（实际 <span x-text="progress.done_km_4w"></span> / 计划 <span x-text="progress.planned_km_4w"></span> km）</span></div>
        <div class="stat"><b x-text="currentPhase ? PHASE_LABELS[currentPhase] : '—'"></b>
          <span>当前时期（至 <span x-text="currentPhaseEnd"></span>）</span></div>
      </div>
    </div>

    <div class="card">
      <div class="calendar">
        <template x-for="d in DOW" :key="d"><div class="dow" x-text="d"></div></template>
        <template x-for="d in days" :key="d.date">
          <div class="day" :class="(d.inMonth ? '' : 'other') + (d.isToday ? ' today' : '') + (d.isRace ? ' race' : '')">
            <div class="dnum" x-text="Number(d.date.slice(8))"></div>
            <template x-for="w in (byDate[d.date] || [])" :key="w.id">
              <button class="wk" :class="'kind-' + w.kind + (w.status === 'completed' ? ' done' : '') + (w.source === 'ai' || w.adjustment_id ? ' ai' : '')"
                      :data-wid="w.id">
                <span x-text="(w.status === 'completed' ? '✓ ' : '') + (w.slot === 2 ? '② ' : '') + shortTitle(w)"></span>
              </button>
            </template>
            <div class="wk rest" x-show="d.isRest && !(byDate[d.date] || []).length">休息</div>
            <!-- 当日实际跑步标注：点击查看活动详情（原生委托分发 data-aid） -->
            <template x-for="a in (actsByDate[d.date] || [])" :key="'a' + a.id">
              <div class="act-tag" :data-aid="a.id" :title="a.name">
                🏃 <span x-text="(a.distance_m / 1000).toFixed(1) + 'km'"></span>
              </div>
            </template>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<!-- 弹窗遮罩必须用 x-if：本环境 WebView2 的 x-show 隐藏机制有缺陷，
     关闭后遮罩可能残留在 DOM 上导致暗屏卡死 -->
<template x-if="modal">
<div class="modal-mask" @click.self="modal = null">
  <div class="modal">
      <div>
        <h3><span class="badge" :class="'kind-' + modal.kind" x-text="KIND_LABELS[modal.kind] || modal.kind"></span>
          <span x-text="modal.title"></span></h3>
        <p class="muted"><span x-text="modal.date"></span> · 第 <span x-text="modal.week_index + 1"></span> 周 ·
          <span x-text="PHASE_LABELS[modal.phase] || modal.phase"></span>
          <span x-show="modal.slot === 2"> · <b>当天第 2 练</b></span>
          <span x-show="modal.source === 'ai' || modal.adjustment_id"> · <b>AI 已调整</b></span></p>
        <p x-text="modal.description"></p>
        <div class="mt8">
          <template x-for="s in modal.segments || []" :key="s.type + JSON.stringify(s)">
            <div class="seg-row" x-html="segText(s)"></div>
          </template>
        </div>
        <p class="mt8" x-show="modal.pace_slow_s_km">
          目标配速：<b x-text="fmtPace(modal.pace_slow_s_km) + (modal.pace_fast_s_km && modal.pace_fast_s_km !== modal.pace_slow_s_km ? ' – ' + fmtPace(modal.pace_fast_s_km) : '')"></b>/km
        </p>
        <p class="mt8" x-show="modal.distance_km || modal.duration_min">
          <span x-show="modal.distance_km">约 <b x-text="modal.distance_km"></b> km</span>
          <span x-show="modal.distance_km && modal.duration_min"> · </span>
          <span x-show="modal.duration_min"><b x-text="fmtDur(modal.duration_min)"></b></span>
        </p>
        <div class="flex mt16">
          <button class="btn primary" x-show="modal.status !== 'completed'"
                  @click="markDone(modal, null)">✓ 标记完成</button>
          <button class="btn" x-show="modal.status !== 'completed' && sameDayActs.length"
                  @click="showLink = !showLink">关联活动完成</button>
          <button class="btn" x-show="modal.status === 'completed'"
                  @click="markDone(modal, null, 'planned')">↩ 恢复为计划</button>
          <button class="btn ghost" @click="modal = null; showLink = false">关闭</button>
        </div>
        <div class="mt8" x-show="showLink && sameDayActs.length">
          <p class="muted">选择当日活动：</p>
          <template x-for="a in sameDayActs" :key="a.id">
            <div class="diff-row">
              <span x-text="a.name || a.source"></span>
              <span class="muted" x-text="(a.distance_m / 1000).toFixed(2) + ' km'"></span>
              <button class="btn small" @click="markDone(modal, a.id)">关联</button>
            </div>
          </template>
        </div>
      </div>
  </div>
</div>
</template>

<!-- 实际跑步活动详情弹窗（日历标注点击打开） -->
<template x-if="actModal">
<div class="modal-mask" @click.self="actModal = null">
  <div class="modal">
    <div class="flex">
      <h3 x-text="actModal.name"></h3>
      <div class="spacer"></div>
      <button class="btn small" @click="actModal = null">✕</button>
    </div>
    <div class="grid cols-4 mb8">
      <div><div class="muted">距离</div><b x-text="actModal.distance_m ? (actModal.distance_m / 1000).toFixed(2) + ' km' : '—'"></b></div>
      <div><div class="muted">时长</div><b x-text="fmtTime(actModal.duration_s)"></b></div>
      <div><div class="muted">平均配速</div><b x-text="actModal.avg_pace_s_km ? fmtPace(actModal.avg_pace_s_km) + '/km' : '—'"></b></div>
      <div><div class="muted">平均心率</div><b x-text="actModal.avg_hr ? actModal.avg_hr.toFixed(0) : '—'"></b></div>
    </div>
    <div class="grid cols-4 mb8">
      <div><div class="muted">步频</div><b x-text="actModal.avg_cadence ? actModal.avg_cadence.toFixed(0) + ' spm' : '—'"></b></div>
      <div><div class="muted">步幅</div><b x-text="actModal.stride_length_m ? actModal.stride_length_m.toFixed(2) + ' m' : '—'"></b></div>
      <div><div class="muted">有氧训练效果</div><b x-text="actModal.aerobic_te != null ? actModal.aerobic_te.toFixed(1) : '—'"></b></div>
      <div><div class="muted">训练负荷</div><b x-text="actModal.exercise_load != null ? actModal.exercise_load.toFixed(0) : '—'"></b></div>
    </div>
    <p class="mt8" x-show="actStructureLabel"><span class="badge soft" x-text="actStructureLabel"></span></p>
    <template x-if="actSegments.length > 1">
      <table class="mt8">
        <thead><tr><th>分段</th><th class="num">距离(m)</th><th class="num">用时</th><th class="num">配速</th><th class="num">平均心率</th></tr></thead>
        <tbody>
          <template x-for="(s, i) in actSegments" :key="i">
            <tr>
              <td x-text="segLabel(s.type)"></td>
              <td class="num" x-text="s.distance_m || '—'"></td>
              <td class="num" x-text="s.duration_s ? fmtTime(s.duration_s) : '—'"></td>
              <td class="num" x-text="s.pace_s_km ? fmtPace(s.pace_s_km) + '/km' : '—'"></td>
              <td class="num" x-text="s.avg_hr ? s.avg_hr.toFixed(0) : '—'"></td>
            </tr>
          </template>
        </tbody>
      </table>
    </template>
    <div class="flex mt16">
      <button class="btn ghost" @click="actModal = null">关闭</button>
    </div>
  </div>
</div>
</template>`;

export function initCalendar() {
  const sec = document.getElementById('page-calendar');
  sec.innerHTML = HTML;

  // 原生事件委托：本环境 WebView2 中 x-for 重复求值产生的克隆按钮会丢失
  // Alpine 绑定（@click 不触发），改用静态 section 上的原生监听按 data-wid
  // / data-aid 分发（课表按钮 → openWorkout；跑步标注 → openActivity）。
  sec.addEventListener('click', (e) => {
    const comp = window.Alpine && Alpine.$data(sec);
    if (!comp) return;
    const btn = e.target.closest('button.wk');
    if (btn && btn.dataset.wid && comp.byDate) {
      const id = parseInt(btn.dataset.wid, 10);
      for (const arr of Object.values(comp.byDate)) {
        const w = arr.find((x) => x.id === id);
        if (w) { comp.openWorkout(w); return; }
      }
    }
    const actEl = e.target.closest('[data-aid]');
    if (actEl && actEl.dataset.aid) {
      comp.openActivity(parseInt(actEl.dataset.aid, 10));
    }
  });

  window.Alpine.data('calendarPage', () => ({
    KIND_LABELS,
    PHASE_LABELS,
    DOW,
    plan: null,
    planTitle: '',
    paces: {},
    progress: null,
    loading: false,
    ym: { y: new Date().getFullYear(), m: new Date().getMonth() + 1 },
    byDate: {},
    actsByDate: {},
    modal: null,
    actModal: null,
    showLink: false,
    sameDayActs: [],

    get monthLabel() { return `${this.ym.y} 年 ${this.ym.m} 月`; },
    get currentPhase() {
      const ph = (this.progress && this.progress.phases || []).find(p => p.current);
      return ph ? ph.phase : null;
    },
    get currentPhaseEnd() {
      const ph = (this.progress && this.progress.phases || []).find(p => p.current);
      return ph ? ph.end_date : '--';
    },
    get raceDaysLeft() {
      if (!this.progress) return '--';
      const d = Math.ceil((new Date(this.progress.race_date + 'T00:00:00')
        - new Date(this.progress.today + 'T00:00:00')) / 86400000);
      return Math.max(0, d);
    },
    get days() {
      const { y, m } = this.ym;
      const first = new Date(y, m - 1, 1);
      const start = new Date(first);
      start.setDate(first.getDate() - (first.getDay() + 6) % 7);
      const out = [];
      const t = new Date();
      const todayIso = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
      for (let i = 0; i < 42; i++) {
        const d = new Date(start);
        d.setDate(start.getDate() + i);
        const iso = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
        out.push({
          date: iso, inMonth: d.getMonth() === m - 1, isToday: iso === todayIso,
          isRace: this.plan && iso === this.plan.race_date,
          isRest: this.plan && iso >= this.plan.start_date && iso <= this.plan.race_date && !this.byDate[iso],
        });
      }
      return out;
    },

    async shown() { await this.load(); },
    async init() { await this.load(); },
    async load() {
      this.loading = true;
      const r = await tryCall('get_active_plan');
      this.loading = false;
      if (!r.ok || !r.data) { this.plan = null; this.byDate = {}; return; }
      this.plan = r.data;
      this.paces = r.data.paces || {};
      const goal = await tryCall('get_active_goal');
      const dist = { 5000: '5K', 10000: '10K', 21097: '半马', 42195: '全马' }
        [r.data.goal_distance_m || (goal.ok && goal.data && goal.data.distance_m)] || '';
      const target = goal.ok && goal.data && goal.data.target_seconds
        ? `（目标 ${this.fmtTime(goal.data.target_seconds)}）` : '（完赛）';
      this.planTitle = `${dist} ${r.data.race_date}${target}`;
      // 拉取可见月份 ± 一周的课表
      const { y, m } = this.ym;
      const gridStart = new Date(y, m - 1, 1);
      gridStart.setDate(gridStart.getDate() - (gridStart.getDay() + 6) % 7);
      const gridEnd = new Date(gridStart);
      gridEnd.setDate(gridStart.getDate() + 41);
      const gridStartIso = this.fmtIsoDate(gridStart);
      const gridEndIso = this.fmtIsoDate(gridEnd);
      const pr = await tryCall('get_plan_progress');
      this.progress = pr.ok ? pr.data : null;
      const ws = await tryCall('get_plan_workouts', r.data.id, gridStartIso, gridEndIso);
      this.byDate = {};
      if (ws.ok) for (const w of ws.data) {
        (this.byDate[w.date] = this.byDate[w.date] || []).push(w);
      }
      // 当日实际跑步标注（已完成活动按日期标注在日历上）
      const acts = await tryCall('list_activities', gridStartIso, gridEndIso, null, 500);
      this.actsByDate = {};
      if (acts.ok) for (const a of acts.data) {
        const d = this.fmtIsoDate(new Date(a.start_ts * 1000));
        (this.actsByDate[d] = this.actsByDate[d] || []).push(a);
      }
    },

    fmtIsoDate(d) {
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },

    async openActivity(id) {
      const { ok, data } = await tryCall('get_activity', id);
      if (!ok || !data) { this.$dispatch('toast', { text: '读取活动失败' }); return; }
      this.actModal = data;
    },
    get actStructureLabel() {
      const st = this.actModal && this.actModal.structure;
      if (!st || !st.length) return '';
      if (st.some((s) => s.type === 'rest' || s.type === 'recovery')) {
        const works = st.filter((s) => s.type === 'work');
        const rests = st.filter((s) => s.type === 'rest' || s.type === 'recovery');
        const km = works.reduce((t, s) => t + (s.distance_m || 0), 0) / 1000;
        return `间歇训练：${works.length} 组跑段共 ${km.toFixed(1)} km，${rests.length} 段休息`;
      }
      return '匀速跑';
    },
    get actSegments() {
      const st = this.actModal && this.actModal.structure;
      return st && st.length > 1 ? st : [];
    },
    segLabel(t) {
      return { work: '跑段', rest: '休息', recovery: '热身/冷身' }[t] || t;
    },

    moveMonth(n) {
      let { y, m } = this.ym;
      m += n;
      if (m < 1) { m = 12; y--; }
      if (m > 12) { m = 1; y++; }
      this.ym = { y, m };
      this.load();
    },
    goToday() {
      const now = new Date();
      this.ym = { y: now.getFullYear(), m: now.getMonth() + 1 };
      this.load();
    },
    shortTitle(w) {
      if (w.kind === 'RACE') return '比赛';
      if (w.kind === 'E' || w.kind === 'RECOVERY') {
        const m = w.duration_min ? Math.round(w.duration_min) : (w.distance_km * 6);
        return `${KIND_LABELS[w.kind]} ${m}′`;
      }
      if (w.kind === 'STRENGTH') return `力量 ${Math.round(w.duration_min || 40)}′`;
      return w.title;
    },

    async openWorkout(w) {
      this.modal = w;
      this.showLink = false;
      const acts = await tryCall('list_activities', w.date, w.date, null, 20);
      this.sameDayActs = acts.ok ? acts.data : [];
    },
    async markDone(w, activityId, status = 'completed') {
      const { ok, error } = await tryCall('set_workout_status', w.id, status, activityId);
      if (!ok) { this.$dispatch('toast', { text: '操作失败: ' + error }); return; }
      this.$dispatch('toast', { text: status === 'completed' ? '已标记完成' : '已恢复为计划' });
      this.modal = null;
      this.showLink = false;
      await this.load();
    },
    // 段落目标配速：E 区间显示范围，其余区间显示单值
    paceLabel(zone) {
      const p = this.paces && this.paces[zone];
      if (!p) return '';
      if (typeof p === 'object') return ` ${this.fmtPace(p.slow_s_km)}–${this.fmtPace(p.fast_s_km)}/km`;
      return ` ${this.fmtPace(p)}/km`;
    },
    segText(s) {
      const zone = ZONE_LABELS[s.zone] || s.zone || '';
      const pz = this.paceLabel(s.zone);
      const rm = { jog: '慢跑', walk: '走路', any: '走路/慢跑/静止均可' }[s.rest_mode];
      switch (s.type) {
        case 'warmup': return `🏃 热身 · ${zone}${pz} · ${s.duration_min} 分钟`;
        case 'cooldown': return `🧊 冷身 · ${zone}${pz} · ${s.duration_min} 分钟`;
        case 'continuous': return `🏃 ${zone}${pz}` + (s.distance_km ? ` · ${s.distance_km} km` : ` · ${s.duration_min} 分钟`);
        case 'tempo': return `⚡ 阈值跑 ${s.reps}×${s.duration_min} 分钟${pz}` + (s.rest_min ? `（组间 ${s.rest_min} 分钟${rm || '慢跑'}）` : '');
        case 'reps': return `⚡ ${s.zone === 'I' ? '间歇' : '重复跑'} ${s.reps}×${s.rep_m}m${pz}（组间 ${s.rest_m}m ${rm || '慢跑恢复'}）`;
        case 'strides': return `⚡ 跨步跑 ${s.reps}×${s.rep_m}m${pz}（约 85% 最快速度）`;
        default: return '';
      }
    },
    fmtPace,
    fmtTime(s) {
      if (!s) return '--';
      // 先四舍五入再拆分，避免 59.6s 进位出「4:60」
      s = Math.round(s);
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      return h ? `${h}:${pad2(m)}:${pad2(sec)}` : `${m}:${pad2(sec)}`;
    },
  }));
}
