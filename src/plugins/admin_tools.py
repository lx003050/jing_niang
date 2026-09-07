"""管理员工具：OP 管理、贴猴、问答库、ophelp

- /op @某人        授予 OP 权限（仅 OP）
- /deop @某人      取消某人的 OP 权限（仅根管理员）
- /suop            给本群所有管理员和群主授予 OP 权限（仅 OP）
- /贴猴 @某人      给某人贴 🐵，此后他每发一条消息，机器人都在该消息下贴一个 🐵 表情回应（仅 OP）
- /取消贴猴 @某人  摘掉某人的 🐵（仅 OP）
- /贴猪 @某人      给某人贴 ㊗️，此后他每发一条消息，机器人都在该消息下贴一个 ㊗️ 表情回应（仅 OP）
- /取消贴猪 @某人  摘掉某人的 ㊗️（仅 OP）
- /问答            录入一问一答（仅 OP）：
                    /问答 [-a|-t]
                    问
                    （问内容）
                    答
                    （答内容）
                    —— 之后群内出现完全等于「问内容」的消息时，自动回复「答内容」；
                    -a 表示全部群可触发，-t 或缺省表示仅本群触发；
                    同一个问句有多个答内容时随机回复其中一个；答内容可带图片。
- /忘记            （仅 OP）：
                    /忘记
                    （问内容）
                    —— 忘记对应的一问一答
- /ophelp          查看全部（含 OP）帮助（仅 OP）

以上 OP 指令均不出现在普通 /help 中。
"""
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

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

from .common import (
    DATA_DIR,
    HELP_DESC,
    QA_IMG_DIR,
    WL_CHAT,
    WL_MANAGE_CMDS,
    hide_help,
    register_help,
    send_forward_text,
    wl_disable,
    wl_enable,
    wl_features,
)
from .events import _build_help_text, _collect_command_forms

logger = logging.getLogger("sorting_hat.admin_tools")

OPS_FILE = DATA_DIR / "ops.json"
MONKEY_FILE = DATA_DIR / "monkey.json"
PIG_FILE = DATA_DIR / "pig.json"
QA_FILE = DATA_DIR / "qa_pairs.json"

QA_IMG_MOUNT = "/app/napcat/qa_images"  # NapCat 容器内挂载路径（对应宿主机 QA_IMG_DIR）

OP_SEED = ai_config.root_seed  # 根管理员 QQ（.env: ROOT_SEED；0=未配置，此时仅 /op 名单生效）


# ---------- 基础存取 ----------
def _load_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- OP 集合 ----------
def is_op(uid: int) -> bool:
    ops = {int(u) for u in _load_json(OPS_FILE, {}).get("ops", []) if str(u).isdigit()}
    ops.add(OP_SEED)  # 种子管理员永远有效
    return uid in ops


def add_op(uid: int) -> None:
    ops = {int(u) for u in _load_json(OPS_FILE, {}).get("ops", []) if str(u).isdigit()}
    ops.add(uid)
    _save_json(OPS_FILE, {"ops": sorted(ops)})


def remove_op(uid: int) -> bool:
    ops = {int(u) for u in _load_json(OPS_FILE, {}).get("ops", []) if str(u).isdigit()}
    if uid not in ops:
        return False
    ops.discard(uid)
    _save_json(OPS_FILE, {"ops": sorted(ops)})
    return True


# ---------- 贴猴 ----------
def _monkey_users() -> set[int]:
    return {int(u) for u in _load_json(MONKEY_FILE, {}).get("users", []) if str(u).isdigit()}


def is_monkey(uid: int) -> bool:
    return uid in _monkey_users()


def add_monkey(uid: int) -> None:
    users = _monkey_users()
    users.add(uid)
    _save_json(MONKEY_FILE, {"users": sorted(users)})


def remove_monkey(uid: int) -> bool:
    users = _monkey_users()
    if uid not in users:
        return False
    users.discard(uid)
    _save_json(MONKEY_FILE, {"users": sorted(users)})
    return True


# ---------- 贴猪 ----------
def _pig_users() -> set[int]:
    return {int(u) for u in _load_json(PIG_FILE, {}).get("users", []) if str(u).isdigit()}


def is_pig(uid: int) -> bool:
    return uid in _pig_users()


def add_pig(uid: int) -> None:
    users = _pig_users()
    users.add(uid)
    _save_json(PIG_FILE, {"users": sorted(users)})


def remove_pig(uid: int) -> bool:
    users = _pig_users()
    if uid not in users:
        return False
    users.discard(uid)
    _save_json(PIG_FILE, {"users": sorted(users)})
    return True


# ---------- 问答库 ----------
def _qa_pairs() -> list[dict]:
    return _load_json(QA_FILE, {}).get("pairs", []) or []


def _save_qa(pairs: list[dict]) -> None:
    _save_json(QA_FILE, {"pairs": pairs})


def find_answers(q: str, group_id: int = 0) -> list:
    """返回问句对应的全部答案。

    group_id 过滤：条目 group_id 为 0（全部群）或等于当前群时才算。
    旧条目没有 group_id 字段时按全部群处理。group_id 传 0 表示不过滤。
    """
    return [
        p["a"]
        for p in _qa_pairs()
        if p.get("q") == q
        and (not group_id or p.get("group_id", 0) in (0, group_id))
    ]


def find_answer(q: str) -> str | None:
    """随机返回一个答案（兼容旧调用）。"""
    answers = find_answers(q)
    return random.choice(answers) if answers else None


def _extract_at(event: MessageEvent) -> int | None:
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq and qq != "all":
                return int(qq)
    return None


# ---------- /op ----------
op_cmd = on_command("op", priority=1, block=True)
register_help("/op", "给某人 OP 权限（仅管理员）")
hide_help("/op")


@op_cmd.handle()
async def op_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await op_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认授予自己
    add_op(target)
    await op_cmd.finish(f"已给 QQ {target} 授予 OP 权限。", at_sender=True)


# ---------- /suop ----------
suop_cmd = on_command("suop", priority=1, block=True)
register_help("/suop", "给本群所有管理员和群主授予 OP 权限（仅管理员）")
hide_help("/suop")


@suop_cmd.handle()
async def suop_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await suop_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    if not isinstance(event, GroupMessageEvent):
        await suop_cmd.finish("这个指令只能在群里使用。", at_sender=True)
    try:
        members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception:
        await suop_cmd.finish("获取群成员列表失败，稍后再试试。", at_sender=True)
    targets = [m["user_id"] for m in members if m.get("role") in ("owner", "admin")]
    for uid in targets:
        add_op(uid)
    await suop_cmd.finish(f"已给本群 {len(targets)} 位管理员/群主授予 OP 权限。", at_sender=True)


# ---------- /deop ----------
deop_cmd = on_command("deop", priority=1, block=True)
register_help("/deop", "取消某人的 OP 权限（仅根管理员）")
hide_help("/deop")


@deop_cmd.handle()
async def deop_handler(bot: Bot, event: MessageEvent):
    if event.user_id != OP_SEED:  # 仅根管理员可用
        await deop_cmd.finish("只有根管理员才能取消别人的 OP。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认取消自己
    if target == OP_SEED:
        await deop_cmd.finish("根管理员不能取消自己的 OP。", at_sender=True)
    if remove_op(target):
        await deop_cmd.finish(f"已取消 QQ {target} 的 OP 权限。", at_sender=True)
    await deop_cmd.finish(f"QQ {target} 本来就不是 OP。", at_sender=True)


# ---------- /贴猴 ----------
monkey_cmd = on_command("贴猴", priority=1, block=True)
register_help("/贴猴", "给某人贴 🐵，此后他发言时在消息下贴 🐵 表情回应（仅管理员）")
hide_help("/贴猴")


@monkey_cmd.handle()
async def monkey_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await monkey_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认贴自己
    if is_monkey(target):
        await monkey_cmd.finish(f"QQ {target} 已经在猴山上啦～", at_sender=True)
    add_monkey(target)
    await monkey_cmd.finish(f"已给 QQ {target} 贴上 🐵，他每发一条消息我都会贴一个 🐵 表情。", at_sender=True)


# ---------- /取消贴猴 ----------
unmonkey_cmd = on_command("取消贴猴", priority=1, block=True)
register_help("/取消贴猴", "摘掉某人的 🐵（仅管理员）")
hide_help("/取消贴猴")


@unmonkey_cmd.handle()
async def unmonkey_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await unmonkey_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认摘自己
    if remove_monkey(target):
        await unmonkey_cmd.finish(f"已摘掉 QQ {target} 的 🐵。", at_sender=True)
    await unmonkey_cmd.finish(f"QQ {target} 本来就没有 🐵。", at_sender=True)


# ---------- 贴表情监听：被贴猴/贴猪者每发一条消息就在消息下贴对应表情 ----------
# 同一用户两次贴表情的最小间隔（秒），避免连续刷屏触发风控
# QQ 表情回应的 emoji_id 必须用 Unicode 十进制码点（🐵=U+1F435=128053，㊗️=U+3297=12951），
# 直接传 emoji 字符或十六进制字符串会被渲染成别的表情（如 12953=U+3299㊙ 不是 ㊗️）
EMOJI_COOLDOWN = 2.0
MONKEY_EMOJI_ID = "128053"   # 🐵
PIG_EMOJI_ID = "12951"       # ㊗️（贴猪用的表情）
_last_emoji: dict[int, float] = {}


def _emoji_rule(event: MessageEvent) -> bool:
    return is_monkey(event.user_id) or is_pig(event.user_id)


emoji_reply_matcher = on_message(rule=_emoji_rule, priority=100, block=False)


@emoji_reply_matcher.handle()
async def emoji_reply_handler(bot: Bot, event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return  # 表情回应仅用于群消息
    now = time.time()
    if now - _last_emoji.get(event.user_id, 0) < EMOJI_COOLDOWN:
        return
    _last_emoji[event.user_id] = now
    uid = event.user_id
    emoji_id = MONKEY_EMOJI_ID if is_monkey(uid) else PIG_EMOJI_ID
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=event.message_id,
            emoji_id=emoji_id,
            set=True,
        )
    except Exception:
        logger.warning("贴表情失败: uid=%s message_id=%s", uid, event.message_id)


# ---------- /贴猪 ----------
pig_cmd = on_command("贴猪", priority=1, block=True)
register_help("/贴猪", "给某人贴 ㊗️，此后他发言时在消息下贴 ㊗️ 表情回应（仅管理员）")
hide_help("/贴猪")


@pig_cmd.handle()
async def pig_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await pig_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认贴自己
    if is_pig(target):
        await pig_cmd.finish(f"QQ {target} 已经趴在猪圈里啦～", at_sender=True)
    add_pig(target)
    await pig_cmd.finish(f"已给 QQ {target} 贴猪，他每发一条消息我都会贴一个 ㊗️ 表情。", at_sender=True)


# ---------- /取消贴猪 ----------
unpig_cmd = on_command("取消贴猪", priority=1, block=True)
register_help("/取消贴猪", "摘掉某人的 ㊗️（仅管理员）")
hide_help("/取消贴猪")


@unpig_cmd.handle()
async def unpig_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await unpig_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认摘自己
    if remove_pig(target):
        await unpig_cmd.finish(f"已摘掉 QQ {target} 的 ㊗️。", at_sender=True)
    await unpig_cmd.finish(f"QQ {target} 本来就没有 ㊗️。", at_sender=True)


# ---------- /问答 ----------
qa_add_cmd = on_command("问答", priority=1, block=True)
register_help("/问答", "录入一问一答（仅管理员）")
hide_help("/问答")


@qa_add_cmd.handle()
async def qa_add_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not is_op(event.user_id):
        await qa_add_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    q, a_msg, scope = _parse_qa(arg)
    if not q or not a_msg:
        await qa_add_cmd.finish(
            "格式：\n/问答 [-a|-t]\n问\n（问内容）\n答\n（答内容，可带图片）\n"
            "其中 -a 表示全部群可触发，-t 或缺省表示仅本群触发。",
            at_sender=True,
        )
    # 把答案里的网络图片下载到本地，避免依赖会过期的临时 URL（rkey）
    a_msg = await _persist_images(a_msg)
    # scope_group: 0 = 全部群，>0 = 仅该群
    scope_group = 0 if scope == "-a" else getattr(event, "group_id", 0)
    pairs = _qa_pairs()
    pairs.append({"q": q, "a": _msg_to_dicts(a_msg), "group_id": scope_group})
    _save_qa(pairs)
    where = "所有群" if scope_group == 0 else "本群"
    await qa_add_cmd.finish(f"我记住了（{where}触发）。", at_sender=True)


async def _persist_images(msg: Message) -> Message:
    """把答案消息中的图片持久化到本地数据目录，file 换成容器内可访问路径。

    - 优先下载网络 url；url 缺失或下载失败时，回退读取本地 file 引用（file:// 或容器挂载路径）；
    - 全部失败则保留原 data，至少尝试原样发送。
    """
    segs = []
    for seg in msg:
        if seg.type == "image":
            data = dict(seg.data or {})
            url = str(data.get("url") or "")
            content: bytes | None = None
            if url.startswith(("http://", "https://")):
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(url)
                        resp.raise_for_status()
                    content = resp.content
                except Exception:
                    content = None
            if content is None:
                for ref in (str(data.get("file") or ""), url):
                    p = _host_path_of_file_ref(ref)
                    if p is None or not p.exists():
                        continue
                    try:
                        content = p.read_bytes()
                    except Exception:
                        content = None
                    if content is not None:
                        break
            if content is not None:
                try:
                    QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
                    seed = url or str(data.get("file") or "img")
                    filename = hashlib.md5(seed.encode("utf-8")).hexdigest()[:16] + ".jpg"
                    path = QA_IMG_DIR / filename
                    if not path.exists():
                        path.write_bytes(content)
                    data = {**data, "file": f"{QA_IMG_MOUNT}/{filename}"}
                except Exception:
                    logger.warning("图片问答图片保存失败: %s", url[:80])
            seg.data = data
        segs.append(seg)
    return Message(segs)


def _parse_qa(arg: Message) -> tuple[str, Message | None, str | None]:
    """解析 /问答 的多行内容，返回 (问内容, 答内容消息, 范围标志)。

    范围标志取「问」之前的首个 -a / -t；缺省为 None（视为仅本群）。
    """
    q_lines: list[str] = []
    a_segs: list[MessageSegment] = []
    mode = None  # None / "q" / "a"
    scope = None
    for seg in arg:
        if seg.type == "text":
            for ln in seg.data.get("text", "").splitlines():
                s = ln.strip()
                if mode is None and s in ("-a", "-t"):
                    scope = s
                elif s == "问":
                    mode = "q"
                elif s == "答":
                    mode = "a"
                elif mode == "q" and s:
                    q_lines.append(s)
                elif mode == "a" and s:
                    a_segs.append(MessageSegment.text(s))
        elif seg.type == "image" and mode == "a":
            a_segs.append(seg)  # 答案里的图片原样保留
    q = "\n".join(q_lines).strip()
    return q, (Message(a_segs) if a_segs else None), scope


def _msg_to_dicts(msg: Message) -> list[dict]:
    """把消息序列化成可存 JSON 的段列表。"""
    return [{"type": seg.type, "data": seg.data} for seg in msg]


def _answer_message(a) -> Message:
    """把存储的答案还原成消息（兼容纯文本与图文列表）。

    旧条目图片 file 为纯文件名时，若本地问答图片目录存在同名文件，
    补全为容器可访问路径，避免因 file 无路径导致发送失败（“答案失效”）。
    """
    if isinstance(a, str):
        return Message(a)
    segs = []
    for d in a:
        if d.get("type") == "image":
            data = dict(d.get("data") or {})
            f = str(data.get("file") or "")
            if f and "/" not in f and not f.lower().startswith(("http:", "https:")):
                try:
                    if (QA_IMG_DIR / f).exists():
                        data = {**data, "file": f"{QA_IMG_MOUNT}/{f}"}
                except Exception:
                    pass
            d = {**d, "data": data}
        segs.append(MessageSegment(d.get("type", "text"), d.get("data") or {}))
    return Message(segs)


# ---------- /忘记 ----------
forget_cmd = on_command("忘记", priority=1, block=True)
register_help("/忘记", "忘记一问一答（仅管理员）")
hide_help("/忘记")


@forget_cmd.handle()
async def forget_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not is_op(event.user_id):
        await forget_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    q = arg.extract_plain_text().strip()
    if not q:
        await forget_cmd.finish("格式：\n/忘记\n（问内容）", at_sender=True)
    pairs = _qa_pairs()
    remaining = [p for p in pairs if p.get("q") != q]
    removed = len(pairs) - len(remaining)
    if removed == 0:
        await forget_cmd.finish("没有找到这个问答，可能早就忘掉了。", at_sender=True)
    _save_qa(remaining)
    await forget_cmd.finish(f"已忘记 {removed} 条关于「{q}」的问答。", at_sender=True)


# ---------- 问答库监听：完全匹配时自动回复 ----------
def _qa_match_rule(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    if not text or text.startswith("/"):
        return False
    group_id = getattr(event, "group_id", 0)
    return bool(find_answers(text, group_id))


qa_monitor = on_message(rule=_qa_match_rule, priority=1, block=True)


@qa_monitor.handle()
async def qa_monitor_handler(bot: Bot, event: MessageEvent):
    text = event.get_plaintext().strip()
    group_id = getattr(event, "group_id", 0)
    answers = find_answers(text, group_id)
    if not answers:
        return
    # 逐条尝试发送：图片 URL 过期等发送失败时自动换下一条，避免静默无响应
    for answer in answers:
        try:
            await qa_monitor.send(_answer_message(answer))
            return
        except Exception:
            logger.warning("问答回复发送失败，尝试下一条: q=%s", text)
    await qa_monitor.send("这条问答的答案好像失效了，让管理员重新 /问答 录一次吧～")


# ---------- 引用 + 「问」→ 问答库快捷触发 ----------
def _reply_id_of(event: MessageEvent) -> int | None:
    for seg in [*event.message, *getattr(event, "original_message", [])]:
        if seg.type == "reply":
            try:
                return int(seg.data.get("id") or 0) or None
            except Exception:
                return None
    return None


def _host_path_of_file_ref(ref: str) -> Path | None:
    """把 NapCat 容器/本地图片 file 引用映射到宿主文件路径。"""
    if not ref:
        return None
    if ref.startswith("file://"):
        ref = ref[len("file://"):]
    if ref.startswith(QA_IMG_MOUNT):
        return QA_IMG_DIR / ref[len(QA_IMG_MOUNT):].lstrip("/")
    p = Path(ref)
    return p if p.exists() else None


def _plain_summary(segs: list) -> str:
    """从段列表提取纯文本摘要（图片/表情做占位），供录入预览用。"""
    parts: list[str] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if not isinstance(d, dict):
            d = {}
        if t == "text":
            s = str(d.get("text") or "").strip()
            if s:
                parts.append(s)
        elif t == "image":
            parts.append("[图片]")
        elif t == "face":
            parts.append("[表情]")
    return " ".join(parts).strip()


async def _expand_reply_body(bot: Bot, body) -> tuple[list[dict], str]:
    """把 get_msg 得到的被引用消息体展开为可存储的平面段列表与预览文本。

    - forward（合并转发）用 get_forward_msg 展开各节点内容（文本/图片等一并保留），
      读取失败时降级为文本占位，避免整条录入失效；
    - 其余段（text/image/face/at 等）原样保留，供后续持久化与回放。
    """
    if isinstance(body, str):
        body = [{"type": "text", "data": {"text": body}}]
    if not isinstance(body, list):
        body = [{"type": "text", "data": {"text": str(body)}}]
    flat: list[dict] = []
    for seg in body:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "forward":
            flat.append(seg)
            continue
        fid = str((seg.get("data") or {}).get("id") or "")
        if not fid:
            flat.append({"type": "text", "data": {"text": "[合并转发]"}})
            continue
        try:
            fm = await bot.call_api("get_forward_msg", id=fid)
            for node in fm.get("messages") or []:
                nm = node.get("message")
                if isinstance(nm, str):
                    nm = [{"type": "text", "data": {"text": nm}}]
                if isinstance(nm, list):
                    flat.extend(s for s in nm if isinstance(s, dict))
        except Exception:
            logger.warning("合并转发展开失败 id=%s", fid, exc_info=True)
            flat.append({"type": "text", "data": {"text": "[合并转发](暂无法展开，请改单条引用)"}})
    return flat, _plain_summary(flat)


def _wen_reply_query(event: MessageEvent) -> str | None:
    """引用消息 + 文本以「问」开头 → 返回剥离「问」前缀的问题内容；不满足返回 None。"""
    text = event.get_plaintext().strip()
    m = re.match(r"^问(?:[\s:：\n]*)([\s\S]*)$", text)
    if not m:
        return None
    if _reply_id_of(event) is None:
        return None
    return m.group(1).strip() or None


def _wen_qa_rule(event: MessageEvent) -> bool:
    return _wen_reply_query(event) is not None


wen_qa_cmd = on_message(rule=_wen_qa_rule, priority=0, block=False)


@wen_qa_cmd.handle()
async def wen_qa_handler(bot: Bot, event: MessageEvent):
    """引用某信息 + 「问↵内容」→ 相当于执行一次 /问答 录入：问=内容，答=被引用信息。"""
    query = _wen_reply_query(event)
    if not query:
        return
    rid = _reply_id_of(event)
    if not rid:
        return
    try:
        m = await bot.get_msg(message_id=rid)
    except Exception:
        await wen_qa_cmd.finish("取不到被引用的内容，稍后再试试？", at_sender=True)
    body = m.get("message")
    if not body:
        await wen_qa_cmd.finish("被引用的内容我读不到，换个引用再试试？", at_sender=True)
    # 展开合并转发、保留图片等段 → 答案 = 被引用的整条信息
    segs, summary = await _expand_reply_body(bot, body)
    if not segs:
        await wen_qa_cmd.finish("被引用的内容好像为空，换个引用再试试？", at_sender=True)
    a_msg = Message(
        MessageSegment(seg.get("type", "text"), seg.get("data") or {})
        for seg in segs
        if isinstance(seg, dict)
    )
    a_msg = await _persist_images(a_msg)
    scope_group = getattr(event, "group_id", 0)  # 本群；私聊为 0 视为全部群（与 /问答 一致）
    pairs = _qa_pairs()
    pairs.append({"q": query, "a": _msg_to_dicts(a_msg), "group_id": scope_group})
    _save_qa(pairs)
    where = "所有群" if scope_group == 0 else "本群"
    preview = (summary[:40] + "…" if len(summary) > 40 else summary) if summary else "[图片/转发等]"
    await wen_qa_cmd.finish(
        f"已记住（{where}触发）：\n问「{query}」\n答：{preview}",
        at_sender=True,
    )


# ---------- /ophelp ----------
ophelp_cmd = on_command("ophelp", priority=1, block=True)
register_help("/ophelp", "查看全部（含 OP）帮助（仅管理员）")
hide_help("/ophelp")


@ophelp_cmd.handle()
async def ophelp_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await ophelp_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    try:
        await send_forward_text(bot, event, _build_help_text(include_hidden=True), name="鲸娘·OP帮助")
    except Exception:
        await ophelp_cmd.finish(_build_help_text(include_hidden=True))
    await ophelp_cmd.finish()


# ---------- /白名单：本群功能白名单管理（仅 OP）----------
# 未纳入白名单管控的群默认全功能可用；某群首次启用任意功能后进入白名单管控，
# 未启用功能（含被动功能）在该群被静默拦截。
wl_cmd = on_command("白名单", priority=1, block=True)
register_help("/白名单", "查看/管理本群功能白名单（仅 OP）：无参=查看；/白名单 /功能=启用；末尾加 -o=移除")
hide_help("/白名单")

_CHAT_SYN = {"chat", "对话", "聊天", "ai"}


def _wl_canonical(tok: str) -> str | None:
    """把用户输入的功能名归一为主命令名或 WL_CHAT；未知功能返回 None。"""
    if tok in _CHAT_SYN:
        return WL_CHAT
    for forms in _collect_command_forms():
        names = [f[1:] for f in forms]
        if tok not in names:
            continue
        mains = [f[1:] for f in forms if f in HELP_DESC]
        return mains[0] if mains else names[0]
    return None


def _wl_forms_of(name: str) -> set[str]:
    """返回某功能（主名或 WL_CHAT）对应的全部可输入命令形式，供启用/移除时整组操作。

    白名单落盘与拦截匹配都基于“原始输入词”，因此启用一个功能须写入它全部的形式
    （主命令 + 别名），移除时同样整组移除，避免只认主名导致别名仍可绕过。
    """
    if name == WL_CHAT:
        return {WL_CHAT}
    out: set[str] = set()
    for forms in _collect_command_forms():
        mains = [f[1:] for f in forms if f in HELP_DESC]
        if mains and mains[0] == name:
            out.update(f[1:] for f in forms)
    return out or {name}


def _wl_line(name: str) -> str:
    if name == WL_CHAT:
        return "· chat 对话（@我 聊天）"
    desc = HELP_DESC.get("/" + name, "")
    return f"· /{name}" + (f"：{desc}" if desc else "")


def _wl_plain(name: str) -> str:
    return "对话（@我 聊天）" if name == WL_CHAT else f"/{name}"


@wl_cmd.handle()
async def whitelist_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not is_op(event.user_id):
        await wl_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    gid = getattr(event, "group_id", None)
    if not gid:
        await wl_cmd.finish("/白名单 只能用于群聊。", at_sender=True)

    toks = arg.extract_plain_text().split()
    remove = any(t in ("-o", "-移除") for t in toks)
    target = next((t for t in toks if t not in ("-o", "-移除")), None)

    if target is None:  # 无功能参数 → 查看本群白名单
        cur = wl_features(gid)
        if cur is None:
            await wl_cmd.finish(
                "本群目前未启用白名单管控，所有功能默认可用。\n"
                "用法：/白名单 /功能名 启用（如 /白名单 /生图），末尾加 -o 移除（如 /白名单 /生图 -o）。\n"
                "首次启用任意功能后，本群进入白名单管控：未启用的功能（含拍一拍、进群欢迎等）将被静默拦截。"
            )
        items = sorted({_wl_canonical(f) or f for f in cur})
        if not items:
            await wl_cmd.finish(
                "本群已进入白名单管控，但白名单为空（仅管理指令可用）。", at_sender=True
            )
        lines = "\n".join(_wl_line(f) for f in items)
        await wl_cmd.finish(
            f"本群白名单已启用，当前开放：\n{lines}\n"
            "（未列入本列表的功能在本群会被静默拦截）"
        )

    name = _wl_canonical(target.lstrip("/／!！").strip())
    if name is None:
        await wl_cmd.finish(
            "没认出来这个功能～ 先在 /help 里找到你想开放的功能名，再发 /白名单 /功能名。"
        )
    if name in WL_MANAGE_CMDS:
        await wl_cmd.finish(f"「{name}」是管理指令，无需白名单即可使用。")

    forms = _wl_forms_of(name)
    if remove:
        removed = sum(1 for f in forms if wl_disable(gid, f))
        if removed == 0:
            await wl_cmd.finish(f"「{_wl_plain(name)}」不在本群白名单中（或本群未启用白名单），无需移除。")
        await wl_cmd.finish(f"已将「{_wl_plain(name)}」移出本群白名单。")
    else:
        was_unmanaged = wl_features(gid) is None
        for f in forms:
            wl_enable(gid, f)
        note = (
            "\n本群现已进入白名单管控：未启用的功能会被静默拦截。"
            if was_unmanaged
            else ""
        )
        await wl_cmd.finish(f"已在本群启用：{_wl_plain(name)}{note}")
