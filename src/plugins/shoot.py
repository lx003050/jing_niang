"""引用图片回复「射」→ 返回白色涂料喷溅动画 GIF

用法：引用（回复）一张图片，然后发送单字「射」；
机器人对该图片生成「白色黏稠涂料喷溅」的循环动画 GIF 后返回。
效果逻辑移植自 white_paint.py（Pillow + numpy），模拟涂料附着在图片前的透明平面上。
"""
import asyncio
import io
import logging
import math
import random
import time
import uuid
from pathlib import Path

import httpx
import numpy as np
from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .common import QA_IMG_DIR

logger = logging.getLogger("sorting_hat.shoot")

# 输出目录与容器内挂载路径（NapCat 挂载 /app/napcat/qa_images，可直接以本地路径发送）
QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR_CONTAINER = "/app/napcat/qa_images"

_PAINT_WIDTH = 480      # 最大宽度（参考 white_paint.py 默认）
_PAINT_FPS = 20         # 每秒帧数
_PAINT_SECONDS = 5      # 动画时长（秒），末帧停留 1s


def _lan() -> object:
    """兼容不同 Pillow 版本的 LANCZOS 重采样常量。"""
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 9.1
        return Image.LANCZOS


def _paint_gif(content: bytes, seed: int) -> bytes | None:
    """把图片字节处理为白色涂料喷溅循环 GIF 字节；失败返回 None。

    逐帧模拟：主喷流飞入 → 撞击后高斯厚度场扩散沉积成黏稠覆盖层 → 顺重力下流，
    液滴自然融合，配合灰度高度场法线打光与高光。逻辑与 white_paint.py 一致。
    """
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    im.thumbnail((_PAINT_WIDTH, round(_PAINT_WIDTH * 1.5)), _lan())
    w, h = im.size
    rng = np.random.default_rng(seed)
    base = np.asarray(im, dtype=np.float32)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    scale = w / _PAINT_WIDTH
    drops = []
    # 主喷流分成多束，形成不同大小的沉积与细碎卫星液滴
    for i in range(155):
        start = float(rng.uniform(.3, 2.15))
        tx = float(np.clip(rng.normal(.53, .20), .09, .93) * w)
        ty = float(np.clip(rng.normal(.52, .23), .10, .94) * h)
        radius = float(rng.uniform(5, 19) if i < 90 else rng.uniform(1.5, 5)) * scale
        drops.append((start, float(rng.uniform(.25, .48)), tx, ty, radius,
                      float(rng.uniform(.65, 1.2)), float(rng.uniform(5, 28)) * scale))
    frames = []
    for frame in range(round(_PAINT_FPS * _PAINT_SECONDS)):
        t = frame / _PAINT_FPS
        depth = np.zeros((h, w), dtype=np.float32)
        airborne = Image.new("RGBA", (w, h))
        pen = ImageDraw.Draw(airborne)
        for start, flight, tx, ty, r, strength, drip in drops:
            age = t - start
            if age < 0:
                continue
            if age < flight:
                p = age / flight
                sx, sy = -.12 * w, -.08 * h
                x = sx + (tx - sx) * p
                y = sy + (ty - sy) * p - .12 * h * math.sin(math.pi * p)
                vx, vy = tx - sx, ty - sy - .12 * h * math.pi * math.cos(math.pi * p)
                norm = math.hypot(vx, vy)
                length = (12 + r * 1.2) * (1 - .45 * p)
                tail = (x - vx / norm * length, y - vy / norm * length)
                rr = max(1, r * (.16 + .22 * p))
                pen.line([tail, (x, y)], fill=(221, 222, 215, 220), width=max(2, int(rr * 2)))
                pen.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(252, 252, 246, 250))
                pen.line([(tail[0] - 1, tail[1] - 1), (x - 1, y - 1)],
                         fill=(255, 255, 251, 190), width=max(1, int(rr * .5)))
                continue
            elapsed = age - flight
            spread = .62 + .38 * (1 - math.exp(-elapsed * 15))
            rad = r * spread
            run = drip * max(0, elapsed - .3) ** .68
            # 有限范围高斯厚度场，液滴自然融合成不规则黏稠覆盖层
            xmin, xmax = max(0, int(tx - r * 3)), min(w, int(tx + r * 3 + 1))
            ymin, ymax = max(0, int(ty - r * 3)), min(h, int(ty + run + r * 3 + 1))
            X, Y = xx[ymin:ymax, xmin:xmax], yy[ymin:ymax, xmin:xmax]
            patch = strength * np.exp(-((X - tx) / (rad * 1.12)) ** 2 - ((Y - ty) / rad) ** 2)
            if r > 7 * scale and run > 0:
                center = np.clip(Y, ty, ty + run)
                patch += .63 * strength * np.exp(-((X - tx) / (rad * .3)) ** 2 - ((Y - center) / (rad * .45)) ** 2)
                patch += .5 * strength * np.exp(-((X - tx) / (rad * .42)) ** 2 - ((Y - ty - run) / (rad * .55)) ** 2)
            depth[ymin:ymax, xmin:xmax] += patch
        alpha = np.clip((depth - .13) * 7, 0, 1)
        smooth = np.minimum(depth, 1.8)
        gy, gx = np.gradient(smooth)
        nx, ny = -gx * 10, -gy * 10
        inv = 1 / np.sqrt(nx * nx + ny * ny + 1)
        light = np.clip((-.4 * nx - .55 * ny + .73) * inv, 0, 1)
        shine = np.clip((-.22 * nx - .3 * ny + .928) * inv, 0, 1) ** 35
        tone = np.clip(190 + 55 * light + 30 * shine, 0, 255)
        paint = np.stack([tone, tone, tone * .982], axis=-1)
        mask = Image.fromarray(np.uint8(alpha * 255))
        shadow = np.asarray(mask.filter(ImageFilter.GaussianBlur(2.2 * scale)), dtype=np.float32) / 255
        shadow = np.roll(shadow, (max(1, int(3 * scale)), max(1, int(2 * scale))), axis=(0, 1))
        composite = base * (1 - .28 * shadow[..., None])
        composite = composite * (1 - alpha[..., None]) + paint * alpha[..., None]
        image = Image.fromarray(np.uint8(np.clip(composite, 0, 255))).convert("RGBA")
        image = Image.alpha_composite(image, airborne).convert("RGB")
        frames.append(image.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    buf = io.BytesIO()
    n = len(frames)
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[round(1000 / _PAINT_FPS)] * (n - 1) + [1000],
        loop=0,
        optimize=False,
        disposal=2,
    )
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
    # GIF 逐帧合成是 CPU 密集操作，放到线程池执行，避免卡住消息处理
    try:
        out = await asyncio.to_thread(_paint_gif, content, random.randint(0, 2**31 - 1))
    except Exception:
        logger.exception("白色涂料喷溅 GIF 生成失败")
        out = None
    if out is None:
        await shoot_matcher.finish("这张图处理失败了，换一张试试？")
    fname = f"shoot_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.gif"
    try:
        (QA_IMG_DIR / fname).write_bytes(out)
    except OSError:
        await shoot_matcher.finish("动图写盘失败了，稍后再试试？")
    await shoot_matcher.finish(MessageSegment.image(file=f"{IMG_DIR_CONTAINER}/{fname}"))


# 启动自检日志：确认本文件最新代码已被加载
logger.info("shoot 已加载 v2：引用图片回复「射」→ 白色涂料喷溅 GIF")
