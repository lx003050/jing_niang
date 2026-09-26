"""鲸娘表情包

- /导入表情（管理员）：回复一条包含图片的合并转发消息，或直接附图，机器人会把图片
  本体下载到 data/qa_images/stickers/，再用视觉模型给每张图打情绪/场景标签（动图会
  均匀抽取多帧一起识别），写入同目录的 index.json。
- 随机触发：普通 AI 回复有 1/8 概率附带一张与回复内容匹配的表情。
- 意图触发：用户明确索要表情（或 AI 调用 send_sticker 工具）时 100% 发送。
- 触发词：发言去掉首尾空白后恰好只有「jn」或「鲸娘」时，直接回一张随机表情，
  不进入 AI 对话（由 priority=0 的匹配器抢先接管并阻断后续处理）。
- 带参触发：发言为「jn评价某某」或「鲸娘评价某某」时，由 AI 依据「某某」和群内上下文
  挑一张应景表情发送，只发表情、不返回任何文字。
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import random
import re
import time
from pathlib import Path

import httpx
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .common import QA_IMG_DIR, ai_config, hide_help, register_help

logger = logging.getLogger("sorting_hat.sticker")

STICKER_DIR = QA_IMG_DIR / "stickers"
STICKER_INDEX = STICKER_DIR / "index.json"
STICKER_CONTAINER = "/app/napcat/qa_images/stickers"

_MAX_TAG = 6
_RECENT_KEEP = 3
_DEFAULT_PROB = 0.125
_MAX_FRAMES = 4  # 动图打标时最多抽取的帧数
_MAX_FORWARD_DEPTH = 8  # 合并转发最大展开层数（防止无限嵌套）

_ASK_RE = re.compile(r"(表情包|表情|斗图|贴纸|吊图|梗图)")
_recent: dict[str, list[str]] = {}

_IMG_MOUNTS = {"/app/napcat/qa_images": str(QA_IMG_DIR)}


def _prob() -> float:
    try:
        return float(getattr(ai_config, "sticker_prob", _DEFAULT_PROB) or _DEFAULT_PROB)
    except Exception:
        return _DEFAULT_PROB


def wants_sticker(text: str) -> bool:
    """用户是否明确索要表情包。"""
    return bool(_ASK_RE.search(text or ""))


def _load_items() -> list[dict]:
    try:
        data = json.loads(STICKER_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    out: list[dict] = []
    for it in items or []:
        if isinstance(it, dict) and str(it.get("file") or "").strip():
            out.append(it)
    return out


def _save_items(items: list[dict]) -> None:
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    STICKER_INDEX.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _guess_ext(data: bytes) -> str:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".img"


def _mime(ext: str) -> str:
    return {
        ".gif": "image/gif",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/png")


def _sample_frames_png(data: bytes, max_frames: int = _MAX_FRAMES) -> list[bytes]:
    """从动图/静图中均匀抽取最多 max_frames 帧，统一转成 PNG，便于视觉模型识别。"""
    try:
        from PIL import Image
    except Exception:
        return []
    try:
        im = Image.open(io.BytesIO(data))
        n = int(getattr(im, "n_frames", 1) or 1)
        if n <= 1:
            idxs = [0]
        else:
            k = min(max_frames, n)
            if k <= 1:
                idxs = [0]
            else:
                idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
        out: list[bytes] = []
        for idx in idxs:
            try:
                im.seek(idx)
            except Exception:
                continue
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
            out.append(buf.getvalue())
        return out
    except Exception as e:
        logger.warning("表情抽帧失败: %s", e)
        return []


_TAG_PROMPT = (
    "你是表情包标注助手。你会看到同一张动图按时间顺序抽取的多帧画面（静图则只有一帧），"
    "请综合这些帧判断它整体的情绪和使用场景，只输出 JSON：\n"
    '{"tags":["标签1","标签2","标签3"],"desc":"一句话描述"}\n'
    "tags 为 2-5 个中文短标签，每个不超过 6 个字，用于判断什么时候该发这张表情，"
    "例如：开心、大笑、疑惑、无语、点赞、鼓励、委屈、生气、打招呼、晚安、震惊、害羞。"
)


async def _tag_image(data: bytes) -> dict:
    from .sorting_hat import _get_client, _get_vision_client

    client = _get_vision_client() or _get_client()
    if client is None:
        return {}
    frames = await asyncio.to_thread(_sample_frames_png, data)
    if frames:
        parts = [("image/png", f) for f in frames]
    else:
        parts = [(_mime(_guess_ext(data)), data)]
    content = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
            },
        }
        for mime, blob in parts
    ]
    try:
        resp = await client.chat.completions.create(
            model=ai_config.ai_vision_model or ai_config.ai_model,
            messages=[
                {"role": "system", "content": _TAG_PROMPT},
                {"role": "user", "content": content},
            ],
            max_tokens=200,
        )
    except Exception as e:
        logger.warning("表情打标失败: %s", e)
        return {}
    out = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return {}
    tags = [str(t).strip() for t in (parsed.get("tags") or []) if str(t).strip()]
    return {"tags": tags[:_MAX_TAG], "desc": str(parsed.get("desc") or "").strip()}


def _host_path(ref: str) -> Path | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("file:///"):
        ref = ref[len("file://"):]
    norm = ref.replace("\\", "/")
    for cpt, host in _IMG_MOUNTS.items():
        if norm == cpt:
            return Path(host)
        if norm.startswith(cpt + "/"):
            return Path(host + norm[len(cpt):])
    return Path(ref)


async def _seg_bytes(bot: Bot, data: dict) -> bytes | None:
    """从图片段中取出图片本体：优先 URL，其次本地路径，最后 get_image 换链。"""
    url = str(data.get("url") or "").strip()
    ref = str(data.get("file") or "").strip()
    if url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
            if r.content:
                return r.content
        except Exception as e:
            logger.warning("表情下载失败 %s: %s", url[:80], e)
    for cand in (ref, url):
        p = _host_path(cand)
        if p is None:
            continue
        try:
            if p.exists():
                return p.read_bytes()
        except Exception:
            pass
    if ref and "/" not in ref:
        try:
            ret = await bot.get_image(file=ref)
            u = str((ret or {}).get("url") or "").strip()
            if u.startswith(("http://", "https://")):
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                    r = await c.get(u)
                    r.raise_for_status()
                if r.content:
                    return r.content
        except Exception as e:
            logger.warning("表情 get_image 失败 %s: %s", ref[:60], e)
    return None


def _forward_ids(body: object) -> list[str]:
    """从消息体中提取全部合并转发段的 id（支持字符串 CQ 码、段列表、单个段）。"""
    hits = re.findall(r"CQ:forward,id=([A-Za-z0-9_\-]+)", str(body))
    if hits:
        return list(dict.fromkeys(hits))
    segs = []
    if isinstance(body, list):
        segs = body
    elif isinstance(body, dict):
        segs = [body]
    else:
        try:
            segs = list(body)  # type: ignore[arg-type]
        except Exception:
            segs = []
    out: list[str] = []
    for seg in segs:
        if isinstance(seg, dict):
            if seg.get("type") != "forward":
                continue
            fid = str((seg.get("data") or {}).get("id") or "").strip()
        else:
            if getattr(seg, "type", "") != "forward":
                continue
            fid = str((getattr(seg, "data", {}) or {}).get("id") or "").strip()
        if fid and fid not in out:
            out.append(fid)
    return out


async def _forward_nodes(bot: Bot, fid: str) -> list:
    try:
        ret = await bot.get_forward_msg(id=fid)
    except Exception:
        try:
            ret = await bot.call_api("get_forward_msg", message_id=fid)
        except Exception as e:
            logger.warning("合并转发解析失败 %s: %s", fid, e)
            return []
    if not isinstance(ret, dict):
        return []
    return list(ret.get("messages") or [])


def _seg_image(seg: object) -> tuple[str, str] | None:
    """从一个段对象/字典中取出图片 (url, file)，非图片段返回 None。"""
    if isinstance(seg, dict):
        if seg.get("type") != "image":
            return None
        data = seg.get("data") or {}
    else:
        if getattr(seg, "type", "") != "image":
            return None
        data = getattr(seg, "data", {}) or {}
    if not isinstance(data, dict):
        return None
    return (str(data.get("url") or "").strip(), str(data.get("file") or "").strip())


async def _collect_image_data(bot: Bot, event: MessageEvent) -> list[dict]:
    """收集当前消息、被引用消息，以及其中任意层级合并转发里的全部图片段。

    NapCat 在 parseMultMsg=false 时会把合并转发的整棵嵌套树内联在 forward 段的
    data.content 里（且深层转发 id 往往无法再用 get_forward_msg 取回），因此这里
    以内联 content 为主、get_forward_msg 为兜底，递归展开所有层级。
    """
    from .sorting_hat import _extract_images, _reply_id

    datas: list[dict] = []
    seen_keys: set[str] = set()

    def add(url: str, file: str) -> None:
        key = url or file
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        datas.append({"url": url, "file": file})

    def add_from(msg: object) -> None:
        for url, file in _extract_images(msg):
            add(url, file)

    async def expand_fid(fid: str, seen_fids: set[str]) -> None:
        if not fid or fid in seen_fids:
            return
        seen_fids.add(fid)
        for node in await _forward_nodes(bot, fid):
            await walk(node, seen_fids)

    async def walk(body: object, seen_fids: set[str], depth: int = 0) -> None:
        if body is None or depth > _MAX_FORWARD_DEPTH:
            return
        if isinstance(body, str):
            add_from(body)
            for fid in _forward_ids(body):
                await expand_fid(fid, seen_fids)
            return
        if isinstance(body, dict):
            seg_type = body.get("type")
            if seg_type == "image":
                img = _seg_image(body)
                if img:
                    add(*img)
                return
            if seg_type == "forward":
                data = body.get("data") or {}
                for node in data.get("content") or []:
                    await walk(node, seen_fids, depth + 1)
                await expand_fid(str(data.get("id") or "").strip(), seen_fids)
                return
            if seg_type:
                return
            if "message" in body:
                await walk(body.get("message"), seen_fids, depth)
            return
        if isinstance(body, (list, tuple)):
            for it in body:
                await walk(it, seen_fids, depth)
            return
        if isinstance(body, Message):
            for seg in body:
                await walk(seg, seen_fids, depth)
            return
        seg_type = getattr(body, "type", "")
        if seg_type == "image":
            img = _seg_image(body)
            if img:
                add(*img)
        elif seg_type == "forward":
            data = getattr(body, "data", {}) or {}
            for node in data.get("content") or []:
                await walk(node, seen_fids, depth + 1)
            await expand_fid(str(data.get("id") or "").strip(), seen_fids)

    add_from(event.message)
    reply_id = _reply_id(event)
    if not reply_id:
        logger.warning("表情图片收集: %d 张（无引用消息）", len(datas))
        return datas
    try:
        msg = await bot.get_msg(message_id=reply_id)
    except Exception as e:
        logger.warning("引用消息获取失败 %s: %s", reply_id, e)
        return datas
    await walk(msg.get("message"), set())
    logger.warning("表情图片收集: %d 张", len(datas))
    return datas


def _score(item: dict, text: str) -> int:
    if not text:
        return 0
    return sum(1 for t in (item.get("tags") or []) if t and t in text)


def pick_sticker(text: str, session_key: str = "") -> dict | None:
    items = _load_items()
    if not items:
        return None
    recent = _recent.get(session_key, [])
    pool = [it for it in items if str(it.get("file")) not in recent] or items
    scored = [(it, _score(it, text)) for it in pool]
    best = max(s for _, s in scored)
    candidates = [it for it, s in scored if s == best] if best > 0 else pool
    return random.choice(candidates)


def _remember(session_key: str, filename: str) -> None:
    if not session_key:
        return
    lst = _recent.setdefault(session_key, [])
    lst.append(filename)
    del lst[:-_RECENT_KEEP]


async def _send_item(bot: Bot, event: MessageEvent, item: dict) -> bool:
    path = STICKER_DIR / str(item.get("file"))
    if not path.exists():
        return False
    try:
        await bot.send(event, MessageSegment.image(file=f"{STICKER_CONTAINER}/{path.name}"))
        return True
    except Exception as e:
        logger.warning("表情发送失败 %s: %s", path.name, e)
        return False


async def maybe_send_sticker(
    bot: Bot,
    event: MessageEvent,
    reply_text: str,
    session_key: str = "",
    force: bool = False,
) -> bool:
    """随机（或用户明确索要时强制）发送一张与回复内容匹配的表情。"""
    if not _load_items():
        return False
    if not force and random.random() >= _prob():
        return False
    item = pick_sticker(reply_text, session_key)
    if item is None:
        return False
    ok = await _send_item(bot, event, item)
    if ok:
        _remember(session_key, str(item.get("file")))
        logger.warning("已发送表情: %s force=%s", item.get("file"), force)
    return ok


async def send_sticker_by_tag(
    bot: Bot, event: MessageEvent, tag: str, session_key: str = ""
) -> tuple[bool, str]:
    """按标签挑一张表情发送（AI 意图触发），返回 (是否成功, 失败说明)。"""
    items = _load_items()
    if not items:
        return False, "表情库还是空的，暂时发不了表情。"
    tag = (tag or "").strip()
    pool = items
    if tag:
        matched = [
            it
            for it in items
            if tag in (it.get("desc") or "")
            or any(t and (tag in t or t in tag) for t in (it.get("tags") or []))
        ]
        if matched:
            pool = matched
    item = random.choice(pool)
    ok = await _send_item(bot, event, item)
    if ok:
        _remember(session_key, str(item.get("file")))
    return ok, "" if ok else "表情发送失败了。"


# ---------- 应景挑图：按「某某」与群内上下文让模型选一张表情，只发表情不发文 ----------
_MAX_CTX_LINES = 12    # 送给模型的群内上下文最多取最近多少条
_MAX_CATALOG = 150     # 送给模型的候选表情数量上限（防止提示词过大）

_PICK_SYSTEM_PROMPT = (
    "你是表情包挑选助手。用户想让你用一张表情包来「评价」某个对象。"
    "下面会给出群内最近的聊天上下文，以及一个可用表情包库（每项含编号、情绪标签、描述）。"
    "请挑出最贴合该对象、同时最应景的那一张，只输出它的编号数字，不要输出任何其它文字。"
    "若实在没有合适的，只输出 none。"
)


def _recent_context(session_key: str) -> str:
    """从 AI 的群聊记忆里取最近若干条，拼成上下文文本；无则返回空串。"""
    from .sorting_hat import _memory

    hist = _memory.get(session_key) or []
    lines: list[str] = []
    for m in hist[-_MAX_CTX_LINES:]:
        c = str(m.get("content") or "").strip()
        if not c:
            continue
        lines.append(f"鲸娘：{c}" if m.get("role") == "assistant" else c)
    return "\n".join(lines)


def _catalog_text(items: list[dict]) -> str:
    lines: list[str] = []
    for i, it in enumerate(items[:_MAX_CATALOG]):
        tags = "、".join(str(t) for t in (it.get("tags") or []) if str(t).strip())
        desc = str(it.get("desc") or "").strip()
        lines.append(f"{i}. 标签[{tags}] {desc}")
    return "\n".join(lines)


async def send_context_sticker(
    bot: Bot, event: MessageEvent, target: str, session_key: str = ""
) -> bool:
    """按「某某」与群内上下文让模型挑一张应景表情发送；只发表情、不返回文字。

    成功返回 True；表情库为空 / AI 未配置 / 调用失败 / 模型未给出有效编号时返回 False，
    由调用方决定是否退化为随机表情。
    """
    items = _load_items()
    if not items:
        logger.warning("评价表情：表情库为空")
        return False
    from .sorting_hat import _get_client

    client = _get_client()
    if client is None:
        logger.warning("评价表情：AI 未配置，退化为随机表情")
        return False
    ctx = _recent_context(session_key)
    parts = [f"要评价的对象：{target}"]
    if ctx:
        parts.append(f"群内最近上下文：\n{ctx}")
    parts.append(f"可用表情包库：\n{_catalog_text(items)}")
    try:
        resp = await client.chat.completions.create(
            model=ai_config.ai_model,
            messages=[
                {"role": "system", "content": _PICK_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            max_tokens=20,
            temperature=0.7,
        )
    except Exception as e:
        logger.warning("评价表情 AI 调用失败: %s", e)
        return False
    out = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\d+", out)
    if not m:
        logger.warning("评价表情：模型未给出编号 (%r)", out[:40])
        return False
    idx = int(m.group(0))
    if idx >= len(items):
        logger.warning("评价表情：模型编号越界 %s / 共 %d 张", idx, len(items))
        return False
    item = items[idx]
    ok = await _send_item(bot, event, item)
    if ok:
        _remember(session_key, str(item.get("file")))
        logger.warning(
            "评价表情已发送: target=%r -> %s (标签=%s)",
            target, item.get("file"), item.get("tags"),
        )
    return ok


# ---------- 触发词：发言恰好只有 jn / 鲸娘 时回一张随机表情，不接 AI ----------
# 带参形式「jn评价某某」「鲸娘评价某某」由 AI 依上下文挑一张应景表情，同样不返回文字。
_JN_TRIGGERS = {"jn", "鲸娘"}
_JN_EVAL_RE = re.compile(r"^(?:jn|鲸娘)\s*评价\s*(?P<target>.+)$", re.I)


def _jn_rule(event: MessageEvent) -> bool:
    """命中两种形式：恰好只有「jn」/「鲸娘」（大小写不敏感），或「jn评价某某」。"""
    text = event.get_plaintext().strip()
    return text.lower() in _JN_TRIGGERS or bool(_JN_EVAL_RE.match(text))


jn_matcher = on_message(rule=_jn_rule, priority=0, block=True)


@jn_matcher.handle()
async def jn_handler(bot: Bot, event: MessageEvent):
    from .sorting_hat import _session_key

    key = _session_key(event)
    m = _JN_EVAL_RE.match(event.get_plaintext().strip())
    if m:
        ok = await send_context_sticker(bot, event, m.group("target").strip(), key)
        if not ok:  # AI 未配置 / 挑不到 / 发送失败时，退化为随机一张
            ok = await maybe_send_sticker(bot, event, "", key, force=True)
        if not ok:
            logger.warning("评价表情未发送: 表情库为空或发送失败")
    else:
        ok = await maybe_send_sticker(bot, event, "", key, force=True)
        if not ok:
            logger.warning("触发词表情未发送: 表情库为空或发送失败")
    await jn_matcher.finish()


import_cmd = on_command("导入表情", priority=1, block=True)
register_help("/导入表情", "导入表情包（管理员）：回复含图片的合并转发消息或直接附图")
hide_help("/导入表情")


@import_cmd.handle()
async def import_handler(bot: Bot, event: MessageEvent):
    from .admin_tools import is_op

    if not is_op(event.user_id):
        await import_cmd.finish("这个指令只有管理员能用哦。", at_sender=True)

    datas = await _collect_image_data(bot, event)
    if not datas:
        await import_cmd.finish(
            "没有找到图片。用法：回复一条包含图片的合并转发消息（或直接附图）再发 /导入表情。",
            at_sender=True,
        )
    await import_cmd.send(f"收到 {len(datas)} 张图片，正在下载并打标签，请稍候…… ⏳")

    items = _load_items()
    known = {str(it.get("file")) for it in items}
    added = skipped = failed = tagged = 0
    for data in datas:
        content = await _seg_bytes(bot, data)
        if not content:
            failed += 1
            continue
        name = hashlib.md5(content).hexdigest()[:16] + _guess_ext(content)
        if name in known:
            skipped += 1
            continue
        try:
            STICKER_DIR.mkdir(parents=True, exist_ok=True)
            (STICKER_DIR / name).write_bytes(content)
        except Exception as e:
            logger.warning("表情写入失败 %s: %s", name, e)
            failed += 1
            continue
        meta = await _tag_image(content)
        if meta.get("tags"):
            tagged += 1
        items.append(
            {
                "file": name,
                "tags": meta.get("tags") or [],
                "desc": meta.get("desc") or "",
                "added": int(time.time()),
            }
        )
        known.add(name)
        added += 1

    if added:
        _save_items(items)
    logger.warning(
        "表情导入完成: 新增=%d 重复=%d 失败=%d 打标=%d 总数=%d",
        added, skipped, failed, tagged, len(items),
    )
    await import_cmd.finish(
        f"导入完成：新增 {added}，跳过重复 {skipped}，失败 {failed}，成功打标签 {tagged}。\n"
        f"表情库共 {len(items)} 张。",
        at_sender=True,
    )
