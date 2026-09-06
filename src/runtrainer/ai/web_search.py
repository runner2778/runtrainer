"""智谱 Web Search API（OpenAI 兼容 chat 里的 tools web_search 在 flash 系
实测不生效——静默接受但无检索、返回空正文；改用独立 Web Search API 端点，
两段式：先检索再让教练模型基于检索结果作答。search_std 约 0.01 元/次。"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

WEB_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
DEFAULT_ENGINE = "search_std"  # 最便宜档（search_pro 0.03 元、搜狗/夸克 0.05 元）

MAX_QUERY_LEN = 70   # 官方上限
MAX_RESULTS = 5
TIMEOUT_S = 25


def zhipu_search(query: str, api_key: str, engine: str = DEFAULT_ENGINE,
                 count: int = MAX_RESULTS) -> list[dict]:
    """执行一次联网检索，返回 [{title, link, content, publish_date?}]。

    失败抛 RuntimeError（由调用方降级为纯知识回答，不阻断对话）。
    """
    body = json.dumps({
        "search_engine": engine,
        "search_query": query[:MAX_QUERY_LEN],
        "search_intent": False,
        "count": count,
        "content_size": "high",
    }).encode()
    req = urllib.request.Request(WEB_SEARCH_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            obj = json.loads(r.read().decode())
    except Exception as e:
        raise RuntimeError(f"联网检索请求失败：{e}") from e
    results = obj.get("search_result") or []
    out = []
    for it in results:
        if not (it.get("title") or it.get("content")):
            continue
        out.append({
            "title": (it.get("title") or "").strip(),
            "link": (it.get("link") or "").strip(),
            "content": (it.get("content") or "").strip(),
            "publish_date": it.get("publish_date"),
        })
    if not out:
        log.warning("联网检索无结果：%s（code=%s msg=%s）",
                    query[:60], obj.get("code"), obj.get("msg"))
    return out
