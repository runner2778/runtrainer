"""真实数据库 + 真实窗口 UI 验证：黑红主题 / 真实数据渲染 / 弹窗关闭无暗屏残留。

用法：.venv\\Scripts\\python tools\\verify_ui.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402
from runtrainer.db import database  # noqa: E402

database.migrate()  # 直接起 web 服务不经过 app 启动流程，探针自行执行迁移

url = _start_web_server()
print("加载:", url, flush=True)

window = webview.create_window("验证", url, js_api=Api(), width=1200, height=800)
out = {}


def js(expr):
    return window.evaluate_js(expr)


def settle(sec=2.0):
    time.sleep(sec)


def loaded():
    try:
        settle(3)
        # 1) 黑红主题
        out["theme"] = json.loads(js(
            "JSON.stringify({t: document.documentElement.dataset.theme,"
            " bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),"
            " accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()})"))

        # 2) 设置页：真实档案 + 同步结果
        js("location.hash = '#/settings'")
        settle(2.5)
        out["settings"] = json.loads(js(
            "JSON.stringify({hasNick: (document.querySelector('#page-settings').textContent||'')"
            "  .includes('沉稳果断坚韧'),"
            " hasStats: (document.querySelector('#page-settings').textContent||'')"
            "  .includes('活动 +'),"
            " syncRows: document.querySelectorAll('#page-settings tbody tr').length})"))

        # 3) 日历：新计划渲染 + 弹窗开关无暗屏残留 + 已完成活动标注
        js("location.hash = '#/calendar'")
        settle(3)
        out["calendar"] = json.loads(js(
            "JSON.stringify({wk: document.querySelectorAll('#page-calendar .wk').length,"
            " actTags: document.querySelectorAll('#page-calendar .act-tag').length,"
            " txt: (document.querySelector('#page-calendar').textContent||'')"
            "  .replace(/\\s+/g,' ').slice(0,100)})"))
        out["plan_progress"] = json.loads(js(
            "JSON.stringify({card: (document.querySelector('#page-calendar').textContent||'')"
            "  .includes('计划进度 · 时期'),"
            " phases: [...document.querySelectorAll('#page-calendar .phase-seg')]"
            "  .filter(x => x.getBoundingClientRect().width > 0).length,"
            " cur: document.querySelectorAll('#page-calendar .phase-seg.cur').length,"
            " stats: [...document.querySelectorAll('#page-calendar .stat b')]"
            "  .map(x => x.textContent.trim()).slice(0,4)})"))
        # 点第一个「可见的」课表按钮（幽灵克隆零尺寸、休息占位是 div）
        js("(() => { const b = [...document.querySelectorAll('#page-calendar button.wk')]"
           "  .find(x => x.getBoundingClientRect().width > 0); b && b.click(); })()")
        settle(1.5)
        out["modal_open"] = json.loads(js(
            "JSON.stringify({masks: document.querySelectorAll('.modal-mask').length,"
            " hasClose: (document.querySelector('.modal')||{textContent:''}).textContent.includes('关闭'),"
            " segs: [...document.querySelectorAll('.modal .seg-row')].map(x=>x.textContent).slice(0,8)})"))
        js("const b=[...document.querySelectorAll('.modal button')].find(x=>x.textContent.includes('关闭'));"
           "b && b.click()")
        settle(1.5)
        out["modal_closed"] = json.loads(js(
            "JSON.stringify({masks: document.querySelectorAll('.modal-mask').length})"))
        # 质量课弹窗：各段落目标配速 + 间歇休息方式标注
        js("(() => { const b = [...document.querySelectorAll('#page-calendar button.wk')]"
           "  .find(x => x.getBoundingClientRect().width > 0"
           "   && /间歇|阈值|重复|跨步|测试/.test(x.textContent)); b && b.click(); })()")
        settle(1.5)
        out["modal_quality"] = json.loads(js(
            "JSON.stringify({title: (document.querySelector('.modal h3')||{textContent:''}).textContent,"
            " segs: [...document.querySelectorAll('.modal .seg-row')].map(x=>x.textContent).slice(0,8)})"))
        js("const b=[...document.querySelectorAll('.modal button')].find(x=>x.textContent.includes('关闭'));"
           "b && b.click()")
        settle(1.0)

        # 4) 活动页：真实活动渲染 + 详情弹窗开关
        js("location.hash = '#/activities'")
        settle(3)
        out["activities"] = json.loads(js(
            "JSON.stringify({rows: document.querySelectorAll('#page-activities tbody tr').length,"
            " noFourSixty: !/\\d:60/.test(document.querySelector('#page-activities').textContent||'')})"))
        js("document.querySelector('#page-activities tbody tr') && "
           "document.querySelector('#page-activities tbody tr').click()")
        settle(2)
        out["act_open"] = json.loads(js(
            "JSON.stringify({masks: document.querySelectorAll('.modal-mask').length})"))
        js("const x=[...document.querySelectorAll('.modal button')].find(b=>b.textContent==='✕');"
           "x && x.click()")
        settle(1.5)
        out["act_closed"] = json.loads(js(
            "JSON.stringify({masks: document.querySelectorAll('.modal-mask').length})"))

        # 5) 健康页：120 天范围选项 + 同配速心率（选定档 × 近 16 周）卡片
        js("location.hash = '#/health'")
        settle(3)
        out["health"] = json.loads(js(r"""JSON.stringify((() => {
  const el = document.getElementById('health-pacehr-chart');
  const card = el ? el.closest('.card') : null;
  const sel = card ? card.querySelector('select') : null;
  const inst = el ? echarts.getInstanceByDom(el) : null;
  const o = inst ? inst.getOption() : null;
  const sBars = (o && o.series) ? o.series[0] : null;     // 当周次数柱
  const sRoll = (o && o.series && o.series[2]) ? o.series[2] : null;  // 滚动均值主曲线
  const d = (sRoll && sRoll.data) ? sRoll.data : [];
  const txt = card ? card.textContent.replace(/\s+/g, ' ').trim() : '';
  return {
    has120: [...document.querySelectorAll('#page-health select option')]
      .some((op) => op.value === '120'),
    pacehrCard: !!el,
    noFourSixty: !/\d:60/.test(document.querySelector('#page-health').textContent || ''),
    noUndef: !/undefined|NaN/.test(txt),
    cardTxt: txt.slice(0, 160),
    opts: sel ? sel.options.length : 0,
    selLabel: (sel && sel.selectedIndex >= 0)
      ? sel.options[sel.selectedIndex].textContent.trim() : '',
    nSeries: o ? o.series.length : 0,
    nYAxis: o ? o.yAxis.length : 0,
    nPts: d.length,
    nonNull: d.filter((v) => v != null).length,
    markLines: (sRoll && sRoll.markLine) ? sRoll.markLine.data.length : 0,
    markArea: (sRoll && sRoll.markArea) ? sRoll.markArea.data.length : 0,
    areaColor: (sRoll && sRoll.markArea) ? sRoll.markArea.itemStyle.color : '',
    areaLabel: (sRoll && sRoll.markArea) ? sRoll.markArea.label.formatter : '',
    rollColor: sRoll ? sRoll.lineStyle.color : '',
    trendStyle: (card ? card.querySelector('.pacehr-summary') : null)
      ? (card.querySelector('.pacehr-summary').getAttribute('style') || '') : '',
    barType: sBars ? sBars.type : '',
    barPts: sBars ? sBars.data.length : 0,
    barSum: sBars ? sBars.data.reduce((a, b) => a + (b || 0), 0) : 0,
    nCat: o ? o.xAxis[0].data.length : 0,
    xInterval: o ? o.xAxis[0].axisLabel.interval : null,
    yScale: o ? o.yAxis[0].scale : null,
    seriesName: sRoll ? sRoll.name : '',
  };
})())"""))
        # 下拉里的全部档位（确认快档真的在列表里、默认档跟着里程走）
        out["health_opts"] = json.loads(js(r"""JSON.stringify((() => {
  const el = document.getElementById('health-pacehr-chart');
  const sel = el ? el.closest('.card').querySelector('select') : null;
  return sel ? [...sel.options].map((o) => o.textContent.trim()) : [];
})())"""))
        # 切换配速档 → 只重画这张图，数据随之换成该档的周序列，其它图不受影响
        out["health_switch"] = json.loads(js(r"""JSON.stringify((() => {
  const el = document.getElementById('health-pacehr-chart');
  const card = el ? el.closest('.card') : null;
  const sel = card ? card.querySelector('select') : null;
  if (!sel || sel.options.length < 2) {
    return { skipped: sel ? sel.options.length : 0 };
  }
  sel.selectedIndex = 1;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return { switched: true, to: sel.options[1].textContent.trim() };
})())"""))
        settle(1.5)
        out["health_switch_after"] = json.loads(js(r"""JSON.stringify((() => {
  const el = document.getElementById('health-pacehr-chart');
  const card = el ? el.closest('.card') : null;
  const sel = card ? card.querySelector('select') : null;
  const o = echarts.getInstanceByDom(el).getOption();
  return {
    selLabel: sel.options[sel.selectedIndex].textContent.trim(),
    seriesName: o.series[2].name,
    nPts: o.series[2].data.length,
    nonNull: o.series[2].data.filter(function (v) { return v != null; }).length,
    barSum: o.series[0].data.reduce(function (a, b) { return a + (b || 0); }, 0),
    chartAlive: !!el.querySelector('canvas'),
    canvases: document.querySelectorAll('#page-health canvas').length,
  };
})())"""))
        out["health_grid"] = json.loads(js(
            "JSON.stringify((() => {const g = (id) => {const c = echarts.getInstanceByDom("
            "  document.getElementById(id)); return c ? c.getOption().grid[0].bottom : null;};"
            "  return {pace: g('health-pacehr-chart'), sleep: g('health-sleep-chart'),"
            "  hrv: g('health-hrv-chart')};})())"))
        # 5.1) 「时刻随运动数据更新」：广播一轮 sync-done（同步完成的真实信号），
        # 验证这张图确实重拉了 list_pace_bin_weekly 并重绘，且不丢用户选的档。
        # 判据用「API 被调用次数 + paceWeekly 对象引用被换新」——
        # `_lastLoad` 经 Alpine.$data() 读回恒为 0（探针侧不可观测），不能当信号。
        out["health_sync_before"] = json.loads(js(r"""JSON.stringify((() => {
  const data = window.Alpine.$data(document.getElementById('page-health'));
  window.__pbBeforeSel = data.paceBinSel;
  window.__pbWeeklyRef = data.paceWeekly;
  window.__pbChartData = JSON.stringify(data.paceWeekly.bins);
  let calls = 0;
  let wrapped = false;
  try {
    const api = window.pywebview.api;
    const orig = api.list_pace_bin_weekly;
    api.list_pace_bin_weekly = function (...a) {
      calls += 1;
      return orig.apply(api, a);
    };
    window.__pbCalls = () => calls;
    wrapped = true;
  } catch (e) {
    wrapped = 'fail:' + e.message;
  }
  window.dispatchEvent(new Event('sync-done'));
  return { wrapped: wrapped, beforeSel: window.__pbBeforeSel };
})())"""))
        settle(3)
        out["health_after_sync"] = json.loads(js(r"""JSON.stringify((() => {
  const data = window.Alpine.$data(document.getElementById('page-health'));
  const el = document.getElementById('health-pacehr-chart');
  const o = echarts.getInstanceByDom(el).getOption();
  return {
    calls: window.__pbCalls ? window.__pbCalls() : -1,
    newPayload: data.paceWeekly !== window.__pbWeeklyRef,
    payloadSame: JSON.stringify(data.paceWeekly.bins) === window.__pbChartData,
    selKept: data.paceBinSel === window.__pbBeforeSel,
    bins: data.paceWeekly.bins.length,
    weeks: data.paceWeekly.weeks.length,
    seriesName: o.series[2].name,
    nPts: o.series[2].data.length,
    nonNull: o.series[2].data.filter(function (v) { return v != null; }).length,
    markLines: o.series[2].markLine ? o.series[2].markLine.data.length : 0,
    barSum: o.series[0].data.reduce(function (a, b) { return a + (b || 0); }, 0),
    canvasAlive: !!el.querySelector('canvas'),
    allCanvases: document.querySelectorAll('#page-health canvas').length,
  };
})())"""))

        # 5.5) 仪表盘：恢复度/倒计时/KPI 瓷砖/三图/教练卡片全部渲染
        js("location.hash = '#/dashboard'")
        settle(3)
        out["dashboard"] = json.loads(js(
            "JSON.stringify({"
            " ready: (document.querySelector('#page-dashboard').textContent||'')"
            "  .match(/恢复良好|恢复一般|需要恢复|数据不足/)?.[0] || '',"
            " pills: document.querySelectorAll('#page-dashboard .ready-pill').length,"
            " tiles: document.querySelectorAll('#page-dashboard .stat-tile').length,"
            " todayRows: document.querySelectorAll('#page-dashboard .today-row').length,"
            " progs: document.querySelectorAll('#page-dashboard .progress').length,"
            " tables: document.querySelectorAll('#page-dashboard table').length,"
            " syncLine: (document.querySelector('#page-dashboard').textContent||'')"
            "  .includes('上次同步'),"
            " coach: (document.querySelector('#page-dashboard').textContent||'')"
            "  .includes('AI 教练'),"
            " placeholder: (document.querySelector('#page-dashboard').textContent||'')"
            "  .includes('开发中'),"
            " wk: (() => { const c = echarts.getInstanceByDom("
            "  document.getElementById('dash-weekly-chart'));"
            "  if (!c) return null; const o = c.getOption();"
            "  return {names: (o.series||[]).map(s=>s.name), gridBottom: o.grid[0].bottom,"
            "  nCat: o.xAxis[0].data.length}; })(),"
            " hrv: (() => { const c = echarts.getInstanceByDom("
            "  document.getElementById('dash-hrv-chart'));"
            "  return c ? {n: c.getOption().series.length,"
            "  w: c.getOption().series[0].lineStyle.width} : null; })(),"
            " rhr: (() => { const c = echarts.getInstanceByDom("
            "  document.getElementById('dash-rhr-chart'));"
            "  return c ? {n: c.getOption().series.length,"
            "  w: c.getOption().series[0].lineStyle.width} : null; })()})"))

        # 5.6) 一键评估：按钮渲染 → 点击 → 卡片切到「最近三周」口径（落库）
        out["assess_before"] = json.loads(js(r"""JSON.stringify((() => {
  const card = [...document.querySelectorAll('#page-dashboard .card')]
    .find(c => c.textContent.includes('成绩水平预估'));
  const btn = card && [...card.querySelectorAll('button')]
    .find(b => b.textContent.includes('评估'));
  const txt = card ? card.textContent : '';
  return {hasBtn: !!btn, label: btn ? btn.textContent.trim() : '',
          quick: txt.includes('一键评估'),
          win: (txt.match(/近 \d+ 天/) || [''])[0],
          vdot: (txt.match(/([\d.]+)\s*VDOT/) || [])[1] || ''};
})())"""))
        js("[...document.querySelectorAll('#page-dashboard button')]"
           ".find(b => b.textContent.includes('评估'))"
           " && [...document.querySelectorAll('#page-dashboard button')]"
           ".find(b => b.textContent.includes('评估')).click()")
        for _ in range(12):
            settle(1.5)
            if js("(document.querySelector('#page-dashboard').textContent||'')"
                  ".includes('重新评估')"):
                break
        out["assess_after"] = json.loads(js(r"""JSON.stringify((() => {
  const card = [...document.querySelectorAll('#page-dashboard .card')]
    .find(c => c.textContent.includes('成绩水平预估'));
  const btn = card && [...card.querySelectorAll('button')]
    .find(b => b.textContent.includes('评估'));
  const txt = card ? card.textContent : '';
  return {label: btn ? btn.textContent.trim() : '',
          disabled: btn ? !!btn.disabled : null,
          win: (txt.match(/近 \d+ 天/) || [''])[0],
          quick: txt.includes('一键评估'),
          ago: (txt.match(/一键评估（([^）]*)）/) || [])[1] || '',
          vdot: (txt.match(/([\d.]+)\s*VDOT/) || [])[1] || '',
          hasInterval: txt.includes('间歇能力'),
          hasVo2max: txt.includes('手表 VO2max'),
          toast: (document.getElementById('toast') || {textContent: ''}).textContent};
})())"""))

        # 5.7) 丹尼尔斯 VDOT 训练表：区间明细 + 完整矩阵 + 点行切档 + 同步后保住手选档
        js("location.hash = '#/vdot'")
        settle(3)
        out["vdot_page"] = json.loads(js(r"""JSON.stringify((() => {
  const sec = document.getElementById('page-vdot');
  const txt = sec ? sec.textContent.replace(/\s+/g, ' ').trim() : '';
  const zrows = [...sec.querySelectorAll('.vdt-zr')]
    .filter(r => [...r.querySelectorAll('.rail')].some(s => / zn-/.test(' ' + s.className)));
  const mx = [...sec.querySelectorAll('.vdt-mx tbody tr')];
  const cur = mx.filter(r => r.classList.contains('cur'));
  const hero = sec.querySelector('.hero .big');
  const badges = [...sec.querySelectorAll('.badge.soft')].map(b => b.textContent.trim());
  return {
    navLink: !!document.querySelector('nav a[href="#/vdot"]'),
    hero: hero ? hero.textContent.trim() : '',
    srcBadge: badges.find(t => t.startsWith('档位：')) || '',
    zoneRows: zrows.length,
    rail0: zrows.length ? zrows[0].querySelector('.rail').className : '',
    chips: sec.querySelectorAll('.vdt-chip').length,
    anchors: sec.querySelectorAll('.vdt-anchor').length,
    hrLast: zrows.length ? zrows[zrows.length - 1].querySelector('.vdt-hr').textContent.trim() : '',
    paceFirst: zrows.length ? zrows[0].querySelector('.num').textContent.trim() : '',
    matrixRows: mx.length,
    mxHead: [...sec.querySelectorAll('.vdt-mx thead th')].map(t => t.textContent.trim()),
    raceCells: mx.length ? mx[0].querySelectorAll('td.num.muted').length : 0,
    curVdot: cur.length ? cur[0].querySelector('b').textContent.trim() : '',
    eq5k: badges.find(t => t.startsWith('5K ')) || '',
    eqHm: badges.find(t => t.startsWith('半马 ')) || '',
    noUndef: !/undefined|NaN/.test(txt),
    txt: txt.slice(0, 150),
  };
})())"""))
        # 点矩阵里 VDOT 60 那一行 → 上方区间明细与高亮跟着切
        out["vdot_click"] = json.loads(js(r"""JSON.stringify((() => {
  const rows = [...document.querySelectorAll('#page-vdot .vdt-mx tbody tr')];
  const t = rows.find(r => r.querySelector('b').textContent.trim() === '60');
  if (!t) return { found: false, n: rows.length };
  t.click();
  return { found: true };
})())"""))
        settle(2)
        out["vdot_after_click"] = json.loads(js(r"""JSON.stringify((() => {
  const sec = document.getElementById('page-vdot');
  const cur = [...sec.querySelectorAll('.vdt-mx tbody tr')].filter(r => r.classList.contains('cur'));
  const zrows = [...sec.querySelectorAll('.vdt-zr')]
    .filter(r => [...r.querySelectorAll('.rail')].some(s => / zn-/.test(' ' + s.className)));
  return {
    hero: (sec.querySelector('.hero .big') || { textContent: '' }).textContent.trim(),
    srcBadge: ([...sec.querySelectorAll('.badge.soft')].map(b => b.textContent.trim())
      .find(t => t.startsWith('档位：')) || ''),
    curVdot: cur.length ? cur[0].querySelector('b').textContent.trim() : '',
    ezPace: zrows.length ? zrows[1].querySelector('.pace').textContent.trim() : '',
    inputVal: (sec.querySelector('input[type=number]') || { value: '' }).value,
  };
})())"""))
        # 同步广播：用户手选的档必须保住（不被拉回「当前预估」）
        js("window.dispatchEvent(new Event('sync-done'))")
        settle(3)
        out["vdot_after_sync"] = json.loads(js(r"""JSON.stringify((() => {
  const sec = document.getElementById('page-vdot');
  const cur = [...sec.querySelectorAll('.vdt-mx tbody tr')].filter(r => r.classList.contains('cur'));
  return {
    hero: (sec.querySelector('.hero .big') || { textContent: '' }).textContent.trim(),
    curVdot: cur.length ? cur[0].querySelector('b').textContent.trim() : '',
  };
})())"""))

        # 6) 目标向导第 2 步：能力预估卡片（综合依据列表）
        js("location.hash = '#/goal'")
        settle(3)
        js("document.querySelector('#page-goal button.dist-card') && "
           "document.querySelector('#page-goal button.dist-card').click()")
        settle(0.8)
        js("[...document.querySelectorAll('#page-goal button')].find(b=>b.textContent.includes('下一步'))"
           "&&[...document.querySelectorAll('#page-goal button')].find(b=>b.textContent.includes('下一步')).click()")
        settle(2)
        out["goal_ability"] = json.loads(js(
            "JSON.stringify({card: !!document.querySelector('#page-goal .card h3') &&"
            "  (document.querySelector('#page-goal').textContent||'').includes('当前水平预估'),"
            " hasVdot: (document.querySelector('#page-goal').textContent||'')"
            "  .match(/综合 VDOT [\\d.]+/)?.[0] || '',"
            " phaseUI: (document.querySelector('#page-goal').textContent||'').includes('训练时期'),"
            " sugg: (document.querySelector('#page-goal').textContent||'').includes('智能判断')})"))
        # 预览：auto 智能判断时期 → 阶段条截断 + 起点时期标注（只预览，不落库）
        js("[...document.querySelectorAll('#page-goal button')].find(b=>b.textContent.includes('预览课表'))"
           "&&[...document.querySelectorAll('#page-goal button')].find(b=>b.textContent.includes('预览课表')).click()")
        for _ in range(15):
            settle(1.5)
            done = js("(document.querySelector('#page-goal').textContent||'').includes('阶段与周跑量')")
            if done:
                break
        out["goal_preview"] = json.loads(js(
            "JSON.stringify({phaseSegs: [...document.querySelectorAll('#page-goal .phase-seg')]"
            "  .filter(x => x.getBoundingClientRect().width > 0).map(x => x.textContent.trim()),"
            " startPhase: (document.querySelector('#page-goal').textContent||'')"
            "  .match(/起点时期：[^（]+/)?.[0] || '',"
            " hasWarn: (document.querySelector('#page-goal').textContent||'').includes('智能判断')})"))

        # 7) 剪贴板：后端 read_clipboard 可调用且返回 {"ok", "data": {"text"}} 信封
        try:
            clip = Api().read_clipboard()
            text = (clip.get("data") or {}).get("text") or ""
            out["clipboard"] = {"ok": clip.get("ok") is True and isinstance(text, str),
                                "text_len": len(text)}
        except Exception as e:  # noqa: BLE001
            out["clipboard"] = {"ok": False, "error": repr(e)}

        # 8) 教练聊天：面板渲染 + 滚动停留在最后对话 + 清空对话按钮（只读验证，
        # 不真实发送消息——回复链路已由 tools/probe_chat_echo.py 只读探针覆盖）
        js("location.hash = '#/coach'")
        settle(3)
        out["coach_chat"] = json.loads(js(
            "JSON.stringify({log: !!document.querySelector('#coach-chat-log'),"
            " input: !!document.querySelector('#page-coach textarea.chat-input'),"
            " sendBtn: [...document.querySelectorAll('#page-coach button')].some(b=>b.textContent.includes('发送')),"
            " clearBtn: !!([...document.querySelectorAll('#page-coach button')]"
            "  .find(b => b.textContent.includes('清空对话'))),"
            " bubbles: document.querySelectorAll('#coach-chat-log .chat-row').length,"
            " stick: (() => { const el = document.getElementById('coach-chat-log');"
            "  return el && el.scrollHeight > el.clientHeight"
            "  ? el.scrollHeight - el.scrollTop - el.clientHeight < 20 : null; })()})"))
        # 清空按钮两步确认：第一次点击只改变文案，不触发清空
        js("[...document.querySelectorAll('#page-coach button')]"
           ".find(b=>b.textContent.includes('清空对话')).click()")
        settle(0.6)
        out["clear_confirm"] = json.loads(js(
            "JSON.stringify({twoStep: [...document.querySelectorAll('#page-coach button')]"
            "  .some(b => b.textContent.includes('再点一次确认清空'))})"))

        # 8b) 运动员画像卡 + 今日决策（本地计算，无 AI 成本）+ 日历决策徽章
        out["coach_profile"] = json.loads(js(
            "JSON.stringify((() => { const c = [...document.querySelectorAll('#page-coach .card')]"
            "  .find(x => x.textContent.includes('运动员画像')); if (!c) return {card: false};"
            " return {card: true,"
            "  verdict: (c.querySelector('.badge')||{textContent:''}).textContent.trim().slice(0,20),"
            "  lines: c.querySelectorAll('details li').length,"
            "  hasNote: c.textContent.includes('生活约束')}; })())"))
        js("location.hash = '#/calendar'")
        settle(3)
        out["calendar_decision"] = json.loads(js(
            "JSON.stringify({badge: !!document.querySelector('#page-calendar .dec-badge'),"
            " txt: (document.querySelector('#page-calendar .dec-badge')||{textContent:''})"
            "  .textContent.trim().slice(0,20),"
            " inToday: !!document.querySelector('#page-calendar .day.today .dec-badge')})"))

        # 9) 同步横幅（启动自动同步结果）
        out["banner"] = json.loads(js(
            "JSON.stringify({txt: (document.querySelector('#sync-banner')||{textContent:''})"
            "  .textContent.trim().slice(0,80)})"))

        # 10) 主观数据接入：配速同步横幅 + 主观卡片（活动/仪表盘/健康/日历四处）
        js("location.hash = '#/dashboard'")
        settle(3)
        out["dash_subjective"] = json.loads(js(r"""JSON.stringify((() => {
  const cards = [...document.querySelectorAll('#page-dashboard .card')];
  const sub = cards.find(c => c.querySelector('h2')
            && c.querySelector('h2').textContent.includes('主观状态'));
  const ps = document.querySelector('#page-dashboard .psync');
  return {
    subjCard: !!sub,
    subjShown: !!sub && getComputedStyle(sub).display !== 'none',
    subjText: sub ? sub.textContent.replace(/\s+/g, ' ').trim().slice(0, 90) : '',
    psync: !!ps,
    psyncText: ps ? ps.textContent.replace(/\s+/g, ' ').trim().slice(0, 70) : '',
    psyncBtn: ps && ps.querySelector('button')
              ? ps.querySelector('button').textContent.trim() : '',
  };})())"""))

        js("location.hash = '#/health'")
        settle(3.5)
        out["health_subjective"] = json.loads(js(r"""JSON.stringify((() => {
  const card = [...document.querySelectorAll('#page-health .card')]
    .find(c => c.textContent.includes('主观用力程度（周均 RPE）'));
  const chart = document.getElementById('health-subj-chart');
  const note = card ? card.querySelector('p.muted') : null;
  return {
    card: !!card,
    chart: !!chart,
    canvas: chart ? chart.querySelectorAll('canvas').length : 0,
    emptyNote: note ? note.textContent.replace(/\s+/g, ' ').trim().slice(0, 80) : '',
  };})())"""))

        js("location.hash = '#/activities'")
        settle(3)
        out["act_subjective"] = json.loads(js(r"""JSON.stringify((() => {
  const page = document.querySelector('#page-activities');
  const heads = [...page.querySelectorAll('thead th')].map(t => t.textContent.trim());
  const btns = [...page.querySelectorAll('button')].map(b => b.textContent.trim());
  return {subjCol: heads.includes('感受/用力'), heads: heads.length,
          hasManualBtn: btns.some(t => t.includes('手动录入')),
          hasShotBtn: btns.some(t => t.includes('截图识别')),
          rows: page.querySelectorAll('tbody tr').length};
})())"""))

        # 10b) 手动录入表单：开→字段齐→关→无遮罩残留（WebView2 暗屏回归）
        js("(() => { const b = [...document.querySelectorAll('#page-activities button')]"
           ".find(x => x.textContent.includes('手动录入')); b && b.click(); })()")
        settle(1.5)
        out["act_form_open"] = json.loads(js(r"""JSON.stringify((() => {
  const m = document.querySelector('.modal.wide');
  if (!m) return {open: false};
  const fields = [...m.querySelectorAll('.form-grid input, .form-grid select')];
  const labels = [...m.querySelectorAll('.form-grid label')].map(l => l.textContent.trim());
  const sel = [...m.querySelectorAll('select')];
  return {
    open: true,
    fields: fields.length,
    labels: labels,
    rpeOptions: sel.length > 1 ? sel[1].options.length : 0,
    hasOcrBtn: [...m.querySelectorAll('button')].some(b => b.textContent.includes('截图识别')),
    hasNote: !!m.querySelector('textarea'),
  };})())"""))
        js("(() => { const m = document.querySelector('.modal.wide');"
           " const b = m && [...m.querySelectorAll('button')]"
           ".find(x => x.textContent.trim() === '取消'); b && b.click(); })()")
        settle(1.5)
        out["act_form_closed"] = json.loads(js(
            "JSON.stringify({masks: document.querySelectorAll('.modal-mask').length,"
            " wide: document.querySelectorAll('.modal.wide').length})"))

        js("location.hash = '#/calendar'")
        settle(3)
        out["cal_psync"] = json.loads(js(
            "JSON.stringify((() => { const ps = document.querySelector('#page-calendar .psync');"
            " return {psync: !!ps,"
            "  txt: ps ? ps.textContent.replace(/\\s+/g,' ').trim().slice(0,70) : ''}; })())"))
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== UI 验证结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:600]}")
