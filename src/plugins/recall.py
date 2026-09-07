"""撤回消息记录与 /查看撤回

- 自动记录每个群的普通消息（含 message_id、内容、发送者、图片），用于撤回后找回内容
- 监听群消息撤回通知（group_recall），把被撤回的消息内容存档（含图片本地化）
- /查看撤回 [@某人] [时间参数] [-a]：
  - @某人：只看某个人的撤回；缺省看所有人
  - 时间参数：-2h / -30m / -1d，缺省过去 1 小时，最多 24 小时
  - -a：附带每条撤回消息的上下文（前后各 5 条）
  - 用合并转发展示；连续撤回（同一人、间隔 5 分钟内）合并为一个节点
  - 仅管理员（OP）可用
"""
import hashlib
import json
import logging
import re
import time

import httpx
from nonebot import on_command, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
)
from nonebot.params import CommandArg

from .admin_tools import OP_SEED, is_op
from .common import DATA_DIR, QA_IMG_DIR, register_help

logger = logging.getLogger("sorting_hat.recall")

MSGS_FILE = DATA_DIR / "recall_msgs.jsonl"    # 群消息记录（含 message_id）
RECALLS_FILE = DATA_DIR / "recalls.jsonl"     # 撤回记录
KEEP_HOURS = 24                                # 记录保留窗口（小时）
MAX_MSGS = 200                                 # 每群内存最多保留消息条数（撤回找回够用，避免内存膨胀）
MAX_RECALLS = 500                              # 每群最多保留撤回条数
TRIM_BYTES = 20 * 1024 * 1024                  # 消息记录文件超过该大小后裁剪（保留 24h）
CTX_COUNT = 5                                  # -a 附带的前后消息条数
ROOT_ONLY_GROUPS = {603421145}                 # 这些群 /查看撤回 仅根管理员可用，其它群为管理员可用

_msgs: dict[int, list[dict]] = {}       # group_id -> [{"id","u","x","imgs","t"}]
_recalls: dict[int, list[dict]] = {}    # group_id -> [{"mid","u","name","x","imgs","t","rt"}]
_nick_cache: dict[tuple[int, int], tuple[str, float]] = {}
_trim_count = 0                         # 写入计数，用于节流触发文件裁剪


def _trim_msgs_file() -> None:
    """消息记录文件过大时裁掉超过 KEEP_HOURS 的旧记录，防止无限膨胀。"""
    try:
        size = MSGS_FILE.stat().st_size
        if size < TRIM_BYTES:
            return
        cutoff = time.time() - KEEP_HOURS * 3600
        keep = []
        with open(MSGS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("t", 0) >= cutoff:
                    keep.append(r)
        tmp = MSGS_FILE.with_suffix(".trim")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(MSGS_FILE)
        logger.info("消息记录已裁剪，保留 %d 条", len(keep))
    except Exception:
        logger.warning("消息记录裁剪失败", exc_info=True)


# ---------- 消息记录 ----------
def _msg_rule(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


msg_logger = on_message(rule=_msg_rule, priority=20, block=False)


@msg_logger.handle()
async def msg_record_handler(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    imgs = [
        (seg.data or {}).get("url", "")
        for seg in event.message
        if seg.type == "image" and (seg.data or {}).get("url")
    ]
    entry = {
        "id": event.message_id,
        "u": event.user_id,
        "x": text,
        "imgs": imgs,
        "t": time.time(),
    }
    buf = _msgs.setdefault(event.group_id, [])
    buf.append(entry)
    del buf[:-MAX_MSGS]
    _persist_msg(event.group_id, entry)
    global _trim_count
    _trim_count += 1
    if _trim_count % 500 == 0:
        _trim_msgs_file()


def _persist_msg(group_id: int, entry: dict) -> None:
    try:
        line = json.dumps({"g": group_id, **entry}, ensure_ascii=False)
        MSGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MSGS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("撤回消息记录持久化失败", exc_info=True)


# ---------- 撤回监听 ----------
def _recall_rule(event: NoticeEvent) -> bool:
    """注意：注解必须用 NoticeEvent（GroupRecallNoticeEvent 的父类），
    若用 MessageEvent 会导致 NoneBot 依赖注入类型不匹配而规则永为 False。"""
    return isinstance(event, GroupRecallNoticeEvent)


recall_notice = on_notice(rule=_recall_rule, priority=1)


async def _download_img(url: str) -> str:
    """把图片下载到本地挂载目录，返回容器内路径；失败返回原 URL。"""
    try:
        QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
        filename = hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + ".jpg"
        path = QA_IMG_DIR / filename
        if not path.exists():
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
        return f"/app/napcat/qa_images/{filename}"
    except Exception:
        return url


@recall_notice.handle()
async def recall_notice_handler(bot: Bot, event: GroupRecallNoticeEvent):
    group_id = event.group_id
    uid = event.user_id
    msg_id = event.message_id
    entry = next((m for m in _msgs.get(group_id, []) if m["id"] == msg_id), None)
    if entry is None:
        return  # 记录里没有（可能是重启前发送的），跳过
    name = str(uid)
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=uid)
        name = info.get("card") or info.get("nickname") or str(uid)
    except Exception:
        pass
    imgs = [await _download_img(url) for url in entry.get("imgs", [])]
    rec = {
        "mid": msg_id,
        "u": uid,
        "name": name,
        "x": entry.get("x", ""),
        "imgs": imgs,
        "t": entry.get("t", time.time()),
        "rt": time.time(),
    }
    buf = _recalls.setdefault(group_id, [])
    buf.append(rec)
    del buf[:-MAX_RECALLS]
    try:
        line = json.dumps({"g": group_id, **rec}, ensure_ascii=False)
        RECALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RECALLS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("撤回记录持久化失败", exc_info=True)


# ---------- 持久化恢复 ----------
def _load() -> None:
    now = time.time()
    try:
        for line in MSGS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                buf = _msgs.setdefault(int(d["g"]), [])
                buf.append({k: d[k] for k in ("id", "u", "x", "imgs", "t")})
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("加载撤回消息记录失败", exc_info=True)
    try:
        for line in RECALLS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                buf = _recalls.setdefault(int(d["g"]), [])
                buf.append({k: d[k] for k in ("mid", "u", "name", "x", "imgs", "t", "rt")})
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("加载撤回记录失败", exc_info=True)
    # 清理超时记录并重写文件（防止无限增长）
    for g in list(_msgs):
        _msgs[g] = [m for m in _msgs[g] if now - m["t"] < KEEP_HOURS * 3600][-MAX_MSGS:]
    for g in list(_recalls):
        _recalls[g] = [r for r in _recalls[g] if now - r["rt"] < KEEP_HOURS * 3600][-MAX_RECALLS:]
    try:
        with open(MSGS_FILE, "w", encoding="utf-8") as f:
            for g, buf in _msgs.items():
                for m in buf:
                    f.write(json.dumps({"g": g, **m}, ensure_ascii=False) + "\n")
        with open(RECALLS_FILE, "w", encoding="utf-8") as f:
            for g, buf in _recalls.items():
                for r in buf:
                    f.write(json.dumps({"g": g, **r}, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("重写撤回记录文件失败", exc_info=True)


# ---------- /查看撤回 ----------
recall_cmd = on_command("查看撤回", priority=1, block=True)
register_help("/查看撤回", "查看撤回消息（仅管理员）：/查看撤回 [@某人] [-2h] [-a]；缺省1小时，最多24h，-a带上下文")


def _parse_args(arg_text: str, message: Message) -> tuple[int | None, int, bool]:
    """解析参数，返回 (目标QQ或None, 时间窗口秒数, 是否带上下文-a)。"""
    at_qq = None
    for seg in message:
        if seg.type == "at" and seg.data.get("qq") not in (None, "all"):
            at_qq = int(seg.data["qq"])
            break
    s = arg_text.lower()
    window = 3600  # 缺省 1 小时
    m = re.search(r"(\d+)\s*([hmd])", s)
    if m:
        num = int(m.group(1))
        unit = {"h": 3600, "m": 60, "d": 86400}[m.group(2)]
        window = max(60, min(num * unit, 24 * 3600))  # 至少1分钟，最多24小时
    with_ctx = "-a" in s
    return at_qq, window, with_ctx


def _fmt_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


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


def _get_context(group_id: int, mid: int) -> list[dict]:
    """取某条消息在记录里前后各 CTX_COUNT 条上下文（含自身）。"""
    buf = _msgs.get(group_id, [])
    idx = next((i for i, m in enumerate(buf) if m["id"] == mid), None)
    if idx is None:
        return []
    start = max(0, idx - CTX_COUNT)
    return buf[start:idx + CTX_COUNT + 1]


async def _build_nodes(bot: Bot, group_id: int, recs: list[dict], with_ctx: bool) -> list[dict]:
    nodes: list[dict] = []
    prev_rt = 0.0
    for r in recs:
        if with_ctx:
            content = Message()
            for m in _get_context(group_id, r["mid"]):
                name = await _get_nick(bot, group_id, m["u"])
                body = m["x"] or "(图片)"
                content.append(MessageSegment.text(f"[{_fmt_hm(m['t'])}] {name}：{body}\n"))
            content.append(MessageSegment.text(f"▶ [{_fmt_hm(r['t'])}] {r['name']} 撤回：{r['x'] or ''}"))
            for img in r.get("imgs") or []:
                content.append(MessageSegment.image(img))
            nodes.append(
                {"type": "node", "data": {"name": r["name"], "uin": str(r["u"]), "content": content}}
            )
            continue
        # 无 -a：连续撤回（同一人、间隔 < 5 分钟）合并为一个节点
        if (
            nodes
            and nodes[-1]["data"]["uin"] == str(r["u"])
            and r["rt"] - prev_rt < 300
        ):
            nodes[-1]["data"]["content"].append(
                MessageSegment.text(f"\n[{_fmt_hm(r['rt'])}] {r['x'] or '(图片)'}")
            )
            for img in r.get("imgs") or []:
                nodes[-1]["data"]["content"].append(MessageSegment.image(img))
            prev_rt = r["rt"]
            continue
        content = Message(MessageSegment.text(f"[{_fmt_hm(r['rt'])}] {r['x'] or '(图片)'}"))
        for img in r.get("imgs") or []:
            content.append(MessageSegment.image(img))
        nodes.append(
            {"type": "node", "data": {"name": r["name"], "uin": str(r["u"]), "content": content}}
        )
        prev_rt = r["rt"]
    return nodes


@recall_cmd.handle()
async def recall_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    if not group_id:
        await recall_cmd.finish("该命令仅在群聊中可用～", at_sender=True)
    if group_id in ROOT_ONLY_GROUPS:
        if event.user_id != OP_SEED:
            await recall_cmd.finish("该群仅根管理员可用～", at_sender=True)
    elif not is_op(event.user_id):
        await recall_cmd.finish("仅管理员可用～", at_sender=True)

    at_qq, window, with_ctx = _parse_args(arg.extract_plain_text(), event.message)
    now = time.time()
    recs = [r for r in _recalls.get(group_id, []) if now - r["rt"] <= window]
    if at_qq is not None:
        recs = [r for r in recs if r["u"] == at_qq]
    if not recs:
        tip = "该时间范围内没有人撤回消息～" if at_qq is None else f"QQ{at_qq} 没有撤回消息～"
        await recall_cmd.finish(tip, at_sender=True)

    recs.sort(key=lambda r: r["rt"])
    nodes = await _build_nodes(bot, group_id, recs, with_ctx)
    try:
        await bot.call_api("send_forward_msg", group_id=group_id, messages=nodes)
    except Exception:
        logger.exception("查看撤回合并转发失败")
        await recall_cmd.finish("合并转发发送失败，稍后再试～", at_sender=True)


# 模块加载时恢复记录
_load()
