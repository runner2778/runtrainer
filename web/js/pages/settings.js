import { call, tryCall } from '../api.js';

const HTML = `
<div class="grid cols-2">
  <div>
    <div class="card">
      <h2>个人档案</h2>
      <div class="form-row"><label>昵称</label><input x-model="profile.nickname"></div>
      <div class="form-row"><label>性别</label>
        <select x-model="profile.sex">
          <option value="">未设置</option><option value="male">男</option><option value="female">女</option>
        </select>
      </div>
      <div class="form-row"><label>出生年份</label><input type="number" x-model="profile.birth_year" min="1930" max="2020"></div>
      <div class="form-row"><label>身高 (cm)</label><input type="number" x-model="profile.height_cm" step="0.1"></div>
      <div class="form-row"><label>体重 (kg)</label><input type="number" x-model="profile.weight_kg" step="0.1"></div>
      <div class="form-row"><label>最大心率（Garmin 同步后可自动填充）</label><input type="number" x-model="profile.max_hr"></div>
      <div class="form-row"><label>静息心率</label><input type="number" x-model="profile.rest_hr"></div>
      <div class="form-row"><label>跑龄水平</label>
        <select x-model="profile.run_experience">
          <option value="beginner">初级</option>
          <option value="intermediate">中级</option>
          <option value="advanced">高级</option>
        </select>
      </div>
      <div class="form-row"><label>VO2max（Garmin 同步自动更新）</label>
        <input type="number" x-model="profile.vo2max" step="0.1" readonly>
      </div>
      <button class="btn primary" @click="saveProfile()">保存档案</button>
    </div>

    <div class="card">
      <h2>AI 教练</h2>
      <div class="form-row">
        <label>服务商</label>
        <select x-model="aiProvider" @change="saveAiProvider()">
          <template x-for="p in aiProviders" :key="p.key">
            <option :value="p.key" x-text="p.label"></option>
          </template>
        </select>
      </div>
      <p class="muted mt8" x-text="aiHint()"></p>
      <template x-if="provFreeText()">
        <div class="form-row">
          <label>模型（输入本地已拉取的模型名）</label>
          <input x-model="aiModel" list="ai-model-suggest" @change="saveAiModel()">
          <datalist id="ai-model-suggest">
            <template x-for="m in modelOptions()" :key="m"><option :value="m"></option></template>
          </datalist>
        </div>
      </template>
      <template x-if="!provFreeText()">
        <div class="form-row">
          <label>模型</label>
          <select x-model="aiModel" @change="saveAiModel()">
            <template x-for="m in modelOptions()" :key="m">
              <option :value="m" x-text="modelLabel(m)"></option>
            </template>
          </select>
        </div>
      </template>
      <template x-if="provNeedsKey()">
        <div class="form-row">
          <label>API Key（存于 Windows 凭据管理器，不落盘）</label>
          <div class="flex">
            <input type="password" :placeholder="keyPlaceholder()" x-model="aiKey" style="flex:1">
            <button class="btn small" @click="pasteInto('aiKey')">📋 粘贴</button>
          </div>
        </div>
        <div class="flex">
          <button class="btn primary" @click="saveAiKey()" :disabled="!aiKey">保存 Key</button>
          <button class="btn" x-show="hasAiKey()" @click="clearAiKey()">清除</button>
          <span class="badge soft" x-show="hasAiKey()">已配置</span>
        </div>
      </template>
      <p class="muted mt8">教练 AI 每天最多自动调用一次。Mock 模式下完全由本地样例模拟，不消耗任何服务商额度。</p>
    </div>
  </div>

  <div>
    <div class="card">
      <h2>Garmin 数据同步</h2>
      <template x-if="!hasGarminPassword">
        <div>
          <div class="form-row"><label>Garmin Connect 账号</label><input x-model="garminUsername"></div>
          <div class="form-row"><label>密码</label>
            <div class="flex">
              <input type="password" x-model="garminPassword" style="flex:1">
              <button class="btn small" @click="pasteInto('garminPassword')">📋 粘贴</button>
            </div>
          </div>
          <div class="flex">
            <button class="btn primary" @click="saveGarmin()" :disabled="!garminUsername || !garminPassword">保存账号</button>
          </div>
        </div>
      </template>
      <template x-if="hasGarminPassword">
        <div>
          <p>已配置账号：<b x-text="garminUsername"></b>
            <span class="muted" x-show="profile.nickname">（<span x-text="profile.nickname"></span>）</span></p>
          <div class="flex mt8">
            <button class="btn primary" @click="syncNow()" :disabled="syncing">
              <span x-text="syncing ? '同步中…' : '立即同步'"></span>
            </button>
            <button class="btn" @click="clearGarmin()">清除账号</button>
          </div>
        </div>
      </template>
      <div class="form-row mt8">
        <label class="flex">
          <input type="checkbox" style="width:auto" x-model="garminCn" @change="saveGarminCn()">
          <span>中国区账号（connect.garmin.cn 独立服务器）</span>
        </label>
      </div>
      <p class="muted mt8">官方 Health API 暂不接受新申请，本应用通过账号凭据自动拉取数据；若同步失败可随时手动导入 FIT 文件。</p>
    </div>

    <div class="card">
      <h2>同步状态</h2>
      <table>
        <thead><tr><th>来源</th><th>上次同步</th><th>状态</th><th>本次结果</th></tr></thead>
        <tbody>
          <template x-for="s in syncStates" :key="s.source">
            <tr>
              <td x-text="s.source"></td>
              <td class="num" x-text="fmtTs(s.last_sync_ts)"></td>
              <td>
                <span class="status-pair" :class="s.last_error ? 'st-critical' : 'st-good'">
                  <span class="dot" :style="dotStyle(s.last_error)"></span>
                  <span x-text="s.last_error ? '失败' : '正常'"></span>
                </span>
                <p class="muted" x-show="s.last_error" x-text="s.last_error"></p>
              </td>
              <td class="muted" x-text="statsText(s)"></td>
            </tr>
          </template>
          <tr x-show="!syncStates.length"><td colspan="4" class="muted">暂无同步记录</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h2>外观与开发者选项</h2>
      <div class="form-row"><label>主题</label>
        <select x-model="theme" @change="saveTheme()">
          <option value="system">跟随系统</option>
          <option value="light">亮色</option>
          <option value="dark">暗色</option>
        </select>
      </div>
      <div class="form-row">
        <label class="flex">
          <input type="checkbox" style="width:auto" x-model="mockMode" @change="saveMockMode()">
          <span>Mock 模式（用样例数据替代 Garmin / DeepSeek，便于演示开发）</span>
        </label>
      </div>
    </div>
  </div>
</div>`;

export function initSettings() {
  const sec = document.getElementById('page-settings');
  sec.innerHTML = HTML;

  window.Alpine.data('settingsPage', () => ({
    profile: { nickname: '', sex: '', birth_year: null, height_cm: null, weight_kg: null, max_hr: null, rest_hr: null, run_experience: 'intermediate' },
    garminUsername: '',
    garminPassword: '',
    hasGarminPassword: false,
    garminCn: true,
    aiProvider: 'deepseek',
    aiProviders: [],
    aiKeys: {},
    aiKey: '',
    aiModel: 'deepseek-v4-pro',
    theme: 'system',
    mockMode: true,
    syncStates: [],
    syncing: false,

    async init() {
      const { ok, data, error } = await tryCall('get_settings');
      if (!ok) { this.$dispatch('toast', { text: '读取设置失败: ' + error }); return; }
      this.profile = { ...this.profile, ...(data.profile || {}) };
      this.garminUsername = data.garmin_username || '';
      this.hasGarminPassword = data.has_garmin_password;
      this.garminCn = data.garmin_cn;
      this.aiProvider = data.ai_provider || 'deepseek';
      this.aiProviders = data.ai_providers || [];
      this.aiKeys = data.ai_keys || {};
      this.aiModel = data.ai_model;
      this.theme = data.theme;
      this.mockMode = data.mock_mode;
      this.syncStates = data.sync_states || [];
    },
    // 切到设置页时刷新（同步状态/档案可能已变化；也兜底启动时 init 未跑成的情况）
    async shown() { await this.init(); },

    async pasteInto(field) {
      const { ok, data, error } = await tryCall('read_clipboard');
      if (!ok || !data || !data.text) { this.$dispatch('toast', { text: '剪贴板无文本内容' }); return; }
      this[field] = data.text;
      this.$dispatch('toast', { text: '已从剪贴板粘贴' });
    },

    dotStyle(err) { return `background: ${err ? 'var(--st-critical)' : 'var(--st-good)'}`; },
    statsText(s) {
      const st = (s.meta && s.meta.last_stats) || {};
      if (!st.activities && !st.health_days && !st.plan_rebuilt && !st.health_backfill
        && !st.auto_analysis) return '—';
      const parts = [];
      if (st.activities) parts.push(`活动 +${st.activities}`);
      if (st.health_days) parts.push(`健康 ${st.health_days} 天`);
      if (st.health_backfill) parts.push(st.health_backfill);
      if (st.health_error) parts.push(`健康本轮未拉到`);
      if (st.plan_rebuilt) parts.push(`课表已按 VDOT ${st.plan_vdot} 更新`);
      if (st.mock_purged) parts.push(`清理演示数据 ${st.mock_purged} 条`);
      if (st.auto_analysis) parts.push(`📊 ${st.auto_analysis}`);
      if (st.auto_analysis_error) parts.push(st.auto_analysis_error);
      return parts.join('，');
    },
    fmtTs(ts) {
      if (!ts) return '—';
      const d = new Date(ts * 1000);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    },

    async saveProfile() {
      const { ok, error } = await tryCall('save_profile', this.profile);
      ok ? this.$dispatch('toast', { text: '档案已保存' }) : this.$dispatch('toast', { text: '保存失败: ' + error });
    },
    async saveGarmin() {
      const { ok, error } = await tryCall('save_garmin_credentials', this.garminUsername, this.garminPassword);
      if (!ok) { this.$dispatch('toast', { text: '保存失败: ' + error }); return; }
      this.hasGarminPassword = true;
      this.garminPassword = '';
      this.$dispatch('toast', { text: 'Garmin 账号已保存' });
    },
    async clearGarmin() {
      await tryCall('clear_garmin_credentials');
      this.hasGarminPassword = false;
      this.garminUsername = '';
      this.$dispatch('toast', { text: '账号已清除' });
    },
    async syncNow() {
      this.syncing = true;
      const { ok, error } = await tryCall('sync_garmin');
      if (!ok) { this.syncing = false; this.$dispatch('toast', { text: '同步失败: ' + error }); return; }
      // 同步在后台线程执行：轮询 sync_state，等 last_sync_ts 变化即本次尝试结束
      const before = (this.syncStates.find(s => s.source === 'garmin') || {}).last_sync_ts || 0;
      const deadline = Date.now() + 120000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const s = await tryCall('get_sync_states');
        if (!s.ok) continue;
        const row = (s.data || []).find((x) => x.source === 'garmin');
        if (!row) continue;
        this.syncStates = s.data;
        if ((row.last_sync_ts || 0) !== before) {
          this.syncing = false;
          if (row.last_error) { this.$dispatch('toast', { text: '同步失败: ' + row.last_error }); }
          else {
            this.$dispatch('toast', { text: '同步完成：' + this.statsText(row), ms: 4000 });
            this.$dispatch('sync-done');
          }
          await this.init();
          return;
        }
      }
      this.syncing = false;
      this.$dispatch('toast', { text: '同步超时（仍在进行），请稍后查看同步状态表' });
    },
    // ---- AI 服务商 ----
    provInfo() { return (this.aiProviders || []).find((p) => p.key === this.aiProvider) || null; },
    provNeedsKey() { const p = this.provInfo(); return !!(p && p.needs_key); },
    provFreeText() { const p = this.provInfo(); return !!(p && p.free_text); },
    modelOptions() { const p = this.provInfo(); return (p && p.models) || []; },
    modelLabel(m) {
      const d = {
        'deepseek-v4-pro': 'deepseek-v4-pro（推理更强，推荐）',
        'deepseek-v4-flash': 'deepseek-v4-flash（便宜快速）',
        'glm-4.7-flash': 'glm-4.7-flash（永久免费，已关思考提速）',
      };
      return d[m] || m;
    },
    aiHint() { const p = this.provInfo(); return p ? p.hint : ''; },
    keyPlaceholder() {
      return this.aiProvider === 'deepseek' ? 'sk-...（platform.deepseek.com 获取）' : 'API Key（服务商控制台获取）';
    },
    hasAiKey() { return !!(this.aiKeys || {})[this.aiProvider]; },
    async saveAiProvider() {
      const { ok, error } = await tryCall('set_setting', 'ai_provider', this.aiProvider);
      if (!ok) { this.$dispatch('toast', { text: '切换失败: ' + error }); await this.init(); return; }
      // 模型名不在新服务商候选列表时，落到其默认模型并保存（与后端回落逻辑一致）
      const info = this.provInfo();
      if (info && info.models && info.models.length && !info.models.includes(this.aiModel)) {
        this.aiModel = info.models[0];
        await tryCall('set_setting', 'ai_model', this.aiModel);
      }
      this.aiKey = '';
      this.$dispatch('toast', { text: '已切换到「' + (info ? info.label : this.aiProvider) + '」' });
    },
    async saveAiKey() {
      const { ok, error } = await tryCall('save_ai_key', this.aiProvider, this.aiKey);
      if (!ok) { this.$dispatch('toast', { text: '保存失败: ' + error }); return; }
      this.aiKeys = { ...this.aiKeys, [this.aiProvider]: true };
      this.aiKey = '';
      this.$dispatch('toast', { text: 'API Key 已保存' });
    },
    async clearAiKey() {
      const { ok, error } = await tryCall('clear_ai_key', this.aiProvider);
      if (!ok) { this.$dispatch('toast', { text: '清除失败: ' + error }); return; }
      this.aiKeys = { ...this.aiKeys, [this.aiProvider]: false };
      this.$dispatch('toast', { text: 'Key 已清除' });
    },
    async saveAiModel() { await tryCall('set_setting', 'ai_model', this.aiModel); },
    async saveTheme() {
      await tryCall('set_setting', 'theme', this.theme);
      this.$dispatch('theme-changed', { theme: this.theme });
    },
    async saveMockMode() { await tryCall('set_setting', 'mock_mode', this.mockMode ? '1' : '0'); },
    async saveGarminCn() { await tryCall('set_setting', 'garmin_cn', this.garminCn ? '1' : '0'); },
  }));
}
