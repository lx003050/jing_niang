"""自动通过好友申请与加群申请

- 好友申请（request_type=friend）：直接 approve
- 加群申请（request_type=group）：add（成员申请入群）/ invite（拉机器人进群）均自动 approve
注意：加群申请仅当机器人为群主/管理员时才能通过；NapCat 需上报对应请求事件。
"""
from nonebot import on_request
from nonebot.adapters.onebot.v11 import Bot, RequestEvent
from nonebot.log import logger

request_matcher = on_request(priority=1, block=True)


@request_matcher.handle()
async def auto_approve(bot: Bot, event: RequestEvent):
    try:
        if event.request_type == "friend":
            await bot.call_api("set_friend_add_request", flag=event.flag, approve=True)
            logger.info(f"自动通过好友申请: {event.user_id}")
        elif event.request_type == "group":
            sub = getattr(event, "sub_type", "add")
            await bot.call_api("set_group_add_request", flag=event.flag, sub_type=sub, approve=True)
            logger.info(f"自动处理加群申请: group={getattr(event, 'group_id', '?')} user={event.user_id} sub={sub}")
    except Exception as e:
        logger.warning(f"自动通过申请失败: user={getattr(event, 'user_id', '?')} err={e}")
