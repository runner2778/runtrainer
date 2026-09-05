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

        # 5) 健康页：120 天范围选项 + 配速-心率周均图卡片
        js("location.hash = '#/health'")
        settle(3)
        out["health"] = json.loads(js(
            "JSON.stringify({has120: [...document.querySelectorAll('#page-health select option')]"
            "  .some(o => o.value === '120'),"
            " pacehrCard: !!document.querySelector('#health-pacehr-chart'),"
            " pacehrText: (document.querySelector('#health-pacehr-chart')"
            "  ? document.querySelector('#health-pacehr-chart').closest('.card').textContent : '')"
            "  .replace(/\\s+/g,' ').slice(0,60),"
            " summary: (document.querySelector('.pacehr-summary')||{textContent:''}).textContent"
            "  .replace(/\\s+/g,' ').trim().slice(0,80),"
            " noFourSixty: !/\\d:60/.test(document.querySelector('#page-health').textContent||''),"
            " paceOpt: (echarts.getInstanceByDom(document.getElementById('health-pacehr-chart'))||{})"
            "  .getOption ? (() => {const o = echarts.getInstanceByDom("
            "  document.getElementById('health-pacehr-chart')).getOption();"
            "  const s = o.series || [];"
            "  return {n: s.length, firstW: (s[0]||{}).lineStyle ? s[0].lineStyle.width : 0,"
            "  firstSym: !!(s[0]||{}).showSymbol,"
            "  lastDashed: !!(s[s.length-1]||{}).lineStyle && s[s.length-1].lineStyle.type === 'dashed',"
            "  axisInterval: o.xAxis[0].axisLabel ? o.xAxis[0].axisLabel.interval : null};})() : {},"
            " gridBottom: (() => {const g = (id) => {const c = echarts.getInstanceByDom("
            "  document.getElementById(id)); return c ? c.getOption().grid[0].bottom : null;};"
            "  return {pace: g('health-pacehr-chart'), sleep: g('health-sleep-chart'),"
            "  hrv: g('health-hrv-chart')};})()})"))

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

        # 8) 教练聊天：面板渲染 + 发送一条消息看回复
        js("location.hash = '#/coach'")
        settle(3)
        out["coach_chat"] = json.loads(js(
            "JSON.stringify({log: !!document.querySelector('#coach-chat-log'),"
            " input: !!document.querySelector('#page-coach textarea.chat-input'),"
            " sendBtn: [...document.querySelectorAll('#page-coach button')].some(b=>b.textContent.includes('发送'))})"))
        js("(() => { const t = document.querySelector('#page-coach textarea.chat-input');"
           " t.value = '你好教练，今天状态不错'; t.dispatchEvent(new Event('input'));"
           " [...document.querySelectorAll('#page-coach button')].find(b=>b.textContent.includes('发送')).click(); })()")
        probe = ("JSON.stringify({bubbles: document.querySelectorAll('#coach-chat-log .chat-row').length,"
                 " coachTxt: (document.querySelector('#coach-chat-log .chat-coach .chat-text')||{textContent:''})"
                 "  .textContent.slice(0,60),"
                 " toast: (document.querySelector('#toast')||{textContent:''}).textContent.trim().slice(0,80)})")
        out["chat_reply"] = json.loads(js(probe))
        # 真实 DeepSeek 响应可达数十秒：轮询等待教练回复气泡或失败 toast
        for _ in range(50):
            settle(2)
            res = json.loads(js(probe))
            out["chat_reply"] = res
            if res["toast"] or (res["coachTxt"] and "思考中" not in res["coachTxt"]):
                break

        # 9) 同步横幅（启动自动同步结果）
        out["banner"] = json.loads(js(
            "JSON.stringify({txt: (document.querySelector('#sync-banner')||{textContent:''})"
            "  .textContent.trim().slice(0,80)})"))
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        window.destroy()


window.events.loaded += loaded
webview.start()
print("=== UI 验证结果 ===")
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:300]}")
