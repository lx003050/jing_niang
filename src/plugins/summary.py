"""群聊省流：/省流 输出群聊摘要

- 持续记录每个群的普通消息（滚动保留最近 72 小时、最多 5000 条）
- /省流 时：
  - 不带 -a：汇总「上次省流时间戳 → 当前」之间的消息（首次则从缓冲最早开始），条数超限时截取最近 N 条
  - 带 -a：不考虑时间戳，直接汇总最近 N 条消息（如 /省流 200 -a）
  - N 缺省为 120 条，上限 5000
  - 每次省流后，为该群记录本次覆盖的最后一条消息时间，供下次省流参考（各群独立）
  - 接入 AI：把消息交给 AI 生成结构化完整摘要（话题/关键信息/待办/参与情况，话题带开始时间）
  - 未接 AI：按说话人统计条数 + 最近消息列表
"""
import json
import logging
import math
import re
import time
from collections import Counter, deque
from pathlib import Path

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg

from .common import DATA_DIR, ai_config, register_help
from .sorting_hat import _get_client

logger = logging.getLogger("sorting_hat.summary")

DEFAULT_COUNT = 120     # 缺省汇总的消息条数
MAX_COUNT = 5000        # 消息条数上限
MAX_HOURS = 72          # 时间窗口上限（小时）
MAX_AI_LINES = 400      # 喂给 AI 的消息条数上限（超出时均匀抽样）

# 消息缓冲与省流时间戳的持久化文件（重启不丢）
MSG_BUF_FILE = DATA_DIR / "msg_buffers.jsonl"
LAST_TS_FILE = DATA_DIR / "summary_last_ts.json"
COMPACT_THRESHOLD = 5 * 1024 * 1024  # 缓冲文件超过 5MB 时按当前缓冲重写
_save_counter = 0

# AI 省流提示词：要求按结构输出，覆盖话题、关键信息、待办与参与情况，话题带开始时间
SUMMARY_PROMPT = (
    "你是「鲸娘」，兼任群聊省流助手。下面是某群的聊天记录，每条带时间和说话人。"
    "请把所有内容总结成一份清晰完整的省流摘要，严格按下面的结构输出：\n"
    "省流版：一句话点出这段时间整体在聊什么或气氛如何\n"
    "【聊了什么】按话题分点，每点以（开始时间）开头（如（14:30）聊了分院），"
    "用一句话讲清一个话题，尽量覆盖全部内容\n"
    "【关键信息】重要结论、关键数字或值得记住的事，没有就写「无」\n"
    "【待办/共识】大家答应要做的事或达成的一致，没有就写「无」\n"
    "【参与情况】最活跃的一两个人加一句氛围评价\n"
    "要求：只依据聊天记录里的真实内容，不许编造；话题的开始时间要从记录里如实读取；"
    "语言精炼，总字数控制在 400 字以内。"
)

_buffers: dict[int, deque] = {}                 # group_id -> deque[(ts, uid, text)]
_last_ts: dict[int, float] = {}                 # group_id -> 上次省流覆盖的最后一条消息时间（各群独立）
_nick_cache: dict[tuple[int, int], tuple[str, float]] = {}


# ---------- 持久化：重启后恢复消息缓冲与省流时间戳 ----------
def _load_buffers() -> None:
    """启动时从磁盘恢复消息缓冲与各群省流时间戳（各群独立）。"""
    now = time.time()
    try:
        for line in MSG_BUF_FILE.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                buf = _buffers.setdefault(int(item["g"]), deque(maxlen=MAX_COUNT))
                buf.append((float(item["t"]), int(item["u"]), item["x"]))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("恢复消息缓冲失败", exc_info=True)
    for gid, buf in list(_buffers.items()):
        while buf and now - buf[0][0] > MAX_HOURS * 3600:
            buf.popleft()
    try:
        data = json.loads(LAST_TS_FILE.read_text(encoding="utf-8"))
        for k, v in (data or {}).items():
            _last_ts[int(k)] = float(v)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("恢复省流时间戳失败", exc_info=True)


def _persist_msg(group_id: int, ts: float, uid: int, text: str) -> None:
    """把一条消息追加到缓冲文件（每条一行 JSON）。"""
    global _save_counter
    try:
        line = json.dumps(
            {"g": group_id, "t": round(ts, 3), "u": uid, "x": text},
            ensure_ascii=False,
        )
        MSG_BUF_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MSG_BUF_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _save_counter += 1
        if _save_counter >= 200:  # 每 200 条检查一次文件体积
            _save_counter = 0
            if MSG_BUF_FILE.stat().st_size > COMPACT_THRESHOLD:
                _compact_file()
    except Exception:
        logger.warning("持久化消息失败", exc_info=True)


def _compact_file() -> None:
    """缓冲文件过大时，按当前内存缓冲重写（只保留各群有效消息）。"""
    try:
        with open(MSG_BUF_FILE, "w", encoding="utf-8") as f:
            for gid, buf in _buffers.items():
                for ts, uid, text in buf:
                    f.write(
                        json.dumps(
                            {"g": gid, "t": round(ts, 3), "u": uid, "x": text},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
    except Exception:
        logger.warning("压缩消息缓冲失败", exc_info=True)


def _persist_last_ts() -> None:
    """把各群省流时间戳写入磁盘。"""
    try:
        LAST_TS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_TS_FILE.write_text(
            json.dumps({str(k): v for k, v in _last_ts.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("持久化省流时间戳失败", exc_info=True)


# ---------- 消息记录（低优先级，不拦截） ----------
def _record_rule(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


msg_recorder = on_message(rule=_record_rule, priority=1000, block=False)


@msg_recorder.handle()
async def record_handler(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    if not text:
        return
    now = time.time()
    buf = _buffers.setdefault(event.group_id, deque(maxlen=MAX_COUNT))
    buf.append((now, event.user_id, text))
    while buf and now - buf[0][0] > MAX_HOURS * 3600:  # 滚动清理超时消息
        buf.popleft()
    _persist_msg(event.group_id, now, event.user_id, text)


# ---------- /省流 ----------
summary_cmd = on_command("省流", aliases={"总结"}, priority=1, block=True)
register_help("/省流", "群聊省流：/省流 汇总上次省流以来消息；/省流 24h 最近24小时；/省流 200 -a 最近200条(忽略时间戳)")


def _parse_arg(text: str) -> tuple[int, float | None, bool]:
    """解析 /省流 参数，返回 (条数, 时间范围秒数或None, 是否带 -a)。

    - 纯数字：按条数取最近 N 条（如 /省流 200）
    - 数字 + h/m/d：按时间范围取（如 /省流 24h / 省流 30m / 省流 2d）
    - 带 -a：不考虑上次省流时间戳，只按条数
    """
    s = text.strip().lower()
    no_ts = "-a" in s
    # 兼容 -24h / 24h / -30m 等带负号写法（负号仅为历史习惯，无负数语义）
    m = re.match(r"-?(\d+)\s*(h|小时|m|分钟|d|天)", s)
    if m:
        num = int(m.group(1))
        unit = {"h": 3600, "小时": 3600, "m": 60, "分钟": 60, "d": 86400, "天": 86400}[m.group(2)]
        return 0, num * unit, no_ts
    m = re.search(r"(\d+)", s)
    if m:
        return max(1, min(int(m.group(1)), MAX_COUNT)), None, no_ts
    return DEFAULT_COUNT, None, no_ts


@summary_cmd.handle()
async def summary_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    if not group_id:
        await summary_cmd.finish("省流只支持在群聊里用哦～", at_sender=True)

    count, time_range, no_ts = _parse_arg(arg.extract_plain_text())
    buf = _buffers.get(group_id, deque())
    now = time.time()

    if time_range is not None:
        # 按时间范围：最近 N 小时/分钟/天
        msgs = [
            m for m in buf if m[0] >= now - time_range and not m[2].startswith("/")
        ][-MAX_COUNT:]
    elif no_ts:
        # 仅按条数，不考虑时间戳
        msgs = [m for m in buf if not m[2].startswith("/")][-count:]
    else:
        # 从上次省流时间戳到当前的消息（首次则从缓冲最早开始）
        last_ts = _last_ts.get(group_id, 0.0)
        msgs = [m for m in buf if m[0] > last_ts and not m[2].startswith("/")]
        msgs = msgs[-count:]

    if not msgs:
        tip = (
            "本群从上一次省流到现在还没有新消息～"
            if time_range is None and not no_ts
            else "该时间范围内还没有可汇总的消息～"
        )
        await summary_cmd.finish(tip, at_sender=True)

    _last_ts[group_id] = msgs[-1][0]  # 留下本次覆盖的最后一条消息时间，供下次参考（各群独立）
    _persist_last_ts()

    reply = await _make_summary(bot, group_id, msgs)
    await summary_cmd.finish(reply, at_sender=True)


def _fmt_time(ts: float, with_date: bool) -> str:
    if with_date:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))
    return time.strftime("%H:%M", time.localtime(ts))


async def _make_summary(bot: Bot, group_id: int, msgs: list[tuple]) -> str:
    """优先让 AI 提炼摘要，失败或未接 AI 时回退到统计模式。"""
    with_date = msgs[-1][0] - msgs[0][0] > 3600 * 20  # 跨度接近一天时带日期
    start = _fmt_time(msgs[0][0], with_date)
    end = _fmt_time(msgs[-1][0], with_date)
    uids = {uid for _, uid, _ in msgs}

    client = _get_client()
    if client is not None:
        lines = []
        prev = None
        for ts, uid, text in msgs:
            if text == prev:  # 连续复读只保留一条，节省 token
                continue
            prev = text
            name = await _get_nick(bot, group_id, uid)
            lines.append(f"[{_fmt_time(ts, with_date)}] {name}：{text}")
        if len(lines) > MAX_AI_LINES:  # 消息过多时均匀抽样，保证覆盖整个时间段
            step = math.ceil(len(lines) / MAX_AI_LINES)
            lines = lines[::step]
        transcript = "\n".join(lines)
        meta = f"时间段 {start}~{end}，共 {len(msgs)} 条消息、{len(uids)} 位成员：\n"
        try:
            resp = await client.chat.completions.create(
                model=ai_config.ai_model,
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": meta + transcript},
                ],
                temperature=0.5,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
            logger.warning("AI 省流返回空内容，回退统计模式")
        except Exception:
            logger.exception("AI 省流失败，回退统计模式")
    return await _simple_summary(bot, group_id, msgs)


async def _get_nick(bot: Bot, group_id: int, uid: int) -> str:
    key = (group_id, uid)
    hit = _nick_cache.get(key)
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    name = str(uid)
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=uid)
        name = info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        pass
    _nick_cache[key] = (name, time.time())
    return name


async def _simple_summary(bot: Bot, group_id: int, msgs: list[tuple]) -> str:
    counter = Counter(uid for _, uid, _ in msgs)
    with_date = msgs[-1][0] - msgs[0][0] > 3600 * 20
    start = _fmt_time(msgs[0][0], with_date)
    end = _fmt_time(msgs[-1][0], with_date)
    lines = [
        "省流版：",
        f"时间段 {start}~{end}，共 {len(msgs)} 条消息，{len(counter)} 位成员参与",
    ]
    for uid, cnt in counter.most_common(3):
        name = await _get_nick(bot, group_id, uid)
        lines.append(f"· {name} 说了 {cnt} 条")
    lines.append("最近几条：")
    for ts, uid, text in msgs[-5:]:
        name = await _get_nick(bot, group_id, uid)
        lines.append(f"[{_fmt_time(ts, with_date)}] {name}：{text[:30]}")
    return "\n".join(lines)


# 模块加载时恢复历史消息缓冲与省流时间戳（重启不丢）
_load_buffers()
