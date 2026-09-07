"""简单问答：正则/包含匹配，不接入 AI 也可用

数据文件: data/qa.json
  格式: [{"question": "关键词或正则", "answer": "回答", "regex": true/false}]
"""
import re

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .common import DATA_DIR, hot_load_json

QA_FILE = DATA_DIR / "qa.json"
_cache: dict = {}


def _load_compiled() -> list[tuple[object, str, bool]]:
    items = hot_load_json(QA_FILE, _cache)
    if not isinstance(items, list):
        return []
    compiled = []
    for item in items:
        q, a = item.get("question", ""), item.get("answer", "")
        is_regex = bool(item.get("regex"))
        if not q or not a:
            continue
        if is_regex:
            try:
                compiled.append((re.compile(q), a, True))
            except re.error:
                continue
        else:
            compiled.append((q, a, False))
    return compiled


def _qa_rule(event: MessageEvent) -> bool:
    # 旧版全局问答已停用：与 admin_tools 的 /问答（群隔离、图文答案）重复，且旧库无群过滤会串群。
    return False


qa_matcher = on_message(rule=_qa_rule, priority=3, block=True)


@qa_matcher.handle()
async def qa_handler(bot: Bot, event: MessageEvent):
    text = event.get_plaintext()
    for pat, answer, is_regex in _load_compiled():
        if (is_regex and pat.search(text)) or (not is_regex and pat in text):
            await bot.send(event, answer)
            return
