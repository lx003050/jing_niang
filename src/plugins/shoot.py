"""引用图片回复「射」→ 返回白色涂料喷溅动画 GIF（支持参数）

用法：引用（回复）一张图片，然后发送单字「射」；
机器人生成「白色黏稠涂料喷溅」循环 GIF 后返回。效果移植自 white_paint.py（Pillow + numpy）。

可选参数（不带参数时全部在合理范围内随机化）：
  射 数量=30 大小=3~15 黏稠=2 起点=右上 不透明度=0.9 颜色=#FFF9C4
  · 数量 count/n         液滴数量（随机默认 5~140）
  · 大小 size           半径区间，按 480px 宽度基准，如 3~15 或单值 8（随机默认更大坨）
  · 黏稠 viscosity/v     0.1~10，越大铺展与下流越慢（随机默认 0.2~2.5，更夸张的拉丝）
  · 起点 origin/o       归一化坐标 x,y（可画外），或 上/下/左/右/左上/右上/左下/右下/中
  · 不透明度 opacity/a   0~1，越接近 1 越不透明（随机默认 0.8~1）
  · 颜色 color/c        #RRGGBB 或 白/淡黄/白黄 等（随机默认纯白~淡黄之间）

说明：随机化会约束在以上合理范围内；手动硬编码的参数直接使用、不受这些范围约束。
随机起点固定只在四个角落（左上/左下/右上/右下）中选。
该功能已在所有群禁用，仅限私聊对话使用。
"""
import asyncio
import io
import logging
import math
import random
import re
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
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageOps

from .common import QA_IMG_DIR

logger = logging.getLogger("sorting_hat.shoot")

# 输出目录与容器内挂载路径（NapCat 挂载 /app/napcat/qa_images，可直接以本地路径发送）
QA_IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR_CONTAINER = "/app/napcat/qa_images"

_PAINT_WIDTH = 400      # 最大宽度（控制 GIF 体积与耗时）
_PAINT_FPS = 12         # 每秒帧数
_PAINT_SECONDS = 3      # 动画时长（秒），末帧停留 1s

# 随机化时的合理范围（仅对"未硬编码的参数"生效）
_RND_COUNT = (5, 140)            # 数量（更多液滴，更密集）
_RND_SIZE_MAX = (12.0, 26.0)     # 最大半径范围（480px 基准），更大坨
_RND_SIZE_MIN_RATIO = (0.3, 0.8)  # 最小半径占最大半径的比例，更粗壮
_RND_VISCOSITY = (0.2, 2.5)      # 黏稠度（偏稀，铺展更快、拉丝滴落更长更夸张）
_RND_OPACITY = (0.8, 1.0)        # 不透明度（更实更厚）
_RND_COLOR_BLUE = (200, 255)     # 颜色纯白~淡黄：R=G=255，B 在此范围取值


def _lan() -> object:
    """兼容不同 Pillow 版本的 LANCZOS 重采样常量。"""
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 9.1
        return Image.LANCZOS


# ==================== 参数解析 ====================
_ORIGIN_PRESETS = {
    "上": ("top",), "下": ("bottom",), "左": ("left",), "右": ("right",),
    "左上": ("top-left",), "右上": ("top-right",), "左下": ("bottom-left",), "右下": ("bottom-right",),
    "中": ("center",), "顶": ("top",), "底": ("bottom",),
}
_ORIGIN_PRESET_XY = {  # 画外起喷点（与 spray 起点语义一致：液滴自该处射入）
    ("top",): (0.5, -0.14), ("bottom",): (0.5, 1.14),
    ("left",): (-0.14, 0.5), ("right",): (1.14, 0.5),
    ("top-left",): (-0.12, -0.10), ("top-right",): (1.12, -0.10),
    ("bottom-left",): (-0.12, 1.10), ("bottom-right",): (1.12, 1.10),
    ("center",): (0.5, 0.5),
}
_COLOR_NAMES = {
    "白": "#FFFFFF", "白色": "#FFFFFF", "white": "#FFFFFF",
    "淡黄": "#FFF9C4", "白黄": "#FFF9C4", "米黄": "#FFF9C4",
    "黄": "#FFF176", "黄色": "#FFF176", "yellow": "#FFFF00",
}
_PARAM_KEY_ALIAS = {
    "数量": "count", "count": "count", "n": "count",
    "大小": "size", "size": "size",
    "黏稠": "viscosity", "黏稠度": "viscosity", "viscosity": "viscosity", "v": "viscosity",
    "起点": "origin", "origin": "origin", "o": "origin",
    "不透明度": "opacity", "透明": "opacity", "opacity": "opacity", "a": "opacity",
    "颜色": "color", "color": "color", "c": "color",
}


def _to_float(val: str, name: str) -> float:
    try:
        f = float(val)
    except ValueError:
        raise ValueError(f"「{name}」的值「{val}」不是有效数值")
    if not math.isfinite(f):
        raise ValueError(f"「{name}」的值「{val}」必须是有限数值")
    return f


def _parse_param_value(key: str, val: str):
    """把某个参数的字符串值解析成 python 对象；格式错误抛 ValueError。"""
    if key == "count":
        try:
            n = int(val)
        except ValueError:
            raise ValueError(f"数量「{val}」不是整数")
        if n < 0:
            raise ValueError("数量不能为负")
        return n
    if key == "size":
        if "~" in val:
            lo, _, hi = val.partition("~")
            lo, hi = _to_float(lo, "大小"), _to_float(hi, "大小")
        else:
            lo = hi = _to_float(val, "大小")
        if not 0 < lo <= hi:
            raise ValueError("大小需满足 0 < 最小 <= 最大，如 3~15")
        return (lo, hi)
    if key == "viscosity":
        v = _to_float(val, "黏稠度")
        if not 0.1 <= v <= 10:
            raise ValueError("黏稠度需在 0.1~10 之间")
        return v
    if key == "opacity":
        v = _to_float(val, "不透明度")
        if not 0 <= v <= 1:
            raise ValueError("不透明度需在 0~1 之间")
        return v
    if key == "origin":
        if val in _ORIGIN_PRESETS:
            return _ORIGIN_PRESET_XY[_ORIGIN_PRESETS[val]]
        if "," in val:
            x, _, y = val.partition(",")
            x, y = _to_float(x, "起点"), _to_float(y, "起点")
            return (x, y)
        raise ValueError("起点需为 x,y 坐标或 上/下/左/右/中 等方位词")
    if key == "color":
        if val in _COLOR_NAMES:
            return _COLOR_NAMES[val]
        if re.fullmatch(r"#[0-9a-fA-F]{6}", val):
            return val.upper()
        raise ValueError("颜色需为 #RRGGBB（如 #FFF9C4）")
    raise ValueError(f"未知参数：{key}")


def _parse_params(body: str) -> dict:
    """把「射」后面的参数字符串解析成规范 dict（key -> 已解析对象）。"""
    out: dict = {}
    tokens = re.findall(r"[^\s,，]+", body.strip())
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"参数格式不对：「{tok}」应为 参数名=值")
        k, _, v = tok.partition("=")
        kk = _PARAM_KEY_ALIAS.get(k)
        if kk is None:
            raise ValueError(f"不认识参数「{k}」，可用：数量 大小 黏稠 起点 不透明度 颜色")
        if kk in out:
            raise ValueError(f"参数「{k}」重复了")
        out[kk] = _parse_param_value(kk, v)
    return out


def _resolve_params(parsed: dict, seed: int) -> dict:
    """合并硬编码参数与随机默认值；未硬编码的参数在合理范围内随机。"""
    r = random.Random(seed)
    if "count" in parsed:
        count = parsed["count"]
    else:
        count = r.randint(*_RND_COUNT)
    if "size" in parsed:
        size_min, size_max = parsed["size"]
    else:
        size_max = round(r.uniform(*_RND_SIZE_MAX), 2)
        size_min = round(max(0.5, size_max * r.uniform(*_RND_SIZE_MIN_RATIO)), 2)
    if "viscosity" in parsed:
        viscosity = parsed["viscosity"]
    else:
        viscosity = round(r.uniform(*_RND_VISCOSITY), 2)
    if "origin" in parsed:
        origin = parsed["origin"]
    else:  # 默认只从四个角落随机喷入（更夸张的横贯式泼洒）
        corner = r.choice(["top-left", "top-right", "bottom-left", "bottom-right"])
        ox, oy = _ORIGIN_PRESET_XY[(corner,)]
        origin = (ox + r.uniform(-0.12, 0.12), oy + r.uniform(-0.12, 0.12))
    if "opacity" in parsed:
        opacity = parsed["opacity"]
    else:
        opacity = round(r.uniform(*_RND_OPACITY), 3)
    if "color" in parsed:
        color = parsed["color"]
    else:  # 纯白→淡黄：R=G=255，仅蓝通道变化
        blue = r.randint(*_RND_COLOR_BLUE)
        color = "#%02X%02X%02X" % (255, 255, blue)
    return {
        "count": count, "size_min": size_min, "size_max": size_max,
        "viscosity": viscosity, "origin": origin, "opacity": opacity, "color": color,
    }


# ==================== GIF 生成核心 ====================
def _paint_gif(content: bytes, seed: int, parsed: dict | None = None) -> bytes | None:
    """把图片字节按参数处理为循环 GIF 字节；失败返回 None。

    parsed 为「射」命令硬编码的参数；未包含的参数会按随机规则补齐。
    随机布局（起喷时间/落点等）用独立 rng 以 seed 复现。
    """
    p = _resolve_params({} if parsed is None else parsed, seed)
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    im.thumbnail((_PAINT_WIDTH, round(_PAINT_WIDTH * 1.5)), _lan())
    w, h = im.size
    rgb = np.array(ImageColor.getrgb(p["color"]), dtype=np.float32)
    if rgb.shape != (3,):
        raise ValueError("颜色请使用 #RRGGBB 格式，透明程度用「不透明度」指定")
    opacity = p["opacity"]
    viscosity = p["viscosity"]

    def rgba(brightness, alpha, highlight=False):
        tint = rgb + (255 - rgb) * brightness if highlight else rgb * brightness
        return (*np.uint8(np.clip(tint, 0, 255)), round(alpha * opacity))

    rng = np.random.default_rng(seed)
    base = np.asarray(im, dtype=np.float32)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    scale = w / 480
    drops = []
    # 主喷流分成多束：较大沉积 + 细碎卫星液滴
    size_min, size_max = p["size_min"], p["size_max"]
    split = min(size_max, max(size_min, 5.0))
    count = p["count"]
    big_n = round(count * 90 / 155)
    for i in range(count):
        start = float(rng.uniform(.3, 2.15))
        tx = float(np.clip(rng.normal(.53, .20), .09, .93) * w)
        ty = float(np.clip(rng.normal(.52, .23), .10, .94) * h)
        radius = float(rng.uniform(split, size_max) if i < big_n
                       else rng.uniform(size_min, split)) * scale
        drops.append((start, float(rng.uniform(.25, .48)), tx, ty, radius,
                      float(rng.uniform(.65, 1.2)), float(rng.uniform(5, 28)) * scale))
    ox, oy = p["origin"]
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
                pn = age / flight
                sx, sy = ox * w, oy * h
                x = sx + (tx - sx) * pn
                y = sy + (ty - sy) * pn - .12 * h * math.sin(math.pi * pn)
                vx, vy = tx - sx, ty - sy - .12 * h * math.pi * math.cos(math.pi * pn)
                norm = max(math.hypot(vx, vy), 1e-6)
                length = (12 + r * 1.2) * (1 - .45 * pn)
                tail = (x - vx / norm * length, y - vy / norm * length)
                rr = max(1, r * (.16 + .22 * pn))
                pen.line([tail, (x, y)], fill=rgba(.86, 220), width=max(2, int(rr * 2)))
                pen.ellipse((x - rr, y - rr, x + rr, y + rr), fill=rgba(.98, 250))
                pen.line([(tail[0] - 1, tail[1] - 1), (x - 1, y - 1)],
                         fill=rgba(.6, 190, True), width=max(1, int(rr * .5)))
                continue
            elapsed = age - flight
            spread = .62 + .38 * (1 - math.exp(-elapsed * 15 / viscosity))
            rad = r * spread
            run = drip * max(0, elapsed - .3 * viscosity) ** .68 / viscosity
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
        alpha = np.clip((depth - .13) * 7, 0, 1) * opacity
        smooth = np.minimum(depth, 1.8)
        gy, gx = np.gradient(smooth)
        nx, ny = -gx * 10, -gy * 10
        inv = 1 / np.sqrt(nx * nx + ny * ny + 1)
        light = np.clip((-.4 * nx - .55 * ny + .73) * inv, 0, 1)
        shine = np.clip((-.22 * nx - .3 * ny + .928) * inv, 0, 1) ** 35
        diffuse = (190 + 55 * light) / 255
        paint = np.clip(rgb * diffuse[..., None] + 30 * shine[..., None], 0, 255)
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


# ==================== 触发与图片获取 ====================
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
    """触发规则：仅私聊对话中，引用（回复）了一张图片且文本为「射」或「射 参数=值 …」。"""
    if isinstance(event, GroupMessageEvent):
        return False  # 该功能已在所有群禁用，仅对话（私聊）可用
    text = event.get_plaintext().strip()
    if text == "射":
        pass
    elif text.startswith("射") and "=" in text[1:]:
        pass
    else:
        return False
    reply = getattr(event, "reply", None)
    if reply is not None and _has_image(getattr(reply, "message", None)):
        return True
    return any(seg.type == "reply" for seg in event.message)


shoot_matcher = on_message(rule=_shoot_rule, priority=0, block=True)

_HELP_PARAMS = (
    "引用一张图片回复「射」→ 白色涂料喷溅 GIF。可选参数（不带则随机）：\n"
    "射 数量=30 大小=3~15 黏稠=2 起点=右上 不透明度=0.9 颜色=#FFF9C4\n"
    "· 数量：5~140（随机）\n· 大小：半径区间，如 3~15\n"
    "· 黏稠：0.1~10\n· 起点：x,y 或 上下左右/方位\n"
    "· 不透明度：0~1\n· 颜色：#RRGGBB 或 白/淡黄"
)


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
    text = event.get_plaintext().strip()
    body = text[1:].strip() if text != "射" else ""
    if body and "=" not in body:
        await shoot_matcher.finish(_HELP_PARAMS)
    try:
        parsed = _parse_params(body) if body else {}
    except ValueError as exc:
        await shoot_matcher.finish(f"参数没看懂：{exc}\n" + _HELP_PARAMS)
    seed = random.randint(0, 2**31 - 1)
    # GIF 逐帧合成是 CPU 密集操作，放到线程池执行，避免卡住消息处理
    try:
        out = await asyncio.to_thread(_paint_gif, content, seed, parsed)
    except ValueError as exc:
        await shoot_matcher.finish(f"参数有问题：{exc}")
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
    # 体积较大的 GIF 上传偶发超时：失败后稍等重试一次，仍失败再给用户提示
    seg = MessageSegment.image(file=f"{IMG_DIR_CONTAINER}/{fname}")
    try:
        await bot.send(event, seg)
    except Exception:
        logger.warning("shoot GIF 首次发送失败，准备重试")
        await asyncio.sleep(2)
        try:
            await bot.send(event, seg)
        except Exception:
            logger.warning("shoot GIF 重试仍失败 path=%s", fname, exc_info=True)
            await shoot_matcher.finish("动图做好了但没发出去（太大或网络抖动），再发一次「射」试试？")
    await shoot_matcher.finish()


# 启动自检日志：确认本文件最新代码已被加载
logger.info("shoot 已加载 v4：引用图片回复「射」→ 白色涂料喷溅 GIF（仅私聊，群已禁用）")
