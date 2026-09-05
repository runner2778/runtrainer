import { initActivities } from './pages/activities.js';
import { initCalendar } from './pages/calendar.js';
import { initCoach } from './pages/coach.js';
import { initDashboard } from './pages/dashboard.js';
import { initGoal } from './pages/goal.js';
import { initHealth } from './pages/health.js';
import { initSettings } from './pages/settings.js';

const PAGES = ['dashboard', 'calendar', 'goal', 'coach', 'activities', 'health', 'settings'];

// 跨页共享状态放在 Alpine.store：
// 本环境的 WebView2 中，命名 x-data 组件之间不共享父作用域（子组件读不到
// 父组件属性），$store 是唯一可靠的跨组件通道。
function registerStore() {
  const store = {
    page: 'settings',
    theme: localStorage.getItem('runtrainer-theme') || 'dark',
    toast: { show: false, text: '' },
    syncBanner: '',
    get effectiveTheme() {
      return this.theme === 'system'
        ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
        : this.theme;
    },
    applyTheme() {
      document.documentElement.dataset.theme = this.effectiveTheme;
    },
    setTheme(t) {
      this.theme = t;
      localStorage.setItem('runtrainer-theme', t);
      this.applyTheme();
    },
    toggleTheme() {
      this.setTheme(this.theme === 'light' ? 'dark' : 'light');
    },
    navigate(p) {
      this.page = PAGES.includes(p) ? p : 'settings';
      location.hash = '#/' + this.page;
    },
    onRoute() {
      const h = location.hash.replace(/^#\//, '') || 'settings';
      this.page = PAGES.includes(h) ? h : 'settings';
      // 通知当前页可见（图表懒初始化 + resize）。30ms：实测 WebView2 里
      // Alpine x-show 的可见性更新可能晚于 setTimeout(0)，0 延迟时
      // 页面容器仍 display:none（健康页图表因此渲染被推迟）
      setTimeout(() => {
        const sec = document.getElementById('page-' + this.page);
        const data = sec && window.Alpine && window.Alpine.$data(sec);
        if (data && typeof data.shown === 'function') data.shown();
      }, 30);
    },
    showToast(text, ms = 2600) {
      this.toast = { show: true, text };
      setTimeout(() => { this.toast = { show: false, text: '' }; }, ms);
    },
  };
  window.Alpine.store('app', store);
  // 注意：Alpine.store 里存的是 reactive 代理，必须用代理做后续变更，
  // 改原始对象不会触发响应式。
  return window.Alpine.store('app');
}

function registerPages() {
  initDashboard();
  initSettings();
  initActivities();
  initHealth();
  initCalendar();
  initGoal();
  initCoach();
}

async function loadBackendPrefs(store) {
  // 主题以后端设置为准（设置页保存到后端）；无后端值时保持默认暗色
  try {
    const { call } = await import('./api.js');
    const data = await call('get_settings');
    if (data && data.theme) store.setTheme(data.theme);
  } catch (e) { /* 后端不可用时维持本地主题 */ }
}

async function autoSync(store) {
  // 启动自动同步：已配置真实 Garmin 账号且非 mock 模式时拉一次最新数据
  try {
    const { call } = await import('./api.js');
    const s = await call('get_settings');
    if (!s) return;
    if (!s.has_garmin_password || s.mock_mode) return;
    const before = (s.sync_states || []).find((x) => x.source === 'garmin') || {};
    store.syncBanner = '🔄 正在同步 Garmin 数据…';
    await call('sync_garmin');
    // 轮询等待本次同步结束（last_sync_ts 变化 = 本次尝试完成）
    const prevTs = before.last_sync_ts || 0;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      let rows;
      try { rows = await call('get_sync_states'); } catch (e) { continue; }
      const row = (rows || []).find((x) => x.source === 'garmin');
      if (!row || (row.last_sync_ts || 0) === prevTs) continue;
      const st = (row.meta && row.meta.last_stats) || {};
      if (row.last_error) {
        store.syncBanner = `⚠️ 同步失败：${row.last_error}`;
      } else {
        const parts = [];
        if (st.activities) parts.push(`新增活动 ${st.activities} 条`);
        if (st.health_days) parts.push(`健康数据 ${st.health_days} 天`);
        if (st.health_backfill) parts.push(st.health_backfill);
        if (st.health_error) parts.push(`健康数据本轮未拉到（${st.health_error.slice(0, 40)}）`);
        if (st.plan_rebuilt) parts.push(`课表已按最新 VDOT ${st.plan_vdot} 动态更新`);
        store.syncBanner = `✅ 同步完成${parts.length ? '：' + parts.join('，') : ''}`;
      }
      // 同步结束（可能重建了课表/更新了数据）→ 通知当前页刷新
      window.dispatchEvent(new Event('sync-done'));
      return;
    }
    store.syncBanner = '⏳ 同步仍在进行中，请稍后在设置页查看';
  } catch (e) {
    store.syncBanner = '';
  }
}

function boot() {
  // 必须等 pywebview 注入 js_api 后再启动：组件 init() 里的桥调用
  // 在 api 就绪前发起会静默挂起（设置/活动页数据为空的原因）
  if (!window.Alpine || !window.pywebview || !window.pywebview.api) {
    setTimeout(boot, 30);
    return;
  }
  const store = registerStore();
  registerPages();
  store.applyTheme();
  store.onRoute();
  window.addEventListener('hashchange', () => store.onRoute());
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => store.applyTheme());
  // 剪贴板联动：WebView2 默认不放开粘贴，输入框内 Ctrl+V 时经 pywebview
  // 读系统剪贴板回填（复制 apikey 等长串内容可直接粘贴）
  const pasteInto = async (t) => {
    try {
      const { call } = await import('./api.js');
      const data = await call('read_clipboard');
      const text = (data && data.text) || '';
      if (!text) return false;
      const s = t.selectionStart != null ? t.selectionStart : t.value.length;
      const p = t.selectionEnd != null ? t.selectionEnd : t.value.length;
      t.value = t.value.slice(0, s) + text + t.value.slice(p);
      t.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    } catch (err) { return false; }
  };
  const isEditable = (t) => t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement;
  document.addEventListener('keydown', async (e) => {
    if (!(e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 'v' || e.key === 'V'))) return;
    if (!isEditable(e.target)) return;
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.read_clipboard) return;
    e.preventDefault();
    await pasteInto(e.target);
  });
  // paste 事件兜底：右键粘贴等非 Ctrl+V 路径；WebView2 若提供 clipboardData
  // 直接取用（微信富文本复制场景），否则退回系统剪贴板
  document.addEventListener('paste', async (e) => {
    if (!isEditable(e.target)) return;
    e.preventDefault();
    let text = '';
    try { text = e.clipboardData ? e.clipboardData.getData('text') : ''; } catch (err) { /* ignore */ }
    if (text) {
      const t = e.target;
      const s = t.selectionStart != null ? t.selectionStart : t.value.length;
      const p = t.selectionEnd != null ? t.selectionEnd : t.value.length;
      t.value = t.value.slice(0, s) + text + t.value.slice(p);
      t.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      await pasteInto(e.target);
    }
  });
  // 同步完成（可能重建课表）→ 刷新当前页；仪表盘无视 2s 防双载强制刷新
  window.addEventListener('sync-done', () => {
    const sec = document.getElementById('page-' + store.page);
    const data = sec && window.Alpine && window.Alpine.$data(sec);
    if (data && typeof data.syncRefresh === 'function') data.syncRefresh();
    else if (data && typeof data.shown === 'function') data.shown();
  });
  window.Alpine.start();
  loadBackendPrefs(store);
  autoSync(store);
}

boot();
