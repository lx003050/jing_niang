"""群禁言：/禁言 期间机器人在该群对所有人静默，不回应任何消息/通知。

- /禁言 [时间] [-r|-op]：仅管理员。时间支持 -24h / -90m / -5s / -2d，缺省 1 小时。
  - 默认：禁言期内所有人（含管理员）都被静默，唯一例外是【根管理员】发 /解除禁言 可提前解除；
  - -r：禁言期内根管理员仍可正常使用 bot；
  - -op：禁言期内根管理员与管理员仍可正常使用 bot。
- /解除禁言（别名 /解禁）：仅管理员，提前结束本群禁言。
"""
import logging
import time

from nonebot import on_command, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRequestEvent,
    Message,
    MessageEvent,
    NoticeEvent,
)
from nonebot.params import CommandArg

from .admin_tools import OP_SEED, is_op
from .common import hide_help, register_help

logger = logging.getLogger("sorting_hat.mute")

# 群禁言状态: gid -> {"until": 到期时间戳, "level": 0|1|2}
#   level 0 = 默认（仅根管理员可发 /解除禁言）；1 = -r（根可用）；2 = -op（根+管理员可用）
_MUTES: dict[int, dict] = {}

DEFAULT_SECONDS = 3600.0  # 无时间参数默认 1 小时
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _mute_info(gid: int | None) -> dict | None:
    if not gid:
        return None
    m = _MUTES.get(gid)
    if not m:
        return None
    if m["until"] <= time.time():
        _MUTES.pop(gid, None)  # 已到期自动清除
        return None
    return m


def _parse_seconds(text: str) -> float | None:
    """解析时间参数：-24h / 90m / 5s / 2d（可带正负号），无单位按小时。"""
    t = text.strip()
    if t.startswith(("-", "+")):
        t = t[1:]  # 前导符号只表示参数写法（-24h 即 24 小时），取绝对值
    if not t:
        return None
    i = 0
    while i < len(t) and (t[i].isdigit() or t[i] == "."):
        i += 1
    num_part, unit = t[:i], t[i:].lower()
    if not num_part:
        return None
    try:
        value = float(num_part)
    except ValueError:
        return None
    mul = _UNIT_SECONDS.get(unit, 3600.0)  # 无单位或未知单位按小时
    return max(value * mul, 1.0)


def _fmt_duration(seconds: float) -> str:
    for unit, div in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds >= div:
            n = seconds / div
            return f"{n:g} {unit}"
    return f"{seconds:g} 秒"


def _human_until(until: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(until).strftime("%H:%M")


# ---------- 静默闸门（priority=-1 最先处理，命中即 block） ----------
def _mute_msg_rule(event: GroupMessageEvent) -> bool:
    """禁言群内的消息是否要静默拦截。"""
    info = _mute_info(event.group_id)
    if not info:
        return False
    uid = event.user_id
    lvl = info.get("level", 0)
    if lvl == 1:
        return uid != OP_SEED
    if lvl == 2:
        return not is_op(uid)
    # level 0：仅放行根管理员的「解除禁言/解禁」命令
    text = event.get_plaintext().strip()
    if uid == OP_SEED and text.startswith(("/解除禁言", "！解除禁言", "!解除禁言", "／解除禁言", "/解禁", "！解禁", "!解禁")):
        return False
    return True


mute_msg_blocker = on_message(rule=_mute_msg_rule, priority=-1, block=True)


@mute_msg_blocker.handle()
async def _mute_msg_handler(bot: Bot, event: GroupMessageEvent):
    pass  # 静默拦截：不回复、不处理


def _mute_notice_rule(event: NoticeEvent) -> bool:
    """禁言群的 poke/进出群/撤回等通知一律静默（按禁言等级放行对应身份的 poke）。"""
    gid = getattr(event, "group_id", None)
    info = _mute_info(gid)
    if not info:
        return False
    uid = getattr(event, "user_id", None)
    lvl = info.get("level", 0)
    if uid is None:
        return True
    if lvl == 1:
        return uid != OP_SEED
    if lvl == 2:
        return not is_op(uid)
    return True


mute_notice_blocker = on_notice(rule=_mute_notice_rule, priority=-1, block=True)


@mute_notice_blocker.handle()
async def _mute_notice_handler(bot: Bot, event: NoticeEvent):
    pass


def _mute_request_rule(event: GroupRequestEvent) -> bool:
    return bool(_mute_info(getattr(event, "group_id", None)))


mute_request_blocker = on_request(rule=_mute_request_rule, priority=-1, block=True)


@mute_request_blocker.handle()
async def _mute_request_handler(bot: Bot, event: GroupRequestEvent):
    pass


# ---------- /禁言 ----------
mute_cmd = on_command("禁言", aliases={"mute"}, priority=1, block=True)
register_help("/禁言", "静默禁言本群（仅管理员）：/禁言 [时间]；时间如 -24h，缺省 1 小时；-r 禁言期间根管理员可用；-op 根与管理员可用")
hide_help("/禁言")


@mute_cmd.handle()
async def mute_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not is_op(event.user_id):
        await mute_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    gid = getattr(event, "group_id", None)
    if not gid:
        await mute_cmd.finish("禁言只能在群聊里使用。", at_sender=True)

    level = 0
    seconds = DEFAULT_SECONDS
    for tok in arg.extract_plain_text().split():
        low = tok.lower()
        if low in ("-r", "-root"):
            level = max(level, 1)
        elif low == "-op":
            level = max(level, 2)
        else:
            d = _parse_seconds(tok)
            if d is not None:
                seconds = d

    until = time.time() + seconds
    _MUTES[gid] = {"until": until, "level": level}

    if level == 1:
        except_desc = "；例外：-r 禁言期间根管理员仍可用"
    elif level == 2:
        except_desc = "；例外：-op 禁言期间根管理员与管理员仍可用"
    else:
        except_desc = "；期间仅根管理员的 /解除禁言 可提前结束"

    await mute_cmd.finish(
        f"🔇 本群已禁言 {_fmt_duration(seconds)}（约至 {_human_until(until)}）。"
        f"期间我不会回应任何人的消息与戳一戳{except_desc}。",
        at_sender=True,
    )


# ---------- /解除禁言 ----------
unmute_cmd = on_command("解除禁言", aliases={"解禁", "unmute"}, priority=1, block=True)
register_help("/解除禁言", "提前解除本群禁言（仅管理员）")
hide_help("/解除禁言")


@unmute_cmd.handle()
async def unmute_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await unmute_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    gid = getattr(event, "group_id", None)
    if not gid:
        await unmute_cmd.finish("解除禁言只能在群聊里使用。", at_sender=True)
    if not _MUTES.get(gid):
        await unmute_cmd.finish("本群当前并没有在禁言中哦。", at_sender=True)
    _MUTES.pop(gid, None)
    await unmute_cmd.finish("🔊 本群禁言已解除，我回来了～", at_sender=True)
