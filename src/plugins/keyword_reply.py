"""关键词回复：不接入 AI 时的简单对话（分院帽风格）

数据文件: data/keywords.json  (支持运行时热更新，无需重启)
"""
import random

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .common import DATA_DIR, ai_config, hot_load_json

KEYWORDS_FILE = DATA_DIR / "keywords.json"
_cache: dict = {}


def _keyword_rule(event: MessageEvent) -> bool:
    # AI 模式下由 AI 接管对话
    if ai_config.ai_enabled:
        return False
    keywords = hot_load_json(KEYWORDS_FILE, _cache)
    text = event.get_plaintext()
    return any(kw in text for kw in keywords)


keyword_matcher = on_message(rule=_keyword_rule, priority=2, block=True)


@keyword_matcher.handle()
async def keyword_handler(bot: Bot, event: MessageEvent):
    keywords = hot_load_json(KEYWORDS_FILE, _cache)
    text = event.get_plaintext()
    for kw, replies in keywords.items():
        if kw in text:
            await bot.send(event, random.choice(replies))
            return
