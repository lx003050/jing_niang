"""复读：同一群内连续 3 条相同消息时复读一次（30 秒冷却）"""
import time
from collections import defaultdict

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from .common import ai_config

def _group_rule(event: GroupMessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


repeat_matcher = on_message(rule=_group_rule, priority=10, block=False)

_last = defaultdict(str)      # group_id -> 上一条文本
_count = defaultdict(int)     # group_id -> 连续相同条数
_cooldown = defaultdict(float)


@repeat_matcher.handle()
async def repeat_handler(bot: Bot, event: GroupMessageEvent):
    # AI 模式下不参与复读，避免打扰
    if ai_config.ai_enabled:
        return

    text = event.get_plaintext().strip()
    gid = event.group_id
    if not text or len(text) < 2:
        _last[gid], _count[gid] = text, 0
        return

    if text == _last[gid]:
        _count[gid] += 1
        if _count[gid] >= 3 and time.time() - _cooldown[gid] > 30:
            _cooldown[gid] = time.time()
            _count[gid] = 0
            await bot.send(event, text)
    else:
        _last[gid], _count[gid] = text, 0
