import { call, tryCall } from '../api.js';

const DISTANCES = [
  { value: 5000, label: '5K', full: '5 公里' },
  { value: 10000, label: '10K', full: '10 公里' },
  { value: 21097, label: '半马', full: '半程马拉松 21.0975km' },
  { value: 42195, label: '全马', full: '全程马拉松 42.195km' },
];
const PHASE_LABELS = { base: '基础期', early: '早期强度', transition: '过渡期', final: '最终强度', taper: '减量期' };
const KIND_LABELS = { E: '轻松跑', M: '马拉松配速', T: '阈值跑', I: '间歇跑', R: '重复跑', LR: '长距离', RECOVERY: '恢复', TUNEUP: '测试赛', RACE: '比赛', STRENGTH: '力量训练' };
const ZONE_LABELS = { E: '轻松配速', M: '马拉松配速', T: '阈值配速', I: '间歇配速', R: '重复配速', race: '比赛配速' };
// 课型 → 强度区间（与仪表盘/日历一致：vdot.PACE_ZONES 的区间与 % 带）——
// 制定出的课表在日历上按此标注「该课属于哪一档强度区间」
const KIND_ZONES = {
  E: { label: '轻松区', band: '59–74%' },
  M: { label: '有氧/马配区', band: '74–82%' },
  T: { label: '乳酸阈值区', band: '82–92%' },
  I: { label: '最大摄氧量区', band: '92–100%' },
  R: { label: '无氧冲刺区', band: '100–105%' },
  LR: { label: '有氧区（长距离）', band: '59–82%' },
  RECOVERY: { label: '恢复区', band: '<59%' },
  TUNEUP: { label: '测试赛强度', band: '' },
  RACE: { label: '比赛强度', band: '' },
};

function fmtPace(s) {
  if (!s) return '--';
  const t = Math.round(s), m = Math.floor(t / 60), sec = t % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}
function fmtTime(s) {
  if (!s) return '--';
  // 先四舍五入再拆分，避免 59.6s 进位出「4:60」
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}
function parseTarget(str) {
  const s = (str || '').trim();
  if (!s) return null;
  const parts = s.split(':').map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return null;
}
function addDays(iso, n) {
  const d = new Date(iso + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
function localToday() {
  // toISOString 是 UTC 日期，北京时间凌晨会差一天，本地日期必须手工拼
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function fmtDate(iso) {
  if (!iso) return '--';
  const [y, m, d] = iso.split('-');
  return `${y}年${Number(m)}月${Number(d)}日`;
}

const HTML = `
<div x-show="step === 1">
  <div class="card">
    <h2>第 1 步 · 选择比赛距离</h2>
    <!-- 距离列表写死为静态 HTML：本环境 WebView2 中，初始挂载时对非空数组的
         template x-for 会被多次求值且每次追加一份克隆（4 张卡片变 12 张）。
         动态列表请以空数组起步再异步填充，或置于 x-if 惰性子树内。 -->
    <div class="grid cols-2">
      <button class="btn big dist-card" :class="form.distance_m === 5000 ? 'primary' : ''" @click="form.distance_m = 5000">
        <b>5K</b><span class="muted">5 公里</span>
      </button>
      <button class="btn big dist-card" :class="form.distance_m === 10000 ? 'primary' : ''" @click="form.distance_m = 10000">
        <b>10K</b><span class="muted">10 公里</span>
      </button>
      <button class="btn big dist-card" :class="form.distance_m === 21097 ? 'primary' : ''" @click="form.distance_m = 21097">
        <b>半马</b><span class="muted">半程马拉松 21.0975km</span>
      </button>
      <button class="btn big dist-card" :class="form.distance_m === 42195 ? 'primary' : ''" @click="form.distance_m = 42195">
        <b>全马</b><span class="muted">全程马拉松 42.195km</span>
      </button>
    </div>
    <div class="flex mt16">
      <button class="btn primary" @click="next1()">下一步 →</button>
    </div>
  </div>
</div>

<div x-show="step === 2">
  <div class="card">
    <h2>第 2 步 · 比赛与训练参数</h2>
    <template x-if="hasPlan">
      <p class="banner warn">已存在训练计划，生成新计划会将旧计划归档。</p>
    </template>
    <div class="grid cols-2">
      <div>
        <div class="form-row"><label>比赛日期（该距离建议至少 <b x-text="minWeeks"></b> 周备赛）</label>
          <input type="date" x-model="form.race_date" :min="today"></div>
        <div class="form-row"><label>目标成绩（如 1:45:00；留空 = 完赛）</label>
          <input x-model="form.targetStr" placeholder="1:45:00"></div>
        <div class="form-row"><label>基础周跑量 (km)</label>
          <input type="number" x-model="form.base_weekly_km" min="10" max="150" step="0.5">
          <p class="wizard-hint" x-show="ctx.avg_weekly_km_4w">近 4 周平均 <b x-text="ctx.avg_weekly_km_4w"></b> km
            <a href="#" @click.prevent="form.base_weekly_km = ctx.avg_weekly_km_4w">使用</a></p></div>
        <div class="form-row"><label>每周训练天数</label>
          <select x-model="form.run_days">
            <option :value="4">4 天</option><option :value="5">5 天</option><option :value="6">6 天</option>
          </select></div>
        <div class="form-row"><label>长距离日</label>
          <select x-model="form.long_run_weekday">
            <option :value="6">周日</option><option :value="5">周六</option><option :value="0">周一</option>
          </select></div>
        <div class="form-row" x-show="!form.pro_mode"><label>一天两练（每周几天）</label>
          <select x-model="form.double_days">
            <option :value="0">不安排</option><option :value="1">1 天</option><option :value="2">2 天</option>
          </select>
          <p class="wizard-hint" x-show="form.double_days > 0">同一训练日拆成两练，日历上以 ② 标注当天第 2 练。</p></div>
        <div class="form-row" x-show="form.double_days > 0 && !form.pro_mode"><label>二练形式</label>
          <select x-model="form.double_mode">
            <option value="auto">按阶段自动</option>
            <option value="threshold">双乳酸阈值（挪威模式：上午 3×8′ 亚阈 + 下午 5×5′ 亚阈）</option>
            <option value="easy">强度课 + 傍晚放松跑</option>
          </select></div>
        <div class="form-row"><label>职业双练模式</label>
          <label class="flex" style="gap:8px;align-items:center">
            <input type="checkbox" style="width:auto" x-model="form.pro_mode">
            <span>效仿职业运动员：休息日轻松跑单练，其余每天两练</span>
          </label>
          <p class="wizard-hint" x-show="form.pro_mode">休息日改为 30 分钟轻松跑单练；其余训练日主课 + 傍晚放松跑（T 日按挪威模式上下午双阈值）；周跑量展示约上浮 7×30 分钟；减量周与比赛周自动恢复常规（含休息日）。</p></div>
        <div class="form-row"><label>每周力量训练次数（穿插在轻松日）</label>
          <select x-model="form.strength_days">
            <option :value="0">不安排</option><option :value="1">1 次</option><option :value="2">2 次</option>
          </select>
          <p class="wizard-hint" x-show="form.strength_days > 0">力量课不占跑量，在轻松填充日穿插；减量周与比赛周自动取消。</p></div>
      </div>
      <div>
        <div class="form-row"><label>VDOT 来源</label>
          <select x-model="form.vdotMode">
            <option value="auto">自动（近期成绩 / 目标反推）</option>
            <option value="manual">手动指定</option>
          </select></div>
        <div class="form-row" x-show="form.vdotMode === 'manual'"><label>VDOT 值（30–85）</label>
          <input type="number" x-model="form.manualVdot" min="30" max="85" step="0.1"></div>
        <p class="muted" x-show="ctx.recent_vdot">近期比赛水平：VDOT <b x-text="ctx.recent_vdot"></b>
          <template x-if="ctx.recent_race">
            <span>（<span x-text="ctx.recent_race.name"></span>
            <span x-text="fmtDate(ctx.recent_race.date)"></span>）</span>
          </template></p>
        <p class="muted mt8">基础课表由丹尼尔斯训练法（VDOT + E/M/T/I/R 配速 + 周期化）确定性生成，DeepSeek 之后按每日数据灵活调整。</p>
        <!-- 当前水平预估：综合手表 VO2max / 配速-心率趋势 / 间歇能力 / 近期比赛 -->
        <template x-if="ctx.ability && ctx.ability.vdot">
          <div class="card mt8">
            <h3>当前水平预估（综合 VDOT <span x-text="ctx.ability.vdot"></span>）</h3>
            <div class="grid cols-4">
              <template x-for="(sec, label) in ctx.ability.predictions" :key="label">
                <div class="stat"><b x-text="label"></b><span x-text="fmtTime(sec)"></span></div>
              </template>
            </div>
            <p class="muted mt8">综合依据：</p>
            <ul class="muted">
              <template x-for="ev in ctx.ability.evidence" :key="ev.source">
                <li><span x-text="ev.detail"></span>（VDOT <span x-text="ev.vdot"></span>）</li>
              </template>
            </ul>
            <!-- 当前水平各区间配速（恢复→无氧冲刺）：生成的课表/日历中的区间
                 安排即按此表标注（每种课的 %VDOT 档位与配速带） -->
            <details class="mt8" x-show="(ctx.ability.zones || []).length" :open="true">
              <summary class="muted small">当前水平各区间配速（恢复 → 无氧冲刺，课表/日历按此标注）</summary>
              <div class="zones-table mt8">
                <template x-for="z in ctx.ability.zones" :key="z.key">
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
            <p class="muted mt8" x-show="ctx.ability.max_hr">HRmax 估算：<span x-text="ctx.ability.max_hr"></span> bpm</p>
          </div>
        </template>
      </div>
    </div>
    <div class="card mt8">
      <h3>训练时期 · 从哪里开始？</h3>
      <div class="grid cols-2">
        <button class="btn big" :class="form.phaseMode === 'auto' ? 'primary' : ''" @click="form.phaseMode = 'auto'">
          <b>让软件智能判断</b><span class="muted">分析近期强度与跑量分布，从对应时期开始</span>
        </button>
        <button class="btn big" :class="form.phaseMode === 'manual' ? 'primary' : ''" @click="form.phaseMode = 'manual'">
          <b>我在跟自己的计划</b><span class="muted">手动选择当前所处时期，向后接续制定</span>
        </button>
      </div>
      <template x-if="ctx.phase_suggestion">
        <div class="mt8">
          <p class="muted">智能判断：你可能处于 <b x-text="PHASE_LABELS[ctx.phase_suggestion.phase]"></b>
            （置信度 <span x-text="ctx.phase_suggestion.confidence"></span>）</p>
          <ul class="muted">
            <template x-for="r in ctx.phase_suggestion.reasons" :key="r"><li x-text="r"></li></template>
          </ul>
        </div>
      </template>
      <div class="mt8" x-show="form.phaseMode === 'manual'">
        <p class="muted">选择你目前所处的时期，课表将从此时期向后制定（前面的冗长课表不再重复）：</p>
        <div class="grid cols-3">
          <button class="btn big" :class="!form.startPhase ? 'primary' : ''" @click="form.startPhase = null">
            <b>从头开始</b><span class="muted">完整周期</span></button>
          <button class="btn big" :class="form.startPhase === 'base' ? 'primary' : ''" @click="form.startPhase = 'base'">
            <b>基础期</b><span class="muted">有氧底子</span></button>
          <button class="btn big" :class="form.startPhase === 'early' ? 'primary' : ''" @click="form.startPhase = 'early'">
            <b>早期强度</b><span class="muted">开始加短间歇</span></button>
          <button class="btn big" :class="form.startPhase === 'transition' ? 'primary' : ''" @click="form.startPhase = 'transition'">
            <b>过渡期</b><span class="muted">间歇 + 节奏</span></button>
          <button class="btn big" :class="form.startPhase === 'final' ? 'primary' : ''" @click="form.startPhase = 'final'">
            <b>最终强度</b><span class="muted">专项峰值</span></button>
          <button class="btn big" :class="form.startPhase === 'taper' ? 'primary' : ''" @click="form.startPhase = 'taper'">
            <b>减量期</b><span class="muted">赛前调整</span></button>
        </div>
      </div>
    </div>
    <div class="flex mt16">
      <button class="btn" @click="step = 1">← 上一步</button>
      <button class="btn primary" @click="next2()">预览课表 →</button>
    </div>
  </div>
</div>

<div x-show="step === 3">
  <div class="card" x-show="!preview">
    <h2>第 3 步 · 生成预览</h2>
    <p class="muted" x-text="loading ? '正在生成课表…' : '预览加载失败'"></p>
  </div>
  <template x-if="preview">
    <div class="grid cols-2">
      <div class="card">
        <h2>配速表（VDOT <span x-text="preview.vdot"></span>）</h2>
        <table>
          <thead><tr><th>强度</th><th>配速 (min/km)</th><th>说明</th></tr></thead>
          <tbody>
            <tr><td><span class="badge kind-E">E</span> 轻松跑</td>
              <td class="num" x-text="fmtPace(preview.pace_table.E.slow_s_km) + ' – ' + fmtPace(preview.pace_table.E.fast_s_km)"></td>
              <td class="muted">有氧基础 / 恢复</td></tr>
            <tr><td><span class="badge kind-M">M</span> 马拉松配速</td>
              <td class="num" x-text="fmtPace(preview.pace_table.M)"></td>
              <td class="muted">专项耐力</td></tr>
            <tr><td><span class="badge kind-T">T</span> 阈值跑</td>
              <td class="num" x-text="fmtPace(preview.pace_table.T)"></td>
              <td class="muted">乳酸阈值</td></tr>
            <tr><td><span class="badge kind-I">I</span> 间歇跑</td>
              <td class="num" x-text="fmtPace(preview.pace_table.I)"></td>
              <td class="muted">VO2max 提升</td></tr>
            <tr><td><span class="badge kind-R">R</span> 重复跑</td>
              <td class="num" x-text="fmtPace(preview.pace_table.R)"></td>
              <td class="muted">速度与经济性</td></tr>
          </tbody>
        </table>
        <h3 class="mt16">等效成绩</h3>
        <div class="grid cols-4">
          <template x-for="(sec, label) in preview.equivalent_times" :key="label">
            <div class="stat"><b x-text="label"></b><span x-text="fmtTime(sec)"></span></div>
          </template>
        </div>
      </div>
      <div class="card">
        <h2>阶段与周跑量（<span x-text="preview.total_weeks"></span> 周）</h2>
        <div class="phase-line">
          <template x-for="(wks, p) in preview.phase_weeks" :key="p">
            <div class="phase-seg" x-show="wks > 0" :style="'flex:' + wks" :class="'ph-' + p"
                 :title="PHASE_LABELS[p] + ' ' + wks + ' 周'"
                 x-text="PHASE_LABELS[p] + ' ' + wks + 'w'"></div>
          </template>
        </div>
        <p class="muted" x-show="preview.start_phase">起点时期：<b x-text="PHASE_LABELS[preview.start_phase]"></b>（此前时期已截断，不再重复铺课）</p>
        <p class="muted">基础 <span x-text="preview.base_weekly_km"></span> km → 峰值
          <span x-text="preview.peak_weekly_km"></span> km；每 3 周 +10%，第 4 周减量 20%。</p>
        <h3 class="mt16">课表预览（前 2 周）</h3>
        <template x-for="wk in [0, 1]" :key="wk">
          <div class="mt8">
            <p class="muted">第 <span x-text="wk + 1"></span> 周 · <span x-text="preview.weekly_km[wk]"></span> km</p>
            <template x-for="w in preview.workouts.filter(x => x.week_index === wk)" :key="w.date + '-' + (w.slot || 1)">
              <div class="diff-row"><span x-text="w.date.slice(5) + (w.slot === 2 ? ' ②' : '')"></span>
                <span class="badge" :class="'kind-' + w.kind" x-text="KIND_LABELS[w.kind] || w.kind"></span>
                <span class="badge zn-chip" :class="'znk-' + w.kind" x-show="zoneOf(w.kind)"
                      :title="'对应强度区间（配速见左侧各区配速表）'" x-text="zoneOf(w.kind)"></span>
                <span x-text="w.title"></span></div>
            </template>
          </div>
        </template>
      </div>
    </div>
  </template>
  <template x-if="preview && preview.warnings.length">
    <div class="card warn-card">
      <h2>⚠ 注意</h2>
      <ul><template x-for="w in preview.warnings" :key="w"><li x-text="w"></li></template></ul>
    </div>
  </template>
  <div class="flex mt16" x-show="preview">
    <button class="btn" @click="step = 2">← 修改参数</button>
    <button class="btn primary" @click="createPlan()" :disabled="creating">
      <span x-text="creating ? '生成中…' : '✅ 确认生成课表'"></span>
    </button>
  </div>
</div>`;

export function initGoal() {
  const sec = document.getElementById('page-goal');
  sec.innerHTML = HTML;

  window.Alpine.data('goalPage', () => ({
    step: 1,
    distances: DISTANCES,
    PHASE_LABELS,
    KIND_LABELS,
    KIND_ZONES,
    today: localToday(),
    ctx: { recent_vdot: null, recent_race: null, avg_weekly_km_4w: null, min_weeks: {} },
    hasPlan: false,
    form: {
      distance_m: 5000, race_date: '', targetStr: '', base_weekly_km: 30,
      run_days: 5, long_run_weekday: 6, vdotMode: 'auto', manualVdot: 40,
      phaseMode: 'auto', startPhase: null,
      double_days: 0, double_mode: 'auto', strength_days: 0, pro_mode: false,
    },
    preview: null,
    loading: false,
    creating: false,

    get minWeeks() {
      return this.ctx.min_weeks[this.form.distance_m] || 8;
    },

    async init() {
      const { ok, data, error } = await tryCall('get_goal_wizard_context');
      if (!ok) { this.$dispatch('toast', { text: '读取向导数据失败: ' + error }); return; }
      this.ctx = data;
      this.form.base_weekly_km = data.avg_weekly_km_4w || 30;
      this.form.manualVdot = data.recent_vdot || 40;
      if (!this.form.race_date) this.form.race_date = addDays(data.today, this.minWeeks * 7);
      const plan = await tryCall('get_active_plan');
      this.hasPlan = !!plan.data;
    },
    async shown() {
      // 切回本页时重新确认是否已有计划（不动用户正在填写的表单）
      const plan = await tryCall('get_active_plan');
      this.hasPlan = !!plan.data;
      // 手动导入/补录刷新 PB 后：当前水平/成绩预估/各区配速即时更新
      // （ctx 只做展示，与表单相互独立，随时可安全替换）
      const { ok, data } = await tryCall('get_goal_wizard_context');
      if (ok) this.ctx = data;
    },
    async syncRefresh() {
      // 同步完成 → 当前水平预估（VDOT/成绩预估）实时更新；
      // 只替换 ctx 展示数据，不动用户正在填写的表单
      const { ok, data } = await tryCall('get_goal_wizard_context');
      if (ok) this.ctx = data;
    },

    next1() {
      if (!this.form.race_date) this.form.race_date = addDays(this.today, this.minWeeks * 7);
      this.step = 2;
    },
    async next2() {
      if (!this.form.race_date) { this.$dispatch('toast', { text: '请选择比赛日期' }); return; }
      if (!this.form.base_weekly_km || this.form.base_weekly_km < 10) {
        this.$dispatch('toast', { text: '周跑量至少 10km' }); return;
      }
      this.step = 3;
      this.preview = null;
      this.loading = true;
      const { ok, data, error } = await tryCall('preview_plan', this.params());
      this.loading = false;
      if (!ok) { this.$dispatch('toast', { text: '生成失败: ' + error }); this.step = 2; return; }
      this.preview = data;
    },
    params() {
      const target = parseTarget(this.form.targetStr);
      return {
        goal: {
          distance_m: this.form.distance_m, race_date: this.form.race_date,
          target_seconds: target, vdot: this.form.vdotMode === 'manual' ? this.form.manualVdot : null,
          name: DISTANCES.find(d => d.value === this.form.distance_m).label,
        },
        plan: {
          base_weekly_km: this.form.base_weekly_km, run_days: this.form.run_days,
          long_run_weekday: this.form.long_run_weekday,
          start_phase: this.form.phaseMode === 'manual' ? (this.form.startPhase || null) : 'auto',
          double_days: this.form.double_days, double_mode: this.form.double_mode,
          strength_days: this.form.strength_days, pro_mode: this.form.pro_mode ? 1 : 0,
        },
      };
    },
    async createPlan() {
      this.creating = true;
      const { ok, data, error } = await tryCall('create_goal_and_plan', this.params());
      this.creating = false;
      if (!ok) { this.$dispatch('toast', { text: '生成失败: ' + error }); return; }
      this.$dispatch('toast', { text: `课表已生成（${data.total_weeks} 周，${data.workouts.length} 节课）` });
      // 新计划影响仪表盘今日课/负荷/倒计时与健康页对照 → 其余页立即刷新
      window.dispatchEvent(new Event('data-changed'));
      setTimeout(() => { location.hash = '#/calendar'; }, 600);
    },

    fmtPace, fmtTime, fmtDate,
    // 课型 → 强度区间显示（与仪表盘/日历一致：label + %VDOT 带）
    zoneOf(kind) {
      const z = KIND_ZONES[kind];
      if (!z) return '';
      return z.band ? z.label + ' · ' + z.band : z.label;
    },
  }));
}
