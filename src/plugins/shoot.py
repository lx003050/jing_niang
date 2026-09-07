"""引用图片回复「射」→ 返回叠加白色黏液特效后的图片

用法：引用（回复）一张图片，然后发送单字「射」；
机器人对该图片叠加白色黏液飞溅/流挂特效后返回处理结果。
效果逻辑移植自 white_slime.py（Pillow），覆盖量固定 1.5，种子随机，每次效果不同。
"""
import io
import logging
import math
import random
import time
import uuid
from pathlib import Path

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .common import QA_IMG_DIR

logger = logging.getLogger("sorting_hat.shoot")

# 输出目录与容器内挂载路径（NapCat 挂载 /app/napcat/qa_images，可直接以本地路径发送）
QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR_CONTAINER = "/app/napcat/qa_images"

_AMOUNT = 1.5                      # 黏液覆盖量（参考 white_slime.py 默认值）
_MAX_EDGE = 2048                   # 处理前最长边上限，超大图先等比缩小，避免占满内存


def _lan() -> object:
    """兼容不同 Pillow 版本的 LANCZOS 重采样常量。"""
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 9.1
        return Image.LANCZOS


def _shift(mask: Image.Image, dx: int, dy: int) -> Image.Image:
    """平移并以零填充边缘，不让阴影绕回画布另一侧。"""
    result = Image.new("L", mask.size)
    result.paste(mask, (dx, dy))
    return result


def add_slime(image: Image.Image, amount: float = _AMOUNT, seed: int | None = None) -> Image.Image:
    """给图片叠加白色黏液飞溅、流挂和高光，返回 RGBA 图片；不修改输入图片。"""
    if not math.isfinite(amount) or not 0.1 <= amount <= 5:
        raise ValueError("覆盖量必须在 0.1～5 之间")
    rng = random.Random(seed)
    base = ImageOps.exif_transpose(image).convert("RGBA")
    w, h = base.size
    # 在统一尺度生成抗锯齿液体遮罩，再适配原图大小
    scale = 1400 / max(w, h)
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
    unit = min(sw, sh)
    mask = Image.new("L", (sw, sh))
    draw = ImageDraw.Draw(mask)

    def drop(x, y, radius, stretch=1):
        draw.ellipse((x - radius, y - radius * stretch, x + radius, y + radius * stretch), fill=255)

    for _ in range(round(32 * amount)):
        x, y = rng.uniform(0, sw), rng.uniform(0, sh)
        radius = rng.uniform(.016, .053) * unit
        drop(x, y, radius, rng.uniform(.7, 1.3))
        # 由粗到细的放射液柱，末端点缀分离液滴
        for _ in range(rng.randint(5, 11)):
            angle = rng.uniform(0, math.tau)
            length = radius * rng.uniform(1.4, 4.3)
            dx, dy = math.cos(angle), math.sin(angle)
            bend = rng.uniform(-.35, .35) * radius
            for step in range(22):
                t = step / 21
                px = x + dx * length * t - dy * bend * math.sin(t * math.pi)
                py = y + dy * length * t + dx * bend * math.sin(t * math.pi)
                drop(px, py, max(.8, radius * .31 * (1 - t) + radius * .04))
            if rng.random() < .8:
                drop(x + dx * length * 1.18, y + dy * length * 1.18, radius * rng.uniform(.08, .19))
        # 重力下垂的流挂与圆润滴头
        if rng.random() < .8:
            length = rng.uniform(1.4, 5) * radius
            drift = rng.uniform(-.6, .6) * radius
            for step in range(35):
                t = step / 34
                drop(x + drift * t, y + length * t, radius * (.24 - .13 * t))
            drop(x + drift, y + length, radius * .22, 1.35)
    for _ in range(round(260 * amount)):
        drop(rng.uniform(0, sw), rng.uniform(0, sh), rng.uniform(.0015, .006) * unit)
    mask = mask.filter(ImageFilter.GaussianBlur(max(.5, unit * .0012)))
    mask = mask.resize((w, h), _lan())
    bevel = max(1, round(min(w, h) * .003))
    shadow = _shift(mask, bevel, bevel * 2).filter(ImageFilter.GaussianBlur(bevel * 1.6))
    shadow = shadow.point(lambda p: round(p * .28))
    result = Image.alpha_composite(base, Image.new("RGBA", base.size, (0, 0, 0, 0)))
    for color, alpha in [((30, 32, 35), shadow), ((239, 239, 233), mask)]:
        layer = Image.new("RGBA", base.size, (*color, 0))
        layer.putalpha(alpha)
        result = Image.alpha_composite(result, layer)
    highlight = ImageChops.subtract(mask, _shift(mask, bevel, bevel))
    highlight = highlight.filter(ImageFilter.GaussianBlur(max(.5, bevel * .35)))
    layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
    layer.putalpha(highlight)
    return Image.alpha_composite(result, layer)


def _process(content: bytes) -> bytes | None:
    """把图片字节处理为叠加特效后的 JPEG 字节；失败返回 None。"""
    with Image.open(io.BytesIO(content)) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
        longest = max(im.size)
        if longest > _MAX_EDGE:
            ratio = _MAX_EDGE / longest
            im = im.resize(
                (max(1, round(im.width * ratio)), max(1, round(im.height * ratio))),
                _lan(),
            )
        res = add_slime(im, amount=_AMOUNT, seed=None)
    # 白色底合成为 JPEG，体积小、群内发送快
    bg = Image.new("RGB", res.size, "white")
    bg.paste(res, mask=res.getchannel("A"))
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _has_image(msg) -> bool:
    """判断消息内容里是否含图片段。"""
    if msg is None:
        return False
    for seg in msg:
        t = seg.get("type") if isinstance(seg, dict) else getattr(seg, "type", "")
        if t == "image":
            return True
    return False


def _shoot_rule(event: MessageEvent) -> bool:
    """触发规则：引用（回复）了一张图片，且本条消息文本恰好为单字「射」。"""
    if event.get_plaintext().strip() != "射":
        return False
    reply = getattr(event, "reply", None)
    if reply is not None and _has_image(getattr(reply, "message", None)):
        return True
    return any(seg.type == "reply" for seg in event.message)


shoot_matcher = on_message(rule=_shoot_rule, priority=0, block=True)


def _scan_refs(msg) -> list[str]:
    """从消息段里提取图片引用（url 优先，其次 file）。"""
    refs: list[str] = []
    if msg is None:
        return refs
    for seg in msg:
        if isinstance(seg, dict):
            if seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
        else:
            if seg.type != "image":
                continue
            data = seg.data or {}
        refs.append(str(data.get("url") or "").strip() or str(data.get("file") or "").strip())
    return [r for r in refs if r]


async def _reply_image_refs(bot: Bot, event: MessageEvent) -> list[str]:
    """提取被引用（回复）消息中的图片引用；event.reply 缺失时回退 get_msg 补取。"""
    reply = getattr(event, "reply", None)
    if reply is not None:
        refs = _scan_refs(getattr(reply, "message", None))
        if refs:
            return refs
    mid = next(
        ((seg.data or {}).get("id") for seg in event.message if seg.type == "reply"),
        None,
    )
    if mid:
        try:
            msg = await bot.get_msg(message_id=int(mid))
            return _scan_refs(msg.get("message"))
        except Exception:
            logger.warning("获取被引用消息失败 mid=%s", mid, exc_info=True)
    return []


async def _fetch_image(ref: str) -> bytes | None:
    """下载/读取图片引用为字节。"""
    if ref.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(ref)
                r.raise_for_status()
            return r.content
        except Exception:
            return None
    # 容器内路径 / 本地路径：尝试映射到宿主机后直接读文件
    host = ref
    if ref.startswith("file:///"):
        host = ref[len("file://"):]
    if host.startswith("/app/napcat/qa_images"):
        host = str(QA_IMG_DIR) + host[len("/app/napcat/qa_images"):]
    p = Path(host)
    try:
        return p.read_bytes() if p.exists() else None
    except OSError:
        return None


@shoot_matcher.handle()
async def shoot_handler(bot: Bot, event: MessageEvent):
    refs = await _reply_image_refs(bot, event)
    if not refs:
        await shoot_matcher.finish("想让我射哪张图呀？先引用一张图片再发「射」。")
    content = await _fetch_image(refs[0])
    if not content:
        await shoot_matcher.finish("这张图我没能读出来，换一张试试？")
    try:
        out = _process(content)
    except Exception:
        logger.exception("白色黏液特效处理失败")
        out = None
    if out is None:
        await shoot_matcher.finish("这张图处理失败了，换一张试试？")
    fname = f"shoot_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.jpg"
    try:
        (QA_IMG_DIR / fname).write_bytes(out)
    except OSError:
        await shoot_matcher.finish("图片写盘失败了，稍后再试试？")
    await shoot_matcher.finish(MessageSegment.image(file=f"{IMG_DIR_CONTAINER}/{fname}"))


# 启动自检日志：确认本文件最新代码已被加载
logger.info("shoot 已加载 v1：引用图片回复「射」→ 白色黏液特效")
