"""娱乐小工具：小猪图、入典、栽桩、功能展示

- /小猪            随机发送一张 PigHub 的小猪图片（所有人可用）
- 入典（被动）      回复某条消息并说「入典」→ 把被回复的内容收进典里，并回复（已入典）
- 查看入典（被动）  消息里出现「查看入典」→ 从典里随机回复一条内容
- /栽桩 @某人 信息  用「合并转发」伪造某人发送了指定内容（文本或图片）
- /展示 /功能      用「合并转发」消息展示某个功能的用法示例，如 /展示 /判刑
"""
import difflib
import hashlib
import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg

from .common import DATA_DIR, QA_IMG_DIR, register_help

logger = logging.getLogger("sorting_hat.fun_tools")


# ==================== /小猪：PigHub 随机小猪图 ====================
PIGHUB_API = "https://pighub.top/api/images?sort=2"  # 一次返回全部图片列表（必须带 sort 参数）
PIGHUB_BASE = "https://pighub.top"
PIG_LIST_TTL = 600                              # 列表缓存 10 分钟
_pig_cache: dict = {"ts": 0.0, "items": []}


async def _pig_items() -> list[dict]:
    """获取 PigHub 图片列表（带缓存，失败时回退旧缓存）。"""
    now = time.time()
    if now - _pig_cache["ts"] <= PIG_LIST_TTL and _pig_cache["items"]:
        return _pig_cache["items"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(PIGHUB_API)
            resp.raise_for_status()
            items = resp.json().get("data") or []
            if items:
                _pig_cache.update(ts=now, items=items)
                return items
    except Exception:
        logger.warning("获取 PigHub 图片列表失败", exc_info=True)
    return _pig_cache["items"]


async def _random_pig_url() -> str | None:
    items = await _pig_items()
    if not items:
        return None
    url = random.choice(items).get("image_url") or ""
    if not url:
        return None
    if url.startswith("http"):
        return url
    return PIGHUB_BASE + quote(url, safe="/:")


pig_pic_cmd = on_command("小猪", priority=1, block=True)
register_help("/小猪", "随机发送一张 PigHub 的小猪图片")


@pig_pic_cmd.handle()
async def pig_pic_handler(bot: Bot, event: MessageEvent):
    url = await _random_pig_url()
    if not url:
        await pig_pic_cmd.finish("猪圈网络波动，抓不到小猪，稍后再试试～", at_sender=True)
    await pig_pic_cmd.finish(MessageSegment.image(url))


# ==================== 入典 ====================
DIAN_FILE = DATA_DIR / "dian.json"
DIAN_MAX = 500  # 典里最多保留的条目数


def _load_dian() -> dict:
    try:
        data = json.loads(DIAN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_dian(data: dict) -> None:
    DIAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIAN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _dian_entries(group_id: int) -> list[dict]:
    """读取指定群的典（各群独立）。"""
    return _load_dian().get("groups", {}).get(str(group_id), [])


def _add_dian(group_id: int, entry: dict) -> None:
    data = _load_dian()
    groups = data.setdefault("groups", {})
    entries = groups.setdefault(str(group_id), [])
    key = (entry.get("text") or "", tuple(entry.get("images") or []))
    if any(
        ((e.get("text") or ""), tuple(e.get("images") or [])) == key for e in entries
    ):
        return  # 同群重复内容不再收录
    entries.append(entry)
    del entries[:-DIAN_MAX]
    _save_dian(data)


def _to_seg_list(content) -> list[dict]:
    """把消息内容（Message 对象 / dict 段列表 / 字符串）统一成 dict 段列表。"""
    if isinstance(content, str):
        return [{"type": "text", "data": {"text": content}}]
    segs = []
    for seg in content or []:
        if isinstance(seg, dict):
            segs.append(seg)
        elif hasattr(seg, "type"):  # MessageSegment
            segs.append({"type": seg.type, "data": dict(seg.data or {})})
    return segs


def _msg_plain(content) -> str:
    """把消息内容（字符串或段列表）转成纯文本（图片段不产生文本，由 _extract_images 单独处理）。"""
    parts = []
    for seg in _to_seg_list(content):
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "face":
            parts.append("[表情]")
        elif t == "at":
            parts.append("@")
        # image 段跳过：文本里不留占位，图片统一存到 images 字段
    return "".join(parts).strip()


async def _extract_images(content) -> list[str]:
    """提取消息内容里的图片，下载到本地挂载目录，返回容器内可访问路径列表。"""
    paths = []
    for seg in _to_seg_list(content):
        if seg.get("type") == "image":
            url = (seg.get("data") or {}).get("url", "")
            if url:
                paths.append(await _local_image(url))
    return paths


def _ru_dian_rule(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    text = event.get_plaintext()
    if text.lstrip().startswith("/"):
        return False  # 命令消息（如 /删除入典 /全部入典）不当作入典
    # 「查看入典」走查看逻辑，不当作入典；只有明确含「入典」且回复了消息才算
    if "入典" not in text or "查看入典" in text:
        return False
    # reply 段会被 NoneBot 提取到 event.reply 并从 event.message 中删除，
    # 因此两种来源都要判断
    return event.reply is not None or any(
        seg.type == "reply" for seg in event.message
    )


ru_dian_matcher = on_message(rule=_ru_dian_rule, priority=1, block=True)


@ru_dian_matcher.handle()
async def ru_dian_handler(bot: Bot, event: GroupMessageEvent):
    # 优先取已提取的 event.reply（含被回复消息内容）；否则回退到 message 里的 reply 段
    content = None
    info = {"sender": "未知", "sender_id": None, "message_id": None, "time": 0}
    if event.reply is not None:
        content = event.reply.message
        s = event.reply.sender
        info.update(
            sender=s.card or s.nickname or str(s.user_id or ""),
            sender_id=s.user_id,
            message_id=event.reply.message_id,
            time=event.reply.time,
        )
    else:
        mid = next(
            ((seg.data or {}).get("id") for seg in event.message if seg.type == "reply"),
            None,
        )
        if mid:
            try:
                msg = await bot.get_msg(message_id=int(mid))
                content = msg.get("message")
                s = msg.get("sender") or {}
                info.update(
                    sender=s.get("card") or s.get("nickname") or str(s.get("user_id") or ""),
                    sender_id=s.get("user_id"),
                    message_id=int(mid),
                    time=msg.get("time") or 0,
                )
            except Exception:
                pass
    if content is None:
        await ru_dian_matcher.finish("（找不到被回复的消息）")
    text = _msg_plain(content)
    images = await _extract_images(content)
    if not text and not images:
        text = "（只有图片的消息）"
    info["text"] = text
    info["images"] = images
    _add_dian(event.group_id, info)
    await ru_dian_matcher.finish("（已入典）")


def _lookup_dian_rule(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    if "查看入典" in text:
        return True
    # 单字「典」，或「典 关键字」「典 @某人」
    return text == "典" or text.startswith("典 ")


lookup_dian_matcher = on_message(rule=_lookup_dian_rule, priority=1, block=True)


def _format_dian_entry(entry: dict) -> Message:
    """把一条典格式化成（图片 + 文本）消息。"""
    msg = Message()
    for img in entry.get("images") or []:
        msg.append(MessageSegment.image(img))
    text = entry.get("text") or ""
    sender = entry.get("sender") or f"QQ{entry.get('sender_id') or '?'}"
    ts = entry.get("time") or 0
    tstr = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
    mid = entry.get("message_id")
    head = f"{sender} {tstr}".strip()
    if mid:
        head += f" (ID:{mid})"
    body = f"「{text}」\n—— {head}" if text else f"—— {head}"
    msg.append(MessageSegment.text(body))
    return msg


@lookup_dian_matcher.handle()
async def lookup_dian_handler(bot: Bot, event: MessageEvent):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    entries = _dian_entries(group_id)
    if not entries:
        await lookup_dian_matcher.finish("典里还是空的，快去回复一条消息说「入典」吧～")

    text = event.get_plaintext().strip()
    # 目标 QQ：消息里有 @ 段则按人查
    at_qq = None
    for seg in event.message:
        if seg.type == "at" and seg.data.get("qq") not in (None, "all"):
            at_qq = int(seg.data["qq"])
            break
    # 关键字：去掉「查看入典」/「典」前缀后的剩余文本
    keyword = ""
    if text.startswith("查看入典"):
        keyword = text[len("查看入典"):].strip()
    elif text.startswith("典"):
        keyword = text[1:].strip()

    if at_qq is not None:
        matched = [e for e in entries if e.get("sender_id") == at_qq]
        if not matched:
            await lookup_dian_matcher.finish(f"QQ{at_qq} 还没有被入典的内容～")
        entry = random.choice(matched)
    elif keyword:
        best, best_score = None, 0.0
        for e in entries:
            s = difflib.SequenceMatcher(None, keyword, e.get("text") or "").ratio()
            if s > best_score:
                best, best_score = e, s
        if best is None or best_score <= 0:
            await lookup_dian_matcher.finish(f"典里没有和「{keyword}」相关的内容～")
        entry = best
    else:
        entry = random.choice(entries)

    await lookup_dian_matcher.finish(_format_dian_entry(entry))


# ==================== /删除入典 ====================
del_dian_cmd = on_command("删除入典", priority=1, block=True)
register_help("/删除入典", "删除入典：/删除入典 @某人 或 /删除入典 某段话；缺省删除本群全部")


def _arg_at(arg: Message) -> int | None:
    """从命令参数里提取被 @ 的 QQ（忽略 @all）。"""
    for seg in arg:
        if seg.type == "at" and seg.data.get("qq") not in (None, "all"):
            return int(seg.data["qq"])
    return None


@del_dian_cmd.handle()
async def del_dian_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    data = _load_dian()
    entries = data.get("groups", {}).get(str(group_id))
    if not entries:
        await del_dian_cmd.finish("典里还是空的，没有可删除的内容～")

    at_qq = _arg_at(arg)
    keyword = arg.extract_plain_text().strip()

    if at_qq is not None:
        keep = [e for e in entries if e.get("sender_id") != at_qq]
        removed = len(entries) - len(keep)
        if not removed:
            await del_dian_cmd.finish(f"QQ{at_qq} 还没有被入典的内容～")
    elif keyword:
        keep = [e for e in entries if keyword not in (e.get("text") or "")]
        removed = len(entries) - len(keep)
        if not removed:
            await del_dian_cmd.finish(f"典里没有和「{keyword}」相关的内容～")
    else:
        keep, removed = [], len(entries)

    data["groups"][str(group_id)] = keep
    _save_dian(data)
    await del_dian_cmd.finish(f"已删除 {removed} 条入典。")


# ==================== /全部入典 ====================
all_dian_cmd = on_command("全部入典", priority=1, block=True)
register_help("/全部入典", "合并转发查看本群全部入典；/全部入典 @某人 只看某个人的")


@all_dian_cmd.handle()
async def all_dian_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    entries = _dian_entries(group_id)
    if not entries:
        await all_dian_cmd.finish("典里还是空的，快去回复一条消息说「入典」吧～")

    at_qq = _arg_at(arg)
    if at_qq is not None:
        entries = [e for e in entries if e.get("sender_id") == at_qq]
        if not entries:
            await all_dian_cmd.finish(f"QQ{at_qq} 还没有被入典的内容～")

    nodes = []
    for e in entries:
        content = Message()
        for img in e.get("images") or []:
            content.append(MessageSegment.image(img))
        text = e.get("text") or ""
        ts = e.get("time") or 0
        tstr = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
        mid = e.get("message_id")
        head = tstr if tstr else ""
        if mid:
            head = f"{head} (ID:{mid})" if head else f"(ID:{mid})"
        body = f"「{text}」" + (f" {head}" if head else "")
        content.append(MessageSegment.text(body))
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": e.get("sender") or f"QQ{e.get('sender_id') or '?'}",
                    "uin": str(e.get("sender_id") or 0),
                    "content": content,
                },
            }
        )
    try:
        await bot.call_api("send_forward_msg", group_id=group_id, messages=nodes)
    except Exception:
        logger.exception("全部入典合并转发失败")
        await all_dian_cmd.finish("合并转发发送失败，稍后再试试～")


# ==================== /展示：合并转发展示功能用法 ====================
_FAKE_UINS = {"小明": "10001", "小红": "10002"}

# 功能名 -> 一段演示对话（说话人, 内容）
SHOWCASES: dict[str, list[tuple[str, str]]] = {
    "今日分院": [
        ("小明", "@分院帽 今日分院"),
        ("分院帽", "@小明 命运之帽已落定……格兰芬多！"),
    ],
    "判刑": [
        ("小明", "@分院帽 /判刑 @小红 3"),
        ("分院帽", "已将 QQ 小红 送入阿兹卡班服刑 3 小时。"),
    ],
    "赦免": [
        ("小明", "@分院帽 /赦免 @小红"),
        ("分院帽", "已赦免 QQ 小红，欢迎回到魔法世界。"),
    ],
    "重新分院": [
        ("小明", "@分院帽 /重新分院"),
        ("分院帽", "重新为你聆听命运的呼唤……嗯……赫奇帕奇！"),
    ],
    "op": [
        ("小明", "@分院帽 /op @小红"),
        ("分院帽", "已给 QQ 小红 授予 OP 权限。"),
    ],
    "suop": [
        ("小明", "@分院帽 /suop"),
        ("分院帽", "已给本群 3 位管理员/群主授予 OP 权限。"),
    ],
    "deop": [
        ("小明", "@分院帽 /deop @小红"),
        ("分院帽", "已取消 QQ 小红 的 OP 权限。"),
    ],
    "贴猴": [
        ("小明", "@分院帽 /贴猴 @小红"),
        ("分院帽", "已给 QQ 小红 贴上 🐵，他每发一条消息我都会贴一个 🐵 表情。"),
    ],
    "贴猪": [
        ("小明", "@分院帽 /贴猪 @小红"),
        ("分院帽", "已给 QQ 小红 贴猪，他每发一条消息我都会贴一个 ㊗️ 表情。"),
    ],
    "小猪": [
        ("小明", "/小猪"),
        ("分院帽", "（随机发送一张 PigHub 的可爱小猪图片）"),
    ],
    "问答": [
        ("小明", "/问答\n问\n分院帽是傻子吗\n答\n不是，我是最聪明的帽子"),
        ("分院帽", "我记住了。"),
        ("小明", "分院帽是傻子吗"),
        ("分院帽", "不是，我是最聪明的帽子"),
    ],
    "忘记": [
        ("小明", "/忘记\n分院帽是傻子吗"),
        ("分院帽", "已忘记 1 条关于「分院帽是傻子吗」的问答。"),
    ],
    "省流": [
        ("小明", "/省流"),
        ("分院帽", "省流版：大家聊了分院、阿兹卡班和今晚的魁地奇比赛……"),
    ],
    "入典": [
        ("小明", "（回复一条消息）入典"),
        ("分院帽", "（已入典）"),
        ("小明", "查看入典"),
        ("分院帽", "（随机回复一条典里的内容）"),
    ],
    "reset": [
        ("小明", "/reset"),
        ("分院帽", "好的，我已经忘记之前说过的话，重新开始吧～"),
    ],
    "help": [
        ("小明", "/help"),
        ("分院帽", "我是【分院帽】……指令：/今日分院 /判刑 /赦免 ……"),
    ],
    "展示": [
        ("小明", "/展示 /判刑"),
        ("分院帽", "（用合并转发展示一段演示对话）"),
    ],
}


def _show_nodes(lines: list[tuple[str, str]], bot_uin: str) -> list[dict]:
    uins = {**_FAKE_UINS, "分院帽": bot_uin}
    nodes = []
    for name, text in lines:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": name,
                    "uin": uins.get(name, "10001"),
                    "content": [{"type": "text", "data": {"text": text}}],
                },
            }
        )
    return nodes


def _show_list_text() -> str:
    return "可展示功能：" + "、".join("/" + k for k in sorted(SHOWCASES))


show_cmd = on_command("展示", priority=1, block=True)
register_help("/展示", "合并转发展示某个功能的用法，如 /展示 /判刑")


@show_cmd.handle()
async def show_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    key = arg.extract_plain_text().strip().lstrip("/").lower()
    if not key or key == "展示":
        await show_cmd.finish("用法：/展示 /功能名\n" + _show_list_text(), at_sender=True)
    lines = SHOWCASES.get(key)
    if not lines:
        await show_cmd.finish(f"没有「{key}」的展示。\n" + _show_list_text(), at_sender=True)
    try:
        params = {"messages": _show_nodes(lines, str(bot.self_id or "10001"))}
        if isinstance(event, GroupMessageEvent):
            params["group_id"] = event.group_id
        else:
            params["user_id"] = event.user_id
        await bot.call_api("send_forward_msg", **params)
    except Exception:
        logger.exception("合并转发发送失败")
        await show_cmd.finish("合并转发发送失败，稍后再试试～", at_sender=True)


# ==================== /栽桩：伪造某人发送消息 ====================
zhuang_cmd = on_command("栽桩", aliases={"栽赃"}, priority=1, block=True)
register_help("/栽桩", "伪造某人发送消息（合并转发），如 /栽桩 @小明 我今晚通宵打游戏")


async def _local_image(url: str) -> str:
    """把图片 URL 下载到本地挂载目录，返回容器内可访问的路径；失败时返回原 URL。"""
    try:
        QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
        filename = hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + ".jpg"
        path = QA_IMG_DIR / filename
        if not path.exists():
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
        return f"/app/napcat/qa_images/{filename}"
    except Exception:
        logger.warning("栽桩图片下载失败，使用原 URL")
        return url


@zhuang_cmd.handle()
async def zhuang_handler(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    nodes = await _build_zhuang_nodes(bot, event, arg)
    if not nodes:
        await zhuang_cmd.finish(
            "格式：\n/栽桩\n@某人 内容\n@某人 内容\n（每行一个 @+内容，可组成对话；"
            "不带 @ 的行会算作你自己发的）",
            at_sender=True,
        )
    try:
        await bot.call_api("send_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception:
        logger.exception("栽桩合并转发发送失败")
        await zhuang_cmd.finish("合并转发发送失败，稍后再试试～", at_sender=True)


async def _build_zhuang_nodes(bot: Bot, event: GroupMessageEvent, arg: Message) -> list[dict]:
    """把 /栽桩 参数解析成合并转发节点列表。

    每行「@某人 内容」是一个发言节点；不带 @ 的行算作发命令的人发言；
    图片行跟随当前发言者；单行时即旧版单个转发。
    """
    # 拆成 token 流：("at", qq) | ("text", 行) | ("image", seg)
    tokens: list[tuple] = []
    for seg in arg:
        if seg.type == "at" and seg.data.get("qq") not in (None, "all"):
            tokens.append(("at", int(seg.data["qq"])))
        elif seg.type == "text":
            for ln in seg.data.get("text", "").splitlines():
                if ln.strip():
                    tokens.append(("text", ln.strip()))
        elif seg.type == "image":
            tokens.append(("image", seg))

    self_qq = event.user_id
    rows: list[dict] = []
    cur: dict | None = None
    for kind, val in tokens:
        if kind == "at":
            cur = {"target": val, "content": Message()}
            rows.append(cur)
        elif kind == "text":
            if cur is None:
                cur = {"target": self_qq, "content": Message()}
                rows.append(cur)
            cur["content"].append(MessageSegment.text(val))
        else:  # image
            if cur is None:
                cur = {"target": self_qq, "content": Message()}
                rows.append(cur)
            url = (val.data or {}).get("url", "")
            if url:
                cur["content"].append(MessageSegment.image(await _local_image(url)))
            else:
                cur["content"].append(MessageSegment.image(val.data.get("file", "")))

    rows = [r for r in rows if r["content"]]
    if not rows:
        return []

    # 生成 node 列表（同一人昵称只查一次）
    name_cache: dict[int, str] = {}
    nodes = []
    for r in rows:
        qq = r["target"]
        if qq not in name_cache:
            try:
                info = await bot.get_group_member_info(group_id=event.group_id, user_id=qq)
                name_cache[qq] = info.get("card") or info.get("nickname") or str(qq)
            except Exception:
                name_cache[qq] = str(qq)
        nodes.append(
            {
                "type": "node",
                "data": {"name": name_cache[qq], "uin": str(qq), "content": r["content"]},
            }
        )
    return nodes


# 启动自检日志：确认本文件最新代码已被加载（排查"改代码没生效"问题）
logger.info("fun_tools 已加载 v2：入典 matcher=%s 查看入典 matcher=%s",
            bool(ru_dian_matcher), bool(lookup_dian_matcher))
