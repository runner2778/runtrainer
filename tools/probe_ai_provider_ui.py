"""探针：设置页 AI 服务商卡片——加载状态、动态模型/Key 区、切换 provider 的模型归一。

只读检查 + 切换演练后把 ai_provider/ai_model 还原到原值（写的是真实 %APPDATA% KV）。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import webview  # noqa: E402

from runtrainer.api.bridge import Api  # noqa: E402
from runtrainer.app import _start_web_server  # noqa: E402

api = Api()
sett = api.get_settings()["data"]
out = {"orig": {"provider": sett["ai_provider"], "model": sett["ai_model"],
                "providers": [p["key"] for p in sett["ai_providers"]],
                "keys": sett["ai_keys"], "has_ds": sett["has_deepseek_key"]}}

url = _start_web_server()
window = webview.create_window("探针", url, js_api=Api(), width=1200, height=800)


def js(expr):
    return window.evaluate_js(expr)


def loaded():
    try:
        time.sleep(4)
        # 1) 加载后组件状态
        out["state"] = js("(() => { const el = document.getElementById('page-settings');"
                          " const d = Alpine.$data(el);"
                          " return JSON.stringify({provider: d.aiProvider,"
                          " providers: (d.aiProviders||[]).map(p=>p.key),"
                          " keys: d.aiKeys, model: d.aiModel,"
                          " hint: (d.aiHint()||'').slice(0,30),"
                          " needsKey: d.provNeedsKey(), freeText: d.provFreeText(),"
                          " opts: d.modelOptions(), hasKey: d.hasAiKey()}); })()")
        out["dom"] = js("JSON.stringify({opts: [...document.querySelectorAll('#page-settings select')]"
                        ".map(s => [...s.options].map(o=>o.textContent)),"
                        " hintP: (document.querySelector('#page-settings .card h2 + div ~ p')||{}).textContent || ''})")
        # 2) 演练（异步写 window.__probe，稍后轮询读取）：智谱 → 模型归一 glm-4-flash
        js("window.__probe = {step: 0}; (async () => { const el = document.getElementById('page-settings');"
           " const d = Alpine.$data(el); d.aiProvider = 'zhipu'; await d.saveAiProvider();"
           " window.__probe = {step: 1, provider: d.aiProvider, model: d.aiModel,"
           " opts: d.modelOptions(), needsKey: d.provNeedsKey(), freeText: d.provFreeText(),"
           " ph: d.keyPlaceholder(), hasKey: d.hasAiKey(),"
           " sel: [...document.querySelectorAll('#page-settings select')].map(s => [...s.options].map(o=>o.textContent))}; })()")
        time.sleep(1.2)
        out["zhipu"] = js("JSON.stringify(window.__probe)")
        # 3) 演练：Ollama → free_text 输入框 + 无 Key 区
        js("window.__probe = {step: 0}; (async () => { const el = document.getElementById('page-settings');"
           " const d = Alpine.$data(el); d.aiProvider = 'ollama'; await d.saveAiProvider();"
           " window.__probe = {step: 1, provider: d.aiProvider, model: d.aiModel,"
           " freeText: d.provFreeText(), needsKey: d.provNeedsKey(),"
           " inputs: [...document.querySelectorAll('#page-settings input')].map(i => i.type + '|' + (i.list || '')),"
           " hint: (d.aiHint()||'').slice(0,30)}; })()")
        time.sleep(1.2)
        out["ollama"] = js("JSON.stringify(window.__probe)")
        # 4) 切回 deepseek 还原（模型应归一回原模型）
        js("window.__probe = {step: 0}; (async () => { const el = document.getElementById('page-settings');"
           " const d = Alpine.$data(el); d.aiProvider = 'deepseek'; await d.saveAiProvider();"
           " window.__probe = {step: 1, provider: d.aiProvider, model: d.aiModel}; })()")
        time.sleep(1.2)
        out["back"] = js("JSON.stringify(window.__probe)")
    except Exception as e:  # noqa: BLE001
        out["fatal"] = repr(e)
    finally:
        # 还原 KV（演练过程把 ai_provider/ai_model 写回了真实存储）
        api.set_setting("ai_provider", sett["ai_provider"])
        api.set_setting("ai_model", sett["ai_model"])
        window.destroy()


window.events.loaded += loaded
webview.start()
for k, v in out.items():
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:900]}")
