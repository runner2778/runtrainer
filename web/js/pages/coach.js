import { tryCall } from '../api.js';

const ACTION_LABELS = {
  keep: '维持原计划', modify: '修改', decrease: '减量', rest: '休息',
  skip: '跳过', add_easy: '加练', shift: '挪课',
};
const KIND_LABELS = {
  E: '轻松跑', M: '马拉松配速', T: '阈值跑', I: '间歇跑', R: '重复跑',
  LR: '长距离', RECOVERY: '恢复跑', TUNEUP: '测试赛', RACE: '比赛', CROSS: '交叉训练',
};
const ZONE_LABELS = { E: '轻松配速', M: '马拉松配速', T: '阈值配速', I: '间歇配速', R: '重复配速' };
const STATUS_LABELS = { pending: '待处理', approved: '已批准', rejected: '已拒绝', applied: '已生效' };
const READINESS = {
  good: { label: '状态良好', cls: 'ok' },
  ok: { label: '状态一般', cls: 'warn' },
  low: { label: '需要恢复', cls: 'bad' },
};

function fmtDate(iso) {
  if (!iso) return '--';
  const [y, m, d] = iso.split('-');
  return `${y}年${Number(m)}月${Number(d)}日`;
}
function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function fmtKm(v) {
  return v == null ? '--' : `${v} km`;
}
function fmtMin(v) {
  return v == null ? '--' : `${v} 分钟`;
}

const HTML = `
<div class="card" x-show="!loading && !hasActivePlan">
  <h2>AI 教练</h2>
  <p class="muted">还没有训练计划。先到 <a href="#/goal">训练目标</a> 页生成课表，AI 教练会每天根据你的睡眠、HRV、心率等数据给出调整建议。</p>
</div>

<div class="card" x-show="loading">
  <h2>AI 教练</h2><p class="muted">加载中…</p>
</div>

<template x-if="!loading && hasActivePlan">
  <div>
    <div class="card">
      <div class="flex between">
        <h2>今日 AI 教练建议 <span class="muted" x-text="fmtDate(today)"></span></h2>
        <button class="btn ghost" @click="refresh()">↻ 刷新</button>
      </div>

      <template x-if="!advice">
        <div class="mv-center mt16">
          <p class="muted">AI 教练会综合近 14 天睡眠 / HRV / 静息心率 / 压力与本周课表，给出今日调整建议。</p>
          <div class="flex mt16">
            <button class="btn primary big" @click="generate(false)" :disabled="working">
              <span x-text="working ? 'AI 思考中…' : '🤖 生成今日建议'"></span>
            </button>
            <button class="btn big" @click="askExtra()" :disabled="working" title="今天想多练一次？AI 评估恢复状态后决定">
              ➕ 今天想加练
            </button>
          </div>
        </div>
      </template>

      <template x-if="advice">
        <div class="mt8">
          <div class="flex between">
            <p class="advice-summary"><b x-text="advice.summary"></b></p>
            <span class="badge" :class="'st-' + (READINESS[advice.readiness] || {}).cls"
                  x-text="(READINESS[advice.readiness] || {}).label || advice.readiness"></span>
          </div>
          <ul class="signals mt8">
            <template x-for="s in advice.key_signals" :key="s"><li x-text="s"></li></template>
          </ul>

          <h3 class="mt16">调整建议（原计划 → 建议）</h3>
          <template x-for="a in advice.adjustments" :key="a.id">
            <div class="diff-row">
              <div class="flex between">
                <div>
                  <span class="badge" :class="'kind-' + (a.workout ? a.workout.kind : (a.changes ? a.changes.kind : 'E'))"
                        x-text="KIND_LABELS[(a.workout && a.workout.kind) || (a.changes && a.changes.kind)] || '—'"></span>
                  <b x-text="fmtDate(a.applies_date)"></b>
                  <span class="muted" x-text="ACTION_LABELS[a.action] || a.action"></span>
                </div>
                <span class="badge" :class="'st-' + a.status" x-text="STATUS_LABELS[a.status] || a.status"></span>
              </div>
              <div class="diff-body mt8">
                <div class="diff-col" x-show="a.workout">
                  <p class="muted">原计划</p>
                  <p x-text="a.workout.title"></p>
                  <p class="muted" x-text="fmtKm(a.workout.distance_km) + ' · ' + fmtMin(a.workout.duration_min)"></p>
                </div>
                <div class="diff-arrow" x-show="a.workout && a.action !== 'keep'">→</div>
                <div class="diff-col" x-show="a.changes">
                  <p class="muted" x-text="a.action === 'add_easy' ? '加练建议' : '建议改为'"></p>
                  <p>
                    <span x-show="a.changes.kind" x-text="KIND_LABELS[a.changes.kind]"></span>
                    <span x-show="a.changes.pace_zone" class="muted"
                          x-text="' @ ' + (ZONE_LABELS[a.changes.pace_zone] || a.changes.pace_zone)"></span>
                  </p>
                  <p class="muted" x-show="a.changes.distance_km || a.changes.duration_min"
                     x-text="fmtKm(a.changes.distance_km) + ' · ' + fmtMin(a.changes.duration_min)"></p>
                </div>
                <div class="diff-col grow" x-show="!a.workout && a.changes">
                  <p class="muted" x-text="a.action === 'add_easy' ? '加练建议' : '调整'"></p>
                  <p x-text="fmtKm(a.changes.distance_km) + ' · ' + fmtMin(a.changes.duration_min)"></p>
                </div>
              </div>
              <p class="reason">💬 <span x-text="a.reason"></span></p>
              <details class="muted mt8" x-show="a.guardrail_log && a.guardrail_log.length">
                <summary>护栏日志</summary>
                <ul><template x-for="g in a.guardrail_log" :key="g"><li x-text="g"></li></template></ul>
              </details>
            </div>
          </template>

          <p class="mt8 muted" x-show="advice.weekly_notes">📋 本周：<span x-text="advice.weekly_notes"></span></p>

          <div class="flex mt16" x-show="allPending">
            <button class="btn primary" @click="decide(true)" :disabled="working">✅ 批准并应用到课表</button>
            <button class="btn" @click="decide(false)" :disabled="working">✖ 拒绝（维持原计划）</button>
          </div>
          <p class="muted mt8" x-show="!allPending && advice.adjustments.length">本日建议已处理，可在下方历史中查看。</p>
        </div>
      </template>

      <p class="banner warn mt16" x-show="error" x-text="error"></p>
    </div>

    <div class="card">
      <div class="flex between">
        <h2>教练聊天 <span class="muted">训练感受 · 改课请求 · 身体反馈</span></h2>
        <div class="flex gap8">
          <span class="chip ai-chip" x-show="aiModelText"
                :title="'当前实际使用的 AI 模型（读本地设置）：在「设置 → AI 教练」确定后锁定；' +
                        '想换模型需手动点更改配置再重新确定。'">
            🤖 <b x-text="aiModelText"></b>
            <template x-if="lastReplySec"><span class="muted">· 上条回复 <b x-text="lastReplySec"></b> 秒</span></template>
          </span>
          <button class="btn ghost" @click="clearChat()"
                  title="清空聊天显示并开启新对话（教练仍记得之前的交流与你的训练数据）">
            <span x-text="_confirmClear ? '再点一次确认清空' : '🗑 清空对话'"></span>
          </button>
        </div>
      </div>
      <div class="mv-center mt8" x-show="chatHasMore">
        <button class="btn ghost small" @click="loadEarlier()">↑ 加载更早的消息</button>
      </div>
      <div class="chat-log mt8" id="coach-chat-log" @scroll="onChatScroll()">
        <p class="muted mv-center" x-show="!messages.length">和教练聊聊吧：想改哪天的课、最近的身体感觉、出差安排……教练会结合你的健康数据与课表回答，需要改课时会给出可批准的调整。</p>
        <template x-for="m in messages" :key="m.id">
          <div class="chat-row" :class="'chat-' + m.role">
            <div class="chat-bubble" :class="m.kind === 'sync_analysis' ? 'sync' : ''">
              <p class="muted sync-tag" x-show="m.kind === 'sync_analysis'">📊 同步后自动分析</p>
              <p class="chat-text" x-text="m.content"></p>
              <div class="mt8" x-show="m.adjustments && m.adjustments.length">
                <p class="muted" x-text="m.auto_applied
                  ? '✅ 已按你的要求直接改到课表（日历已更新）：'
                  : '调整建议（批准后应用到课表）：'"></p>
                <template x-for="a in m.adjustments" :key="a.id">
                  <div class="diff-row">
                    <div class="flex between">
                      <div>
                        <span class="badge" :class="'kind-' + (a.workout ? a.workout.kind : (a.changes ? a.changes.kind : 'E'))"
                              x-text="KIND_LABELS[(a.workout && a.workout.kind) || (a.changes && a.changes.kind)] || '—'"></span>
                        <b x-text="fmtDate(a.applies_date)"></b>
                        <span class="muted" x-text="ACTION_LABELS[a.action] || a.action"></span>
                      </div>
                      <div>
                        <template x-if="a.status === 'pending'">
                          <span class="flex">
                            <button class="btn small primary" :data-cmd="'chat-approve'" :data-mid="m.id">✓ 批准</button>
                            <button class="btn small" :data-cmd="'chat-reject'" :data-mid="m.id">✖ 拒绝</button>
                          </span>
                        </template>
                        <span class="badge" :class="'st-' + a.status" x-show="a.status !== 'pending'"
                              x-text="STATUS_LABELS[a.status] || a.status"></span>
                      </div>
                    </div>
                    <div class="diff-body mt8">
                      <div class="diff-col" x-show="a.workout">
                        <p class="muted">原计划</p>
                        <p x-text="a.workout.title"></p>
                      </div>
                      <div class="diff-arrow" x-show="a.workout && a.action !== 'keep'">→</div>
                      <div class="diff-col" x-show="a.changes">
                        <p class="muted" x-text="a.action === 'add_easy' ? '加练建议' : '建议改为'"></p>
                        <p>
                          <span x-show="a.changes.kind" x-text="KIND_LABELS[a.changes.kind]"></span>
                          <span x-show="a.changes.pace_zone" class="muted"
                                x-text="' @ ' + (ZONE_LABELS[a.changes.pace_zone] || a.changes.pace_zone)"></span>
                        </p>
                        <p class="muted" x-show="a.changes.distance_km || a.changes.duration_min"
                           x-text="fmtKm(a.changes.distance_km) + ' · ' + fmtMin(a.changes.duration_min)"></p>
                      </div>
                      <div class="diff-col grow" x-show="!a.workout && a.changes">
                        <p x-text="fmtKm(a.changes.distance_km) + ' · ' + fmtMin(a.changes.duration_min)"></p>
                      </div>
                    </div>
                    <p class="reason">💬 <span x-text="a.reason"></span></p>
                  </div>
                </template>
              </div>
              <p class="muted mt4" x-show="m.profile_updates && Object.keys(m.profile_updates).length">
                📝 已更新档案：<span x-text="profileUpdatesText(m.profile_updates)"></span>
              </p>
            </div>
          </div>
        </template>
      </div>
      <div class="flex mt8">
        <textarea class="chat-input" x-model="chatInput" rows="2" :disabled="chatWorking"
          placeholder="例：这周末出差，把周日长距离换到周六（改课请求会直接改到课表）；或：我最大心率其实是 195"
          @keydown.enter.prevent.exact="send()"></textarea>
        <button class="btn primary" @click="send()" :disabled="chatWorking || !chatInput.trim()">
          <span x-text="chatWorking ? '思考中…' : '发送'"></span>
        </button>
      </div>
    </div>

    <div class="card" x-show="history.length">
      <h2>调整历史</h2>
      <table>
        <thead><tr><th>日期</th><th>动作</th><th>理由</th><th>状态</th></tr></thead>
        <tbody>
          <template x-for="h in history" :key="h.id">
            <tr>
              <td x-text="h.applies_date"></td>
              <td><span class="badge" :class="'kind-' + ((h.workout && h.workout.kind) || 'E')"
                        x-text="ACTION_LABELS[h.action] || h.action"></span></td>
              <td class="muted" x-text="h.reason"></td>
              <td><span class="badge" :class="'st-' + h.status" x-text="STATUS_LABELS[h.status] || h.status"></span></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>`;

export function initCoach() {
  const sec = document.getElementById('page-coach');
  sec.innerHTML = HTML;

  // 原生事件委托：本环境 WebView2 中 x-for 重复求值产生的克隆按钮会丢失
  // Alpine 绑定（@click 不触发），聊天调整的批准/拒绝按钮用 data-cmd 分发。
  sec.addEventListener('click', (e) => {
    const comp = window.Alpine && Alpine.$data(sec);
    if (!comp) return;
    const el = e.target.closest('[data-cmd]');
    if (el && el.dataset.mid) {
      const mid = parseInt(el.dataset.mid, 10);
      if (el.dataset.cmd === 'chat-approve') comp.chatDecide(mid, true);
      if (el.dataset.cmd === 'chat-reject') comp.chatDecide(mid, false);
    }
  });

  window.Alpine.data('coachPage', () => ({
    today: localToday(),
    hasActivePlan: false,
    advice: null,
    history: [],
    messages: [],
    chatHasMore: false,
    chatInput: '',
    chatWorking: false,
    loading: true,
    working: false,
    error: '',
    aiModelText: '',    // 教练页直观显示当前使用的模型（本地设置，不上 API）
    lastReplySec: '',   // 上一条聊天回复实测耗时（秒）
    // 是否吸附在聊天底部：用户向上翻历史时自动取消，发送消息/首载时置回
    _stickBottom: true,
    _confirmClear: false,
    ACTION_LABELS,
    KIND_LABELS,
    ZONE_LABELS,
    STATUS_LABELS,
    READINESS,

    get allPending() {
      return this.advice && this.advice.adjustments.some(a => a.status === 'pending');
    },

    async init() {
      await this.refresh();
    },
    async shown() {
      await this.refresh();
    },
    async refresh() {
      const { ok, data, error } = await tryCall('get_coach_snapshot');
      this.loading = false;
      if (!ok) { this.error = '读取教练数据失败: ' + error; return; }
      this.error = '';
      this.hasActivePlan = data.has_active_plan;
      this.advice = data.advice;
      this.history = data.history || [];
      await this.refreshModelInfo();
      await this.loadMessages();
    },
    // 当前实际使用的模型（读本地 get_settings；设置页「确定并锁定」后此处即更新）
    async refreshModelInfo() {
      const s = await tryCall('get_settings');
      if (!s.ok) return;
      const d = s.data || {};
      const prov = (d.ai_providers || []).find((p) => p.key === d.ai_provider);
      const modelNotes = {
        'glm-4.7-flash': '', 'glm-5.3-flash': '（思考常开，较慢）',
        'deepseek-v4-pro': '', 'deepseek-v4-flash': '',
      };
      this.aiModelText = (prov ? prov.label : (d.ai_provider || '—'))
        + ' · ' + (d.ai_model || '—') + (modelNotes[d.ai_model] || '')
        + (d.mock_mode ? '（Mock）' : '');
    },
    async loadMessages(limit = 60) {
      // 首屏只取最近 60 条（调整详情重，避免一次渲染上百条）；
      // 吸附底部时加载后滚到底，向上翻历史时保持视口位置不变
      const el = document.getElementById('coach-chat-log');
      const prevH = el ? el.scrollHeight : 0;
      const { ok, data } = await tryCall('get_chat_history', limit);
      if (!ok) return;
      this.messages = data || [];
      this.chatHasMore = (data || []).length >= limit;
      await this.$nextTick();
      if (this._stickBottom) this.scrollChat();
      else if (el) el.scrollTop += el.scrollHeight - prevH;
    },
    async loadEarlier() {
      this._stickBottom = false;
      await this.loadMessages(500);
    },
    onChatScroll() {
      const el = document.getElementById('coach-chat-log');
      if (!el) return;
      // 距底部超过 40px 视为用户在看历史，暂停自动滚底
      this._stickBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    },
    async clearChat() {
      // 两步确认（WebView2 内 window.confirm 行为不可靠，用按钮二次点击代替）
      if (!this._confirmClear) {
        this._confirmClear = true;
        setTimeout(() => { this._confirmClear = false; }, 4000);
        return;
      }
      this._confirmClear = false;
      const { ok, error } = await tryCall('clear_chat_history');
      if (!ok) { this.$dispatch('toast', { text: '清空失败: ' + error }); return; }
      this.messages = [];
      this.chatHasMore = false;
      this._stickBottom = true;
      this.$dispatch('toast', { text: '已开启新对话（教练保留对你的训练数据和此前交流的记忆）' });
    },
    async send() {
      const text = this.chatInput.trim();
      if (!text || this.chatWorking) return;
      this.chatInput = '';
      this.chatWorking = true;
      this._stickBottom = true;
      // 真实 API 响应可能数十秒：先乐观显示用户消息 + 思考中占位，避免空白等待
      this.messages.push({ id: 'local-' + Date.now(), role: 'user', content: text,
        adjustments: [], profile_updates: {} });
      const pending = { id: 'pending-' + Date.now(), role: 'coach', content: '教练思考中…',
        adjustments: [], profile_updates: {} };
      this.messages.push(pending);
      this.scrollChat();
      const t0 = performance.now();
      const { ok, data, error } = await tryCall('coach_chat', text);
      if (ok) {
        // 实测单条回复耗时（毫秒 → 秒），显示在页头模型徽章旁
        this.lastReplySec = (Math.round((performance.now() - t0) / 100) / 10).toFixed(1);
      }
      this.chatWorking = false;
      if (!ok) {
        this.messages = this.messages.filter(m => m.id !== pending.id);
        this.$dispatch('toast', { text: '教练回复失败: ' + error });
        this.scrollChat();
        return;
      }
      if (data.rebuild) {
        this.$dispatch('toast', { text: `课表已按最新水平重建（VDOT ${data.rebuild.vdot}）` });
      }
      await this.loadMessages();
      this.scrollChat();
      // 改课/重建可能已直接生效 → 其余页面（日历/仪表盘）立即重拉
      window.dispatchEvent(new Event('data-changed'));
    },
    async chatDecide(mid, approve) {
      const { ok, data, error } = await tryCall('decide_chat_adjustments', mid, approve);
      if (!ok) { this.$dispatch('toast', { text: '操作失败: ' + error }); return; }
      this.$dispatch('toast', {
        text: approve
          ? `已批准并应用到课表${data.errors && data.errors.length ? '（部分失败: ' + data.errors.join('；') + '）' : ''}`
          : '已拒绝，维持原计划',
      });
      await this.loadMessages();
      await this.refresh();
      window.dispatchEvent(new Event('data-changed'));
    },
    profileUpdatesText(u) {
      const labels = { max_hr: '最大心率', rest_hr: '静息心率', weight_kg: '体重', run_experience: '跑步经验' };
      return Object.entries(u)
        .map(([k, v]) => `${labels[k] || k} ${v}${k === 'weight_kg' ? ' kg' : ''}`)
        .join('、');
    },
    scrollChat() {
      const el = document.getElementById('coach-chat-log');
      if (el) el.scrollTop = el.scrollHeight;
    },
    async generate(extra) {
      this.working = true;
      const { ok, data, error } = await tryCall('request_coach_advice', extra, extra ? this.note : '');
      this.working = false;
      if (!ok) {
        this.error = error;
        this.$dispatch('toast', { text: 'AI 建议失败: ' + error });
        return;
      }
      await this.refresh();
      this.$dispatch('toast', { text: '今日建议已生成' });
    },
    askExtra() {
      this.note = window.prompt('今天想加练？可以写一句原因（如：周末想多跑点）：', '');
      if (this.note === null) return;
      this.generate(true);
    },
    async decide(approve) {
      this.working = true;
      const { ok, data, error } = await tryCall('decide_coach_advice', approve);
      this.working = false;
      if (!ok) { this.$dispatch('toast', { text: '操作失败: ' + error }); return; }
      this.$dispatch('toast', {
        text: approve
          ? `已批准并应用到课表${data.errors.length ? '（部分失败: ' + data.errors.join('；') + '）' : ''}`
          : '已拒绝，维持原计划',
      });
      await this.refresh();
      window.dispatchEvent(new Event('data-changed'));
    },

    fmtDate, fmtKm, fmtMin,
  }));
}
