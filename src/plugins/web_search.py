"""联网搜索（skill）：让鲸娘对话时遇到不懂/拿不准/需要实时信息的问题时上网搜一搜再作答。

- 后端：博查 Bocha Web Search API（.env 配置 BOCHA_API_KEY 后启用）
- 触发：由 sorting_hat 的 function calling 自主调用 web_search 工具，
  搜到结果回填给模型再追问一次，由模型读完资料后作答。
- 开关：全局开启（不受群白名单约束）；未配置 Key 时该工具不挂载。
"""
import logging

import httpx

from .common import ai_config

logger = logging.getLogger("sorting_hat.web_search")

# 供 sorting_hat 挂载到 _AI_TOOLS 的工具定义
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索。当你遇到不知道、拿不准、或需要时效性/事实核查的信息时调用，"
            "例如新闻、天气、比分、价格、最新动态、近期事件、陌生名词/人物/作品等。"
            "纯闲聊、你已确定的知识、纯创作类请求不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，尽量精炼，用最可能出现在网页标题里的说法",
                }
            },
            "required": ["query"],
        },
    },
}


def web_search_available() -> bool:
    """是否已配置博查 Key（未配置则不挂载联网搜索工具）。"""
    return bool(ai_config.bocha_api_key)


async def run_web_search(query: str, count: int = 0) -> str:
    """调用博查搜索，返回给模型阅读的纯文本结果。

    任何异常都吞掉并返回带说明的文本，保证不会打断 AI 回复流程。
    """
    query = (query or "").strip()
    if not query:
        return "（搜索关键词为空，未执行搜索）"
    if not ai_config.bocha_api_key:
        return "（联网搜索未配置 API Key，无法联网）"

    n = count if count else ai_config.bocha_search_count
    n = max(1, min(int(n), 10))
    headers = {
        "Authorization": f"Bearer {ai_config.bocha_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "freshness": "noLimit", "summary": True, "count": n}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(ai_config.bocha_base_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("联网搜索请求失败: %s", e)
        return f"（联网搜索请求失败：{e}）"
    return _format_results(query, data, n)


def _format_results(query: str, data: dict, n: int) -> str:
    """把博查返回的 JSON 整理成模型易读的编号列表。"""
    code = (data or {}).get("code")
    if code not in (None, 200):
        return f"（联网搜索失败：{data.get('msg') or code}）"
    pages = (((data or {}).get("data") or {}).get("webPages") or {}).get("value") or []
    if not pages:
        return f"（联网搜索「{query}」没有找到相关结果）"
    lines = [f"以下是联网搜索「{query}」的结果，请据此作答："]
    for i, p in enumerate(pages[:n], 1):
        title = str(p.get("name") or "").strip()
        url = str(p.get("url") or "").strip()
        snip = str(p.get("summary") or p.get("snippet") or "").strip()
        lines.append(f"{i}. {title}\n   来源：{url}\n   摘要：{snip}")
    return "\n".join(lines)
