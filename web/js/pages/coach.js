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
              <span x-text="working ? 'DeepSeek 思考中…' : '🤖 生成今日建议'"></span>
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
      </div>
      <div class="chat-log mt8" id="coach-chat-log">
        <p class="muted mv-center" x-show="!messages.length">和教练聊聊吧：想改哪天的课、最近的身体感觉、出差安排……教练会结合你的健康数据与课表回答，需要改课时会给出可批准的调整。</p>
        <template x-for="m in messages" :key="m.id">
          <div class="chat-row" :class="'chat-' + m.role">
            <div class="chat-bubble">
              <p class="chat-text" x-text="m.content"></p>
              <div class="mt8" x-show="m.adjustments && m.adjustments.length">
                <p class="muted">调整建议（批准后应用到课表）：</p>
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
          placeholder="例：这周末出差，把周日长距离换到周六；或：我最大心率其实是 195"
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
    chatInput: '',
    chatWorking: false,
    loading: true,
    working: false,
    error: '',
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
      await this.loadMessages();
    },
    async loadMessages() {
      const { ok, data } = await tryCall('get_chat_history', 100);
      if (ok) this.messages = data || [];
    },
    async send() {
      const text = this.chatInput.trim();
      if (!text || this.chatWorking) return;
      this.chatInput = '';
      this.chatWorking = true;
      // 真实 API 响应可能数十秒：先乐观显示用户消息 + 思考中占位，避免空白等待
      this.messages.push({ id: 'local-' + Date.now(), role: 'user', content: text,
        adjustments: [], profile_updates: {} });
      const pending = { id: 'pending-' + Date.now(), role: 'coach', content: '教练思考中…',
        adjustments: [], profile_updates: {} };
      this.messages.push(pending);
      this.scrollChat();
      const { ok, data, error } = await tryCall('coach_chat', text);
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
    },

    fmtDate, fmtKm, fmtMin,
  }));
}
