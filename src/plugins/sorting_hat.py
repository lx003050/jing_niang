"""鲸娘 AI 智能体

在 .env 中设置 AI_ENABLED=true 并填写 OPENAI_API_KEY 后自动生效：
- 群聊中 @机器人 或回复机器人 -> 由 AI 以「鲸娘」人设回复
- 私聊机器人 -> 由 AI 回复
- 群聊上下文按「群」共享：自动记录群内所有成员的普通发言（带说话人昵称），
  同群的对话连成一段历史，AI 能分辨是谁在说话、接住群聊上下文；
  私聊仍按用户独立分段（/reset 可清空对应上下文）

不配置 API Key 时该插件自动失效，退回关键词/问答等无 AI 功能。
"""
import asyncio
import base64
import difflib
import json
import logging
import re
import shutil
import time
import uuid
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
from openai import AsyncOpenAI

from .common import (
    CAT_PERSONA,
    DATA_DIR,
    model_override_path,
    persona_path,
    persona_scope,
    ai_config,
    register_help,
    send_forward_text,
    wl_features,
)

logger = logging.getLogger("sorting_hat.ai")

SYSTEM_PROMPT_FILE = DATA_DIR / "system_prompt.md"

DEFAULT_SYSTEM_PROMPT = CAT_PERSONA  # 默认人格 = -cat 提供的鲸娘提示词

# -q 答题模式的专用提示词：优先保证解答正确、完整、有条理
QA_SYSTEM_PROMPT = (
    "你是「鲸娘」的学术答疑形态，此刻以严谨、条理清晰的解题导师身份作答。"
    "用户会发来题目文字或题目图片，请遵循：\n"
    "1. 先用自己的话复述题意，确保理解正确；\n"
    "2. 给出解题思路（为什么这么做）；\n"
    "3. 给出步骤清晰的完整解答过程；\n"
    "4. 最后单独点出答案。\n"
    "若图片模糊、题看不全或条件不足，如实说明缺什么，不要编造。"
    "可以保留一点鲸娘爱摸鱼又粘人的性子，但正确性和完整性永远优先。"
)

# 群聊额外语境说明（不覆盖用户自定义的 system_prompt.md，仅追加说明）
GROUP_CONTEXT_HINT = (
    "你在一个 QQ 群里。下面[昵称]开头的对话记录只是【背景信息】："
    "帮助你了解大家正聊到什么、谁说了什么，用来理解语境与接住话茬，"
    "绝不是让你模仿、套用或复刻任何群成员的说话方式、口吻、句式或梗。\n"
    "发言准则（按优先级）：\n"
    "① 你的一切发言必须严格遵循 system prompt 为你设定的人格、语气与规则，永远以 system prompt 为准；\n"
    "② 严禁模仿或学习聊天记录里任何成员的语言风格，不照搬、不戏仿任何人的话风与口头禅；\n"
    "③ 严禁把聊天记录原样粘贴、逐条复述或整体回显成你的回复——禁止输出任何以“[昵称]”开头的行或段落，"
    "禁止成段引用群成员的发言原文；\n"
    "④ 想提到别人的话时，只能用自己的话概括大意或评论观点，不照抄原句；\n"
    "⑤ 像普通群友一样简短、口语化地自然接话，能接住上下文即可，不要每次正式长篇回答；\n"
    "⑥ 每条背景记录前的[昵称(QQ号)]代表该消息的发言成员，不同的昵称是不同的独立的人："
    "请区分谁在对谁说话、话题由谁发起，回应时要对应到正确的人；"
    "若你想称呼对方，可用其昵称称呼，避免把不同成员的发言混成同一个人的话。"
)

_client: AsyncOpenAI | None = None
_vision_client: AsyncOpenAI | None = None
_memory: dict[str, list[dict]] = {}   # 会话 key -> 消息历史
_last_call: dict[int, float] = {}     # user_id -> 上次请求时间


def _load_system_prompt(event: MessageEvent | None = None) -> str:
    # 本群/私聊人格优先 > 管理员自定义 system_prompt.md > 默认人格
    if event is not None:
        try:
            t = persona_path(persona_scope(event)).read_text(encoding="utf-8").strip()
            if t:
                return t
        except Exception:
            pass
    try:
        t = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    return DEFAULT_SYSTEM_PROMPT


def _get_client() -> AsyncOpenAI | None:
    global _client
    if _client is not None:
        return _client
    if not ai_config.openai_api_key:
        return None
    _client = AsyncOpenAI(
        api_key=ai_config.openai_api_key,
        base_url=ai_config.openai_base_url or None,
        timeout=60.0,
    )
    return _client


def _get_vision_client() -> AsyncOpenAI | None:
    """识图专用客户端：走 ai_vision_base_url / ai_vision_api_key，未配置时回退主对话通道。"""
    global _vision_client
    if _vision_client is not None:
        return _vision_client
    key = ai_config.ai_vision_api_key or ai_config.openai_api_key
    base = ai_config.ai_vision_base_url or ai_config.openai_base_url
    if not key:
        return None
    _vision_client = AsyncOpenAI(
        api_key=key,
        base_url=base or None,
        timeout=60.0,
    )
    return _vision_client


def _session_key(event: MessageEvent) -> str:
    # 群聊按「群」共享上下文（所有成员共一段对话历史，可识别不同发言者）；
    # 私聊按用户分段，避免不同人的对话混在一起
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    return f"private_{event.user_id}"


# ---- 对话层防复读：防止 AI 一直重复同一句话（口头禅执行过头 / 模型复读） ----
_REPEAT_HIST = 3        # 与最近 N 条本机回复比对相似度
_REPEAT_EXACT_MIN = 4   # “原文相同”判定最短长度（去空白后），短填充词（嗯/是的/哈哈）不误伤
_REPEAT_RATIO = 0.85    # 与历史回复文本高度相似视为复读
_REPEAT_MIN_LEN = 8     # 参与相似度判定的最短长度（去空白后），短确认句不误伤
_REPEAT_FREQ = 3        # 同一句话在会话历史中累计出现次数（含既往），超过视为长期复读
_REPEAT_INTRA = 2       # 单条回复内同一完整句（>=5 字）允许出现次数

REPAIR_REPEAT_TMPL = (
    "下面是一句 AI 的聊天回复，它存在复读问题：\n{reply}\n\n"
    "问题：{reason}\n"
    "请把它改写成同样语气但说法全新的一句话：\n"
    "- 不要出现原回复里已有的任何完整句子或长片段；\n"
    "- 保持原意与说话风格（标点、口头禅可用一次或不用），但换上完全不同的措辞；\n"
    "- 直接输出改写后的回复正文，不要任何解释、引号或编号。"
)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _repeat_score(a: str, b: str) -> float:
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _hist_repeat_desc(reply: str, history: list[dict]) -> str | None:
    """与最近历史回复比较：原文相同或高度相似则返回复读原因，否则 None。"""
    rn = _norm_text(reply)
    if not rn:
        return None
    prev = [str(m.get("content") or "") for m in history if m.get("role") == "assistant"]
    prev = [p for p in prev if _norm_text(p)][-_REPEAT_HIST:]
    for p in reversed(prev):
        pn = _norm_text(p)
        if rn == pn and len(rn) >= _REPEAT_EXACT_MIN:
            return f"与最近回复原文相同：「{p[:30]}」"
        if len(rn) >= _REPEAT_MIN_LEN and len(pn) >= _REPEAT_MIN_LEN:
            r = _repeat_score(reply, p)
            if r >= _REPEAT_RATIO:
                return f"与最近回复高度相似（{r:.0%}）：「{p[:30]}」"
    return None


def _freq_repeat_desc(reply: str, history: list[dict]) -> str | None:
    """长期频率检查：同一句话在整段历史里反复出现（隔几条又复读）也拦截。"""
    rn = _norm_text(reply)
    if len(rn) < _REPEAT_MIN_LEN:
        return None
    cnt = sum(
        1
        for m in history
        if m.get("role") == "assistant" and _norm_text(str(m.get("content") or "")) == rn
    )
    if cnt >= _REPEAT_FREQ:
        return f"这句话在最近 {len(history)} 条记录里已说过 {cnt} 次"
    return None


def _intra_repeat_desc(reply: str) -> str | None:
    """单条回复内部复读：同一完整句（去空白 >=5 字）出现 _REPEAT_INTRA 次以上。"""
    parts = [p.strip() for p in re.split(r"[。！？!?…~]+", reply)]
    seen: dict[str, int] = {}
    for p in parts:
        pn = _norm_text(p)
        if len(pn) < 5:
            continue
        seen[pn] = seen.get(pn, 0) + 1
    for pn, c in seen.items():
        if c >= _REPEAT_INTRA:
            return f"回复内部同一句「{pn[:30]}」出现了 {c} 次"
    return None


def _repeat_desc(reply: str, history: list[dict]) -> str | None:
    """汇总三重复读检查（历史相同/相似、长期频率、单条内部），返回原因或 None。"""
    for fn in (_hist_repeat_desc, _freq_repeat_desc):
        reason = fn(reply, history)
        if reason:
            return reason
    return _intra_repeat_desc(reply)


# ---- 发言断句：按换行把一条长回复拆成多条依次发送，更接近真人一句一句发；每条保证括号成对 ----
_CHUNK_MAX_MSGS = 8   # 一次回复最多拆几条，超过则整条合并发送，避免刷屏


def _chunk_lines(reply: str) -> list[str] | None:
    """把回复按换行拆成多条（过滤空白段）；不满足拆分条件（无换行 / 段数超上限）返回 None。"""
    lines = [ln.strip() for ln in reply.split("\n") if ln.strip()]
    if len(lines) < 2 or len(lines) > _CHUNK_MAX_MSGS:
        return None
    return lines


def _balance_parens(text: str) -> str:
    """让分段文本的括号成对：段首多出的右括号前补左括号，段尾未闭合补右括号。

    例如 "（我喜欢你" -> "（我喜欢你）"，"骗你的）" -> "（骗你的）"。
    全角/半角各按自己的配对补齐，不互相混用。
    """
    for o, c in (("（", "）"), ("(", ")")):
        delta = text.count(o) - text.count(c)
        if delta > 0:
            text += c * delta
        elif delta < 0:
            text = o * (-delta) + text
    return text


async def _send_chunked(bot: Bot, event: MessageEvent, reply: str, at_sender: bool = True) -> None:
    """断句发送：优先按行拆分逐条发送；不满足拆分条件时整条原样发送。"""
    lines = _chunk_lines(reply)
    if lines is None:
        await bot.send(event, reply, at_sender=at_sender)
        return
    for i, seg in enumerate(lines):
        seg = _balance_parens(seg)
        try:
            await bot.send(event, seg, at_sender=at_sender and i == 0)
        except Exception:
            logger.warning("断句发送第 %d 条失败: %r", i + 1, seg[:40])


# 群成员昵称缓存: (group_id, user_id) -> (昵称, 时间)
_nick_cache: dict[tuple[int, int], tuple[str, float]] = {}


async def _get_nick(bot: Bot, group_id: int, uid: int) -> str:
    """取群内昵称（群名片优先），失败回退 QQ 号；缓存 10 分钟。"""
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


def _enabled_rule(event: MessageEvent) -> bool:
    # 仅在 AI 开启且有 Key 时接管对话
    if not (ai_config.ai_enabled and bool(ai_config.openai_api_key)):
        logger.warning("AI规则拒绝: AI未启用或缺少API Key")
        return False
    text = event.get_plaintext().lstrip()
    # 答题关键词 / 引用+「问」前缀优先判定（无需 @ 或引用解析）
    qa_hit = _detect_qa_mode(text)
    if qa_hit:
        logger.warning("AI规则通过(答题关键词): %r", text[:40])
        return True
    # 指令消息（/xxx）交给命令处理器，AI 不接管
    if text.startswith(("/", "！", "!")):
        logger.warning("AI规则拒绝: 指令前缀 text=%r", text[:40])
        return False
    # 触发方式：
    #  a) 被 @ / 私聊 / 回复机器人（to_me）
    if event.to_me:
        logger.warning("AI规则通过(to_me): %r", text[:40])
        return True
    logger.warning("AI规则判定: to_me=False qa=%s text=%r", bool(qa_hit), text[:40])
    return False


# ---------- 图片与引用消息处理 ----------
# 答题模式触发关键词：出现即视为要"回答问题"。用明确意图词，避免日常对话误触发。
_QA_KEYS = ("/回答", "回答我", "帮我回答", "题目", "做题", "答题", "求解")
_QA_FLAG_RE = re.compile(r"(^|\s)-{1,2}[qQ](\s|$)")


def _detect_qa_mode(text: str) -> bool:
    """检测答题模式（解题通路，当前已停用：恒返回 False；恢复时恢复下方原逻辑）。"""
    return False


def _reply_id(event: MessageEvent) -> int | None:
    """提取被引用消息的 message_id。优先用 original_message（保留完整引用段），再回退当前 message。"""
    for msg in (getattr(event, "original_message", None), event.message):
        if msg is None:
            continue
        raw = str(msg)
        m = re.search(r"CQ:reply,id=(\d+)", raw)
        if not m:
            m = re.search(r"\[reply:id=(\d+)\]", raw)
        if m:
            return int(m.group(1))
    return None


def _extract_images(msg: object) -> list[tuple[str, str]]:
    """从 OneBot 消息中提取所有图片段，返回 (url, file) 列表（无则空串）。"""
    segs: list[object] = []
    if isinstance(msg, Message):
        segs = list(msg)
    elif isinstance(msg, str) and msg.strip():
        try:
            segs = list(Message(msg))
        except Exception:
            segs = []
    elif isinstance(msg, list):
        parts: list[MessageSegment] = []
        for m in msg:
            if isinstance(m, dict):
                data = m.get("data") or {}
                if not isinstance(data, dict):
                    data = {}
                parts.append(MessageSegment(type=m.get("type", "") or "", data=data))
            elif isinstance(m, MessageSegment):
                parts.append(m)
        segs = parts
    pairs: list[tuple[str, str]] = []
    for seg in segs:
        if getattr(seg, "type", "") != "image":
            continue
        data = getattr(seg, "data", {}) or {}
        pairs.append((str(data.get("url") or "").strip(), str(data.get("file") or "").strip()))
    return pairs


def _plain_text_of(msg: object) -> str:
    """从 OneBot 消息中提取纯文本。"""
    if isinstance(msg, Message):
        return msg.extract_plain_text()
    if isinstance(msg, str):
        try:
            return Message(msg).extract_plain_text()
        except Exception:
            return msg
    if isinstance(msg, list):
        buf: list[str] = []
        for m in msg:
            if isinstance(m, dict) and m.get("type") == "text":
                buf.append(str((m.get("data") or {}).get("text", "")))
        return "".join(buf)
    return ""


# NapCat 容器内图片目录 <-> 宿主机图片目录（机器人自己发过的图片 ref 是容器路径）
_IMG_MOUNTS = {
    "/app/napcat/qa_images": "/srv/sorting_hat/data/qa_images",
    "/app/napcat/cards": "/srv/sorting_hat/data/cards",
}


def _map_container_path(path: str) -> str | None:
    """把容器内路径映射为宿主机可读路径；映射不了返回 None。"""
    norm = path.replace("\\", "/")
    for cpt, host in _IMG_MOUNTS.items():
        if norm == cpt:
            return host
        if norm.startswith(cpt + "/"):
            return host + norm[len(cpt):]
    return None


async def _read_local_image(path: str) -> str | None:
    """读取本地图片文件转 base64 data URI。"""
    try:
        data = Path(path).read_bytes()
        if not data:
            return None
        ext = Path(path).suffix.lower()
        ctype = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }.get(ext, "image/jpeg")
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except Exception:
        return None


async def _download_image(url: str) -> str | None:
    """下载网络图片并转为 base64 data URI。失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        if not resp.content:
            return None
        ctype = (resp.headers.get("content-type", "") or "").split(";")[0].strip().lower()
        if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"):
            ctype = "image/jpeg"
        b64 = base64.b64encode(resp.content).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except Exception:
        return None


async def _resolve_image(ref: str, bot: Bot | None = None) -> str | None:
    """把图片引用（http URL / file:/// 容器路径 / 本地路径 / 缓存名）解析为 base64 data URI。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://")):
        uri = await _download_image(ref)
        if uri:
            return uri
        logger.warning("图片下载失败: %s", ref[:120])
        return None
    if ref.startswith("file:///"):
        host = _map_container_path(ref[len("file://"):])
    else:
        host = ref
    if host:
        uri = await _read_local_image(host)
        if uri:
            return uri
    # 裸缓存文件名（如 xxx.image）：尝试 NapCat get_image 换取可下载 URL
    if bot is not None and "/" not in ref and not ref.startswith("file"):
        try:
            ret = await bot.get_image(file=ref)
            url = str((ret or {}).get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                uri = await _download_image(url)
                if uri:
                    return uri
        except Exception:
            pass
    logger.warning("本地图片读取失败: %s", ref[:120])
    return None


async def _get_reply_info(bot: Bot, event: MessageEvent) -> dict | None:
    """若消息引用了某条消息，拉取被引用消息的发送者、文本与图片；合并转发会展开节点内容。"""
    reply_id = _reply_id(event)
    logger.warning(
        "引用诊断: 解析id=%s 当前=%r 原始=%r",
        reply_id,
        str(event.message)[:80],
        str(getattr(event, "original_message", ""))[:80],
    )
    if not reply_id:
        return None
    try:
        msg = await bot.get_msg(message_id=reply_id)
    except Exception:
        return None
    body = msg.get("message")
    sender = msg.get("sender") or {}
    text = _plain_text_of(body).strip()
    images = _extract_images(body)

    # 被引用的是合并转发：展开各节点文本与图片作为题干
    fwd = re.search(r"CQ:forward,id=([A-Za-z0-9_\-]+)", str(body))
    if fwd:
        try:
            ret = await bot.get_forward_msg(id=fwd.group(1))
            parts: list[str] = []
            for node in ret.get("messages") or []:
                n_body = node.get("message")
                n_sender = node.get("sender") or {}
                n_name = str(n_sender.get("nickname") or n_sender.get("card") or "发言人")
                n_text = _plain_text_of(n_body).strip()
                images += _extract_images(n_body)
                if n_text:
                    parts.append(f"{n_name}: {n_text}")
            if parts:
                text = (text + "\n" if text else "") + "\n".join(parts)
        except Exception:
            logger.warning("合并转发解析失败", exc_info=True)

    return {
        "sender": str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "未知"),
        "text": text,
        "images": images,
    }


def _build_content(text: str, image_uris: list[str]) -> list[dict]:
    """构造 OpenAI 兼容的 content 数组（文字 + 图片 data URI）。"""
    parts: list[dict] = []
    for uri in image_uris:
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    parts.append({"type": "text", "text": text})
    return parts


async def _send_placeholder(bot: Bot, event: MessageEvent, qa: bool = False) -> int | None:
    """先发送占位提示消息，返回 message_id；失败返回 None。答题模式用"我记住了"确认语。"""
    msg_text = "📝 我记住了，正在努力解题……" if qa else "✨ 少女祈祷中……"
    try:
        if isinstance(event, GroupMessageEvent):
            sent = await bot.send_group_msg(group_id=event.group_id, message=msg_text)
        else:
            sent = await bot.send_private_msg(user_id=event.user_id, message=msg_text)
        mid = int((sent or {}).get("message_id") or 0)
        return mid or None
    except Exception:
        return None


async def _recall_placeholder(bot: Bot, message_id: int | None) -> None:
    """撤回占位提示消息（QQ 群消息 2 分钟内可撤；失败静默）。"""
    if not message_id:
        return
    try:
        await bot.delete_msg(message_id=message_id)
    except Exception:
        pass


# ---------- LaTeX/Markdown 渲染为图片并按合并转发发送 ----------
RENDER_JS = "/srv/render/render.js"                 # node 渲染脚本
RENDER_TMP_DIR = Path("/srv/render")                # 渲染输出目录
QA_IMG_HOST_DIR = Path("/srv/sorting_hat/data/qa_images")  # 宿主机图片目录
QA_IMG_CONTAINER = "/app/napcat/qa_images"          # NapCat 容器内挂载路径

_FORMULA_RE = re.compile(r"[\\$]")  # 含反斜杠或美元即视为含 LaTeX/Markdown 公式


async def _render_to_png(md_text: str) -> Path | None:
    """调用 render.js 把 Markdown+LaTeX 渲染成 PNG 长图，返回路径；失败返回 None。"""
    ts = time.strftime("%Y%m%d%H%M%S")
    tag = f"{ts}_{uuid.uuid4().hex[:8]}"
    in_file = Path(f"/tmp/qa_render_{tag}.md")
    out_prefix = str(RENDER_TMP_DIR / f"qr_{tag}")
    in_file.write_text(md_text, encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", RENDER_JS, str(in_file), out_prefix,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("渲染超时")
            return None
        png = Path(out_prefix + ".png")
        if proc.returncode != 0 or not png.exists():
            logger.warning("渲染失败 rc=%s err=%s", proc.returncode, (err or b"")[:300])
            return None
        return png
    except Exception as e:
        logger.warning("渲染异常: %s", e)
        return None
    finally:
        try:
            in_file.unlink()
        except Exception:
            pass


async def _send_forward_images(
    bot: Bot, event: MessageEvent, png_paths: list[Path], name: str = "鲸娘"
) -> bool:
    """把图片复制到 NapCat 挂载目录，以合并转发卡片发送。"""
    copies: list[Path] = []
    try:
        for p in png_paths:
            dst = QA_IMG_HOST_DIR / p.name
            shutil.copyfile(p, dst)
            copies.append(dst)
        nodes = [
            {
                "type": "node",
                "data": {
                    "name": name,
                    "uin": int(bot.self_id),
                    "content": [
                        {"type": "image", "data": {"file": f"{QA_IMG_CONTAINER}/{d.name}"}}
                    ],
                },
            }
            for d in copies
        ]
        if isinstance(event, GroupMessageEvent):
            await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
        return True
    except Exception as e:
        logger.warning("合并转发发送失败: %s", e)
        for c in copies:
            try:
                c.unlink()
            except Exception:
                pass
        return False


ai_matcher = on_message(rule=_enabled_rule, priority=1, block=True)


# ---------- 群聊上下文记录：AI 开启时记录群内普通发言（带昵称），供 AI 接话 ----------
def _group_log_rule(event: MessageEvent) -> bool:
    if not (ai_config.ai_enabled and bool(ai_config.openai_api_key)):
        return False
    if not isinstance(event, GroupMessageEvent):
        return False
    text = event.get_plaintext().strip()
    if not text:
        return False
    return not text.startswith(("/", "！", "!"))


group_logger = on_message(rule=_group_log_rule, priority=20, block=False)


@group_logger.handle()
async def group_log_handler(bot: Bot, event: GroupMessageEvent):
    # 机器人自己发的消息（AI/关键词/问答回复等）不记录，避免污染 AI 上下文
    if event.user_id == int(bot.self_id):
        return
    text = event.get_plaintext().strip()
    if not text:
        return
    key = f"group_{event.group_id}"
    history = _memory.setdefault(key, [])
    nick = await _get_nick(bot, event.group_id, event.user_id)
    history.append({"role": "user", "content": f"[{nick}({event.user_id})] {text}"})
    if len(history) > ai_config.ai_max_history:
        _memory[key] = history[-ai_config.ai_max_history :]


# AI 可调用的工具：自主把违规者关进阿兹卡班 + 按需代发群内已开放的功能
_AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_to_azkaban",
            "description": "把指定 QQ 号的用户关进阿兹卡班服刑。仅在该用户确实严重违规（辱骂、刷屏、骚扰、攻击他人）时调用，不要因为开玩笑或普通聊天就调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "integer", "description": "要关押的用户 QQ 号"},
                    "days": {"type": "integer", "description": "服刑天数，1~30"},
                    "reason": {"type": "string", "description": "判刑理由（简短）"},
                },
                "required": ["target_qq", "days", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "invoke_feature",
            "description": (
                "在用户明确表示想使用某个功能时，代发/执行该功能。只有这些功能可代发："
                "help（用户问你能做什么/要功能帮助时）；"
                "生图（用户说想画画/生成图片/做张图等，args 填用户想要的画面描述）；"
                "伊蕾娜（用户想抽一张伊蕾娜图时）；"
                "邦多利（用户想抽一张邦多利卡图时）。"
                "功能是否有权限受当前群白名单约束，未开放的会被拒绝，无需自行编造其它功能。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feature": {"type": "string", "description": "要代发的功能：help / 生图 / 伊蕾娜 / 邦多利"},
                    "args": {"type": "string", "description": "传给该功能的参数。生图时为用户想画的画面描述；其余可为空"},
                    "mode": {
                        "type": "string",
                        "enum": ["img2img", "t2i"],
                        "description": (
                            "仅在 feature=生图 时选择生成方式："
                            "用户引用/发送了图片并要求以它为底图修改、重绘、换风格 → img2img（会以该图为底图生成）；"
                            "若引用的图片只是聊天上下文/风格举例，用户是想要按文字描述新画一张 → t2i。省略默认 t2i。"
                        ),
                    },
                },
                "required": ["feature"],
            },
        },
    },
]


# AI 代发功能支持表：功能主名 -> 可用的白名单命令名（任一命中即视为该功能已开放）
_FEATURE_CMDS = {
    "help": {"help", "帮助"},
    "生图": {"生图", "文生图", "图生图", "生成图片"},
    "伊蕾娜": {"伊蕾娜"},
    "邦多利": {"邦多利"},
}
_FEATURE_SYN = {
    "help": {"help", "帮助"},
    "生图": {"生图", "文生图", "图生图", "生成图片", "画图", "画画", "图片生成"},
    "伊蕾娜": {"伊蕾娜"},
    "邦多利": {"邦多利"},
}


def _norm_feature(feature: str) -> str | None:
    """把模型给出的 feature 归一为主名；不认识返回 None。"""
    key = (feature or "").strip().lower()
    for main, syns in _FEATURE_SYN.items():
        if key in syns:
            return main
    return None


def _feature_allowed(group_id: int, main: str) -> bool:
    """功能是否在本群开放：群未纳入白名单管控则默认放行。"""
    feats = wl_features(group_id)
    if feats is None:
        return True
    return bool(feats & _FEATURE_CMDS[main])


async def _ai_invoke_feature(
    bot: Bot, event: MessageEvent, main: str, args: str, mode: str = ""
) -> str:
    """执行 AI 代发的功能（受当前群白名单约束），返回给 AI 的结果说明。"""
    group_id = getattr(event, "group_id", 0)
    if not _feature_allowed(group_id, main):
        return f"「{main}」功能当前群没有开放（白名单外），我不能代发哦。"
    try:
        if main == "help":
            from .events import _build_help_text
            text = _build_help_text()
            try:
                await send_forward_text(bot, event, text, name="鲸娘·帮助")
            except Exception:
                await bot.send(event, text)
            return "功能清单已经发过去啦，让 TA 在里面挑想要的吧～"
        if main == "生图":
            from .image_gen import _reply_ref_images, _save_ref_image, ai_image_file
            prompt = (args or "").strip()
            if not prompt:
                return "代发生图需要先知道要画什么，请让用户补一句画面描述。"
            ref: Path | None = None
            if (mode or "").strip().lower() == "img2img":
                refs = await _reply_ref_images(bot, event)
                if refs:
                    ref = await _save_ref_image(refs[0])
                if ref is None:
                    return "你想基于某张图片做图生图，但我没有拿到被引用的图片，请让用户引用/发一张图后再试一次。"
            path = await ai_image_file(prompt, ref)
            if path is None:
                return "生图请求失败了（上游繁忙或 Key 未配置），没能出图。"
            await bot.send(event, MessageSegment.image(file=f"{QA_IMG_CONTAINER}/{path.name}"))
            return "画好啦，图片已经发出来了～"
        if main == "伊蕾娜":
            from .irena import pick_irena_path
            path = await pick_irena_path(False)
            if path is None:
                return "伊蕾娜图库还是空的，暂时抽不了。"
            await bot.send(event, MessageSegment.image(file=f"{QA_IMG_CONTAINER}/{path.name}"))
            return "伊蕾娜酱已经发出来了～"
        if main == "邦多利":
            from .bandori import fetch_bandori_card
            path = await fetch_bandori_card()
            if path is None:
                return "邦多利图站暂时拿不到图，没能抽到卡。"
            await bot.send(event, MessageSegment.image(file=f"{QA_IMG_CONTAINER}/bandori/{path.name}"))
            return "邦多利卡图来啦，已经发出来了～"
    except Exception as e:
        logger.warning("AI 代发功能失败 feature=%s: %s", main, e)
        return "代发功能时出了点问题，没能成功……"
    return f"「{main}」执行完成。"


def _extract_at(event: MessageEvent) -> int | None:
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq and qq != "all":
                return int(qq)
    return None


@ai_matcher.handle()
async def ai_handler(bot: Bot, event: MessageEvent):
    client = _get_client()
    if client is None:
        await ai_matcher.finish("AI 功能未配置 API Key，请检查 .env 设置。")

    user_id = event.user_id
    now = time.time()
    if now - _last_call.get(user_id, 0) < ai_config.ai_cooldown:
        await ai_matcher.finish("别急嘛，让我先捋捋帽檐……（稍等一下下再问）")
    _last_call[user_id] = now

    raw_text = event.get_plaintext().strip()
    if not raw_text:
        await ai_matcher.stop_propagation()
        return

    qa_mode = _detect_qa_mode(raw_text)
    text = re.sub(r"(^|\s)-{1,2}[qQ](\s|$)", " ", raw_text).strip()

    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    caller = f"group {group_id}" if group_id else f"private {user_id}"
    logger.warning("AI 收到请求: %s | 原文=%r | qa模式=%s", caller, raw_text[:60], qa_mode)
    at_target = _extract_at(event)
    # 给 AI 说话人上下文，便于其判断是否对某人判刑
    speaker = f"[当前说话人QQ:{user_id}"
    if at_target:
        speaker += f"，被@目标QQ:{at_target}"
    speaker += "]"
    # 群聊历史/当前消息统一带 [昵称(QQ号)] 前缀，私聊保持纯文本
    if isinstance(event, GroupMessageEvent):
        nick = await _get_nick(bot, event.group_id, user_id)
        hist_user = f"[{nick}({user_id})] {text}"
    else:
        nick = str(user_id)
        hist_user = text

    # 引用消息：拉取被引用内容的文本与图片，让 AI 围绕它回答而非群内最新消息
    reply_info = await _get_reply_info(bot, event)
    cur_images = _extract_images(event.message)
    # original_message 保留完整引用/图片段，合并进来避免当前消息图片段丢失
    cur_images += _extract_images(getattr(event, "original_message", None))
    image_pairs = list(cur_images) + (list(reply_info["images"]) if reply_info else [])

    # 解析图片为 base64（最多 2 张），依次尝试 url -> file 缓存名
    image_uris: list[str] = []
    seen: set[str] = set()
    for img_url, img_file in image_pairs[:4]:
        key = img_url or img_file
        if key in seen:
            continue
        seen.add(key)
        for ref in (img_url, img_file):
            if not ref:
                continue
            uri = await _resolve_image(ref, bot)
            if uri:
                if uri not in image_uris:
                    image_uris.append(uri)
                break
        if len(image_uris) >= 2:
            break

    # 模型选择：带图 -> 视觉模型；纯文本 -q -> 答题模型；群级 grok 覆盖 -> grok；否则用对话模型
    use_vision = bool(image_uris)
    override = None
    if not use_vision and not qa_mode:
        try:
            _ov = model_override_path(persona_scope(event)).read_text(encoding="utf-8").strip()
            if _ov:
                override = _ov
        except Exception:
            pass
    if use_vision:
        model = ai_config.ai_vision_model or ai_config.ai_model
        if ai_config.ai_vision_api_key or ai_config.ai_vision_base_url:
            client = _get_vision_client() or client
    elif qa_mode:
        model = ai_config.ai_qa_model or ai_config.ai_model
    elif override:
        model = override
    else:
        model = ai_config.ai_model
    if override:
        if not ai_config.grok_api_key:
            await ai_matcher.finish("grok 对话模型未配置 Key，请管理员在 .env 配置 GROK_API_KEY。")
        client = AsyncOpenAI(api_key=ai_config.grok_api_key, base_url=ai_config.grok_base_url or None, timeout=60.0)
    logger.warning(
        "AI 参数: 引用=%s 图=%d/%d 视觉模型=%s 模型=%s 覆盖=%s",
        bool(reply_info), len(image_uris), len(image_pairs), use_vision, model, override or "",
    )

    key = _session_key(event)
    history = _memory.setdefault(key, [])

    # 被引用消息的说明拼进用户消息，避免 AI 误接群内最新一条
    if reply_info:
        q_text = reply_info["text"] or "[图片]"
        quoted = f"\n（用户正在引用/回复 {reply_info['sender']} 的消息：{q_text}，请主要围绕这条被引用的消息回答）"
    else:
        quoted = ""
    user_text = f"{speaker} {hist_user}{quoted}"
    if qa_mode:
        user_text += "\n（答题模式：请给出完整的解题思路、分步解答过程与最终答案）"

    messages: list[dict] = [
        {"role": "system", "content": QA_SYSTEM_PROMPT if qa_mode else _load_system_prompt(event)}
    ]
    if isinstance(event, GroupMessageEvent):
        messages.append({"role": "system", "content": GROUP_CONTEXT_HINT})
    messages += history
    messages.append(
        {
            "role": "user",
            "content": _build_content(user_text, image_uris) if image_uris else user_text,
        }
    )
    kwargs: dict = dict(model=model, messages=messages, temperature=0.5 if qa_mode else 0.8)
    if not use_vision:  # 视觉模型不一定支持 function calling
        kwargs["tools"] = _AI_TOOLS
    # 先发占位提示，避免用户干等；完整回答后撤回
    placeholder_id = await _send_placeholder(bot, event, qa=qa_mode)
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception:
        # 某些模型不支持 tools，去掉 tools 重试一次
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=0.5 if qa_mode else 0.8
            )
        except Exception as e2:
            logger.exception("AI 请求失败: %s", e2)
            await _recall_placeholder(bot, placeholder_id)
            await ai_matcher.finish("啊，我的魔法好像出了点问题（网络或 API 异常），稍后再试试吧～")

    msg = resp.choices[0].message
    reply = (msg.content or "").strip()
    if not reply:
        # 推理模型（如 DeepSeek-V4）的回答可能落在 reasoning_content，取其作为兜底回复
        extra = getattr(msg, "model_extra", None) or {}
        reply = str(extra.get("reasoning_content") or getattr(msg, "reasoning_content", "") or "").strip()

    # 处理工具调用：AI 自主判刑 / 代发已开放功能（仅对话模式启用过 tools）
    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            if tc.function.name == "send_to_azkaban":
                target = int(args.get("target_qq") or 0)
                days = int(args.get("days") or 1)
                reason = str(args.get("reason") or "")
                logger.info("AI 触发判刑: target=%s days=%s reason=%s", target, days, reason)
                from .sorting_system import sentence_user

                ok, info = await sentence_user(target, days, group_id)
                if not ok:
                    reply = (reply + "\n" if reply else "") + f"（{info}）"
                elif not reply:
                    reply = info
            elif tc.function.name == "invoke_feature":
                feature = _norm_feature(str(args.get("feature") or ""))
                if feature is None:
                    continue
                note = await _ai_invoke_feature(
                    bot, event, feature, str(args.get("args") or ""),
                    str(args.get("mode") or ""),
                )
                logger.info("AI 代发功能: feature=%s mode=%s note=%s", feature, args.get("mode"), note)
                reply = (reply + "\n" if reply else "") + note

    if not reply:
        reply = "嗯哼……我的魔法书翻了半天也没找到合适的话，再换个问法试试？"

    # 对话层防复读：检出与历史/内部重复时，按原风格自动改写一次；仍复读则放行防死循环。
    # 答题模式 -q 跳过（解题正确性优先，不干预长推导中的必要复述）
    if not qa_mode:
        repeat_reason = _repeat_desc(reply, history)
        if repeat_reason:
            logger.warning("AI 复读拦截: %s", repeat_reason)
            try:
                rw = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": REPAIR_REPEAT_TMPL.format(reply=reply, reason=repeat_reason)}],
                    temperature=0.9,
                )
                fixed = (rw.choices[0].message.content or "").strip()
            except Exception:
                fixed = ""
            if fixed and not _repeat_desc(fixed, history):
                reply = fixed
                logger.warning("AI 复读已改写: %r", reply[:40])
            else:
                logger.warning("AI 复读改写未通过，放行原回复（避免死循环）")

    # 记录上下文（群聊存带昵称的原文，私聊存原文；图片以文本标记代替避免体积膨胀）
    history.append({"role": "user", "content": hist_user + (" [图片]" if cur_images else "")})
    history.append({"role": "assistant", "content": reply})
    if len(history) > ai_config.ai_max_history:
        _memory[key] = history[-ai_config.ai_max_history :]
    if len(_memory) > 300:  # 全局上限，防止长时间运行内存膨胀
        _memory.clear()

    logger.warning("AI 回复完成: 长度=%d 预览=%r", len(reply), reply[:40])
    # 撤回占位提示，然后正式回复
    await _recall_placeholder(bot, placeholder_id)

    # -q 答题模式：一律渲染成图片并以合并转发发送（避免公式无法显示与长文截断）
    if qa_mode:
        png = await _render_to_png(reply)
        if png is not None:
            if await _send_forward_images(bot, event, [png]):
                logger.warning("已渲染并合并转发解答图片: %s", png.name)
                try:
                    png.unlink()
                    (RENDER_TMP_DIR / (png.stem + ".pdf")).unlink()
                    (RENDER_TMP_DIR / (png.stem + ".html")).unlink()
                except Exception:
                    pass
                return
        logger.warning("渲染或合并转发失败，回退为文本发送")
    # 断句发送：回复含换行时拆成多条依次发送（每条括号成对），仅 @ 第一条
    await _send_chunked(bot, event, reply)


# ---------- /reset 重置对话记忆 ----------
reset_cmd = on_command("reset", aliases={"重置", "清除记忆"}, priority=1, block=True)
register_help("/reset", "重置对话记忆")


@reset_cmd.handle()
async def reset_handler(event: MessageEvent):
    key = _session_key(event)
    _memory.pop(key, None)
    await reset_cmd.finish("好的，我已经忘记之前说过的话，重新开始吧～")
