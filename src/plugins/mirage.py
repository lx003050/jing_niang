"""幻影坦克（/幻影坦克）

把两张图合成一张「静态看是表图、动起来是里图」的动画 WebP（webp_mirage 库）。

用法一：/幻影坦克 同时带上两张图片，按出现顺序依次作为 表图、里图
用法二：/幻影坦克 引用（回复）一张图片，按提示再补发另一张
用法三：/幻影坦克 不带图片，按提示分两次发送

补图方式：回复一条含图片的消息，且该消息正文只写「表图」或「里图」，
取被回复消息的第一张图片作为对应图。
"""
import asyncio
import logging
import time
import uuid
from pathlib import Path

from PIL import Image
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from ._webp_mirage import create as mirage_create
from .common import register_help
from .image_gen import (
    IMG_DIR,
    _image_refs,
    _recall_hint,
    _reply_ref_images,
    _save_ref_image,
    _send_hint,
    _send_result_image,
)

logger = logging.getLogger("sorting_hat.mirage")

COVER, INNER = "cover", "inner"
LABELS = {"表图": COVER, "里图": INNER}
LABEL_NAME = {COVER: "表图", INNER: "里图"}
SESSION_TTL = 300.0

_sessions: dict[tuple[str, int], dict] = {}


def _key(event: MessageEvent) -> tuple[str, int]:
    if isinstance(event, GroupMessageEvent):
        return (f"g{event.group_id}", int(event.user_id))
    return (f"p{event.user_id}", int(event.user_id))


def _own_images(event: MessageEvent) -> list[str]:
    return _image_refs(event.message)


def _quoted_images(event: MessageEvent) -> list[str]:
    reply = getattr(event, "reply", None)
    if reply is None:
        return []
    return _image_refs(getattr(reply, "message", None))


def _has_reply_seg(event: MessageEvent) -> bool:
    return any(getattr(seg, "type", "") == "reply" for seg in event.message)


def _sweep() -> None:
    now = time.time()
    for key in [k for k, v in _sessions.items() if now - v.get("ts", 0) > SESSION_TTL]:
        _sessions.pop(key, None)


def _new_session(unlabeled: str | None) -> dict:
    return {"cover": None, "inner": None, "unlabeled": unlabeled, "ts": time.time()}


def _is_mirage_cmd(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    for pre in ("/", "！", "!", "／"):
        if text == pre + "幻影坦克":
            return True
    return text == "幻影坦克"


def _is_label_msg(event: MessageEvent) -> bool:
    if event.get_plaintext().strip() not in LABELS:
        return False
    if _key(event) not in _sessions:
        return False
    return bool(_quoted_images(event)) or _has_reply_seg(event)


mirage_cmd = on_message(rule=_is_mirage_cmd, priority=1, block=True)
label_matcher = on_message(rule=_is_label_msg, priority=1, block=True)

register_help(
    "/幻影坦克",
    "幻影坦克：把两张图合成一张「静态是表图、动起来是里图」的动画 WebP。"
    "/幻影坦克 同时带两张图（按顺序=表图/里图）；"
    "或引用一张图再按提示补发另一张；或直接发 /幻影坦克 按提示分两次发送。"
    "补图：回复一条含图片的消息，正文只写「表图」或「里图」。",
)


async def _prompt(bot: Bot, event: MessageEvent, text: str) -> None:
    try:
        await mirage_cmd.send(text)
    except Exception:
        logger.warning("幻影坦克提示发送失败: %s", text[:60], exc_info=True)


def _canvas_size(cover_path: Path) -> tuple[int, int] | None:
    """QQ 里动画 WebP 的播放行为与画布「朝向」强相关：

    - 正方形会被当成「动画表情」，在聊天框就内联自动播放，点开前就演完，
      不能用作幻影坦克，必须避免。
    - 横版（宽 > 高）在 QQ 里同样无法正常点播：聊天框和点开都不切换画面。
    - 只有竖版（高 > 宽）会被当成普通图片：先显示第一帧（表图），
      点开后才播放并停在里图。参照样本 832x1216 与已确认可用的「正方形补竖版」
      都是竖版，实测同为竖版才生效。

    因此封面不是「明显竖版」时，统一补成 3:4 竖版画布（画布宽取封面短边）；
    已经是明显竖版的沿用原画布，不给正常竖图白白补边。
    画布宽高必须是偶数：容器右下角的 2x2 补块要求边长成对，
    任一维为奇数会让补块塌缩（如 2x1），导致 QQ 里动画不播放。
    """
    try:
        with Image.open(cover_path) as im:
            w, h = im.size
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    if h > w and abs(w - h) > max(w, h) * 0.1:
        return None
    side = max(2, min(min(w, h), 2048) & ~1)
    return (side, max(2, round(side * 4 / 3) & ~1))


async def _generate(
    bot: Bot, event: MessageEvent, cover_ref: str, inner_ref: str
) -> None:
    placeholder = await _send_hint(bot, event, "🧩 正在合成幻影坦克……")
    cover_png = await _save_ref_image(cover_ref)
    inner_png = await _save_ref_image(inner_ref)
    if cover_png is None or inner_png is None:
        await _recall_hint(bot, placeholder)
        await _prompt(bot, event, "图片读取失败了，重新 /幻影坦克 换两张试试吧～")
        return
    out = IMG_DIR / f"mirage_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.webp"
    try:
        size = await asyncio.to_thread(_canvas_size, cover_png)
        await asyncio.to_thread(
            mirage_create,
            cover=cover_png,
            inner=inner_png,
            output=out,
            size=size,
            overwrite=True,
        )
    except Exception as e:
        logger.warning("幻影坦克合成失败: %s", str(e)[:200])
        await _recall_hint(bot, placeholder)
        await _prompt(bot, event, f"合成失败了：{str(e)[:80]}")
        return
    await _recall_hint(bot, placeholder)
    if not await _send_result_image(bot, event, out):
        await _prompt(bot, event, "图合成好了，但发送超时没送到群里，再试一次吧～")


async def _accept(bot: Bot, event: MessageEvent, role: str, ref: str) -> None:
    key = _key(event)
    sess = _sessions.get(key)
    if sess is None:
        return
    sess[role] = ref
    sess["ts"] = time.time()
    other = INNER if role == COVER else COVER
    if sess.get("unlabeled") and not sess.get(other):
        sess[other] = sess.pop("unlabeled")
    if sess.get(COVER) and sess.get(INNER):
        _sessions.pop(key, None)
        await _generate(bot, event, sess[COVER], sess[INNER])
        return
    missing = COVER if not sess.get(COVER) else INNER
    await _prompt(bot, event, f"请发送{LABEL_NAME[missing]}！")


@mirage_cmd.handle()
async def _mirage_handler(bot: Bot, event: MessageEvent):
    _sweep()
    key = _key(event)
    own = _own_images(event)
    if len(own) >= 2:
        await _generate(bot, event, own[0], own[1])
        return
    if len(own) == 1:
        _sessions[key] = _new_session(own[0])
        await _prompt(bot, event, "请发送另一张图片！")
        return
    quoted = _quoted_images(event)
    if quoted:
        _sessions[key] = _new_session(quoted[0])
        await _prompt(bot, event, "请发送另一张图片！")
        return
    _sessions[key] = _new_session(None)
    await _prompt(bot, event, "请发送表图！")


@label_matcher.handle()
async def _label_handler(bot: Bot, event: MessageEvent):
    role = LABELS.get(event.get_plaintext().strip())
    if role is None or _key(event) not in _sessions:
        return
    refs = await _reply_ref_images(bot, event)
    if not refs:
        await label_matcher.finish(
            "没找到图片，请回复一条含图片的消息，并且只写「表图」或「里图」。"
        )
    await _accept(bot, event, role, refs[0])
