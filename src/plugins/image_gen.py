"""文生图 / 图生图（/生图）

- /生图 描述：文生图
- 引用图片 + /生图 [描述]：以引用图为参考的图生图
- 参数 -o：不使用默认优化提示词，按用户原文直出

依赖 moyuu.cc 等 OpenAI 兼容生图接口（images.generations / images.edits）。
遇上游渠道临时不可用自动重试。
"""
import asyncio
import base64
import io
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from openai import AsyncOpenAI

from .admin_tools import OP_SEED, is_op
from .common import DATA_DIR, ai_config, hide_help, register_help, send_forward_text

logger = logging.getLogger("sorting_hat.image")

# 本地图片目录（NapCat 挂载为 /app/napcat/qa_images，可直接以本地路径发送）
IMG_DIR = DATA_DIR / "qa_images"
IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR_CONTAINER = "/app/napcat/qa_images"

# 默认优化提示词（追加在用户描述之后；-o 参数跳过）
DEFAULT_T2I_PROMPT = (
    "，画面高质量、细节丰富、光影自然、色彩准确，"
    "构图专业，商业级高清渲染，必要时附上意境氛围")
DEFAULT_I2I_PROMPT = (
    "，参考图中目标主体与构图进行重绘优化，"
    "提升细节与质感，画面清晰、皮肤与材质真实，高清商业渲染")

# -anime 模式：高质量动漫提示词（配合 nano-banana 模型）
ANIME_T2I_PROMPT = (
    "，高质量动漫插图风格：线条干净精细、赛璐璐上色搭配唯美光影，"
    "人物五官精致、眼睛水润有神且含高光，发丝根根分明，"
    "服饰与背景细节丰富、层次分明，构图考究、画风统一，"
    "达到商业动画原画/游戏立绘级别的完成度")
ANIME_I2I_PROMPT = (
    "，以高质量动漫插图画风重绘参考图：保持人物形象、姿势与构图不变，"
    "线条精细干净、赛璐璐上色，光影氛围唯美，"
    "五官精致、眼神传神、发丝清晰，服饰背景细节完善，完成度高")

_IMG_MOUNTS = {
    "/app/napcat/qa_images": str(IMG_DIR),
}

# 生图失败可重试的错误标志（上游渠道临时不可用，平台错误自带 retry 提示）
_RETRYABLE_MARKS = ("get_channel_failed", "retry", "timeout", "timed out", "upstream", "502", "503", "504")

# -ensure / -e：审查类报错时自动加「更安全的措辞」重试（原 prompt 主体与参考图保持不变）
_ENSURE_DEFAULT = 10   # 未指定数字时默认尝试次数
_ENSURE_SAFE_SUFFIXES = (
    "画面干净温和、内容积极向上，适合大众观看",
    "风格优雅含蓄、得体大方，不涉及任何敏感或争议内容",
    "以纯艺术表现呈现，构图美观、主题健康正面",
    "整体氛围明亮治愈，人物形象亲和，画面无不当元素",
    "画面唯美和谐，内容为温馨正面的日常场景",
)

# ---- /生图 -local：本机 sd.cpp（SD1.5 CPU 推理）----
LOCAL_SD_SH = "/opt/sdrel/sd.sh"                # 服务器运行包装（独立 glibc2.39 loader）
LOCAL_SD_MODEL = "/opt/models/sd15-Q4_0.gguf"   # SD1.5 Q4_0 GGUF 单文件
_LOCAL_SD_LOCK = asyncio.Lock()                 # 一张约占满双核 20-30 分钟，禁止并发
_LOCAL_SD_WIDTH, _LOCAL_SD_HEIGHT = 384, 384    # 默认出图规模
_LOCAL_SD_STEPS = 15                            # 默认迭代步数
_LOCAL_SD_MIN_EDGE = 256                        # -s 可指定的最小边
_LOCAL_SD_MAX_EDGE = 1024                       # -s 可指定的最大边
_LOCAL_SD_MAX_STEPS = 80                        # -step 可指定的最大步数
_LOCAL_SD_PROC: asyncio.subprocess.Process | None = None  # 当前正在跑的本地推理子进程（供 /停生图 打断）
_LOCAL_SD_CANCEL = False                        # 打断标志：置位后本地生图流程尽快终止


def _parse_local_opt(raw_low: str) -> tuple[tuple[int, int] | None, int | None]:
    """解析 -local 专属参数，返回 (尺寸(W,H)|None, 步数|None)。

    用法：-s 宽x高（如 -s 512x768）指定规模、-step 次数（如 -step 30）指定迭代步数；
    尺寸按 32 的倍数就近取整并夹到 [256,1024]，步数夹到 [1,80]。
    """
    size: tuple[int, int] | None = None
    m = re.search(
        r"(?:-s(?![a-z0-9])|-size(?![a-z0-9]))\s*(\d{2,5})\s*[xX*×]\s*(\d{2,5})",
        raw_low,
    )
    if m:

        def _snap(v: int) -> int:
            return max(_LOCAL_SD_MIN_EDGE, min(_LOCAL_SD_MAX_EDGE, round(v / 32) * 32))

        size = (_snap(int(m.group(1))), _snap(int(m.group(2))))
    steps: int | None = None
    m = re.search(r"-steps?\s*(\d{1,3})", raw_low)
    if m:
        steps = max(1, min(_LOCAL_SD_MAX_STEPS, int(m.group(1))))
    return size, steps


def _local_eta_min(width: int, height: int, steps: int) -> int:
    """估算本机出图耗时（分钟）：384x384/15 步基线约 28 分钟，按像素数 × 步数线性折算。"""
    return max(1, round(28 * (width * height) / (384 * 384) * (steps / 15)))


_NSFW_AUDIT_SYS = (
    "你是严格的图片内容审核员。请判断图片是否存在以下问题："
    "裸露或色情、性暗示、血腥暴力、涉政敏感、未成年人不宜内容，或其它违法违规内容。\n"
    '仅输出一个 JSON 对象：{"safe": true 或 false, "reason": "一句话原因"}（safe=false 表示存在违规）。'
)


def _ensure_suffix(attempt: int) -> str:
    """第 attempt 次重试(>=1)追加的安全措辞，轮换使用避免与上次雷同。"""
    return "，" + _ENSURE_SAFE_SUFFIXES[(attempt - 1) % len(_ENSURE_SAFE_SUFFIXES)]


def _map_container_path(path: str) -> str | None:
    """容器内路径映射为宿主机可读路径。"""
    norm = path.replace("\\", "/")
    for cpt, host in _IMG_MOUNTS.items():
        if norm == cpt:
            return host
        if norm.startswith(cpt + "/"):
            return host + norm[len(cpt):]
    return None


def _image_refs(msg: object) -> list[str]:
    """从消息内容（段列表/Message 或 OneBot 原始 dict 列表）中提取图片引用（url / file）。"""
    refs: list[str] = []
    if msg is None:
        return refs
    for seg in msg:
        if isinstance(seg, dict):
            seg_type = seg.get("type", "")
            data = seg.get("data") or {}
            if not isinstance(data, dict):
                data = {}
        else:
            seg_type = getattr(seg, "type", "")
            data = getattr(seg, "data", None) or {}
        if seg_type != "image":
            continue
        url = str(data.get("url") or "").strip()
        file = str(data.get("file") or "").strip()
        refs.append(url or file)
    return [r for r in refs if r]


async def _reply_ref_images(bot: Bot, event: MessageEvent) -> list[str]:
    """提取参考图片：优先被引用（回复）消息中的图片，其次当前消息中的图片。

    引用消息的完整内容由 NoneBot 解析到 event.reply（来自 get_msg API）；
    若 event.reply 缺失但事件里残留 reply 段，回退调用 get_msg 补取。
    """
    refs: list[str] = []
    reply = getattr(event, "reply", None)
    if reply is not None:
        refs.extend(_image_refs(getattr(reply, "message", None)))
    if not refs:
        mid = next(
            ((seg.data or {}).get("id") for seg in event.message if seg.type == "reply"),
            None,
        )
        if mid:
            try:
                msg = await bot.get_msg(message_id=int(mid))
                refs.extend(_image_refs(msg.get("message")))
            except Exception:
                logger.warning("获取被引用消息图片失败 mid=%s", mid, exc_info=True)
    if not refs:
        refs.extend(_image_refs(event.message))
    return refs


def _to_rgb_png(content: bytes) -> bytes | None:
    """统一转换成 RGB PNG：moyuu edit 接口对 RGBA(带 alpha/4 通道)PNG 及部分 JPEG 会回显原图。"""
    try:
        im = Image.open(io.BytesIO(content))
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


async def _save_ref_image(ref: str) -> Path | None:
    """把图片引用下载/读取为本地临时文件（统一转 RGB PNG），返回路径。"""
    content: bytes | None = None
    if ref.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
                r = await c.get(ref)
                r.raise_for_status()
            content = r.content
        except Exception:
            return None
    else:
        host = _map_container_path(ref[len("file://"):]) if ref.startswith("file:///") else ref
        if not host:
            return None
        p = Path(host)
        if not p.exists():
            return None
        content = p.read_bytes()
    rgb = _to_rgb_png(content)
    if rgb is None:
        return None
    out = IMG_DIR / f"ref_{uuid.uuid4().hex[:8]}.png"
    try:
        out.write_bytes(rgb)
        return out
    except Exception:
        return None


async def _images_call_with_retry(
    client: AsyncOpenAI, *, edit: Path | None, prompt: str, model: str, size: str
) -> object:
    """调用生图接口，遇上游渠道临时不可用自动重试（最多 5 轮，递进等待）。"""
    last: Exception | None = None
    for attempt in range(5):
        try:
            if edit is not None:
                return await client.images.edit(model=model, image=edit, prompt=prompt, n=1, size=size)
            return await client.images.generate(model=model, prompt=prompt, n=1, size=size)
        except Exception as e:
            last = e
            code = getattr(e, "status_code", None)
            msg = str(e).lower()
            retryable = (
                (code is not None and code >= 500)
                or code == 429
                or "channel" in msg
                or "retry" in msg
                or "timeout" in msg
                or "timed out" in msg
                or "upstream" in msg
            )
            if not retryable or attempt == 4:
                raise
            logger.warning("生图上游不可用，第 %d/%d 次重试: %s", attempt + 1, 5, str(e)[:150])
            await asyncio.sleep(3 + attempt * 4)
    assert last is not None
    raise last


async def ai_text2image_file(prompt: str) -> Path | None:
    """供 AI 对话通路等外部调用的云端文生图：按默认机型生成并存入本地，返回文件路径。"""
    if not ai_config.moyuu_api_key:
        return None
    client = AsyncOpenAI(
        api_key=ai_config.moyuu_api_key, base_url=ai_config.moyuu_base_url, timeout=180.0
    )
    try:
        resp = await _images_call_with_retry(
            client, edit=None, prompt=prompt, model=ai_config.image_model, size=ai_config.image_size
        )
    except Exception as e:
        logger.warning("AI 通路生图失败: %s", str(e)[:150])
        return None
    item = ((resp.model_dump().get("data") or [{}])[0]) or {}
    b64 = item.get("b64_json") or ""
    url = item.get("url") or ""
    if url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
            b64 = base64.b64encode(r.content).decode("ascii")
        except Exception:
            b64 = ""
    if not b64:
        return None
    out = IMG_DIR / f"ai2img_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
    try:
        out.write_bytes(base64.b64decode(b64))
        return out
    except Exception:
        return None


image_cmd = on_command("生图", aliases={"文生图", "图生图", "生成图片"}, priority=5, block=True)
register_help("/生图", "文生图/图生图：/生图 描述；引用图片则按图生图；-o 不用默认提示词；-local 本机 SD1.5 慢速出图（可加 -s 宽x高 改规模、-step 次数 改迭代步数）；-nsfw 跳过审查（仅根管理员）")


@image_cmd.handle()
async def image_handler(bot: Bot, event: MessageEvent):
    raw = event.get_plaintext().lstrip()
    low = raw.lower()
    glm = "-glm" in low and (" -glm" in f" {low}" or low.endswith("-glm") or low.startswith("-glm"))
    qwen = False if glm else ("-qwen" in low and (" -qwen" in f" {low}" or low.endswith("-qwen") or low.startswith("-qwen")))
    seedp = False if (glm or qwen) else ("-seedp" in low and (" -seedp" in f" {low}" or low.endswith("-seedp") or low.startswith("-seedp")))
    seed = False if (glm or qwen or seedp) else ("-seed" in low and (" -seed" in f" {low}" or low.endswith("-seed") or low.startswith("-seed")))
    anime = False if (glm or qwen or seed or seedp) else ("-anime" in low and (" -anime" in f" {low}" or low.endswith("-anime") or low.startswith("-anime")))
    gemini31 = False if (glm or qwen or seed or seedp or anime) else ("-gemini3.1" in low and (" -gemini3.1" in f" {low}" or low.endswith("-gemini3.1") or low.startswith("-gemini3.1")))
    gemini = False if (glm or qwen or seed or seedp or anime or gemini31) else ("-gemini" in low and (" -gemini" in f" {low}" or low.endswith("-gemini") or low.startswith("-gemini")))
    sense = False if (glm or qwen or seed or seedp or anime or gemini31 or gemini) else ("-sense" in low and (" -sense" in f" {low}" or low.endswith("-sense") or low.startswith("-sense")))
    no_opt = "-o" in low and (" -o" in f" {low}" or low.endswith("-o") or low.startswith("-o"))
    is_local = "-local" in low and (" -local" in f" {low}" or low.endswith("-local") or low.startswith("-local"))
    is_nsfw = "-nsfw" in low and (" -nsfw" in f" {low}" or low.endswith("-nsfw") or low.startswith("-nsfw"))
    # -local 专属参数：-s 宽x高 指定规模、-step 次数 指定迭代步数（云端机型不生效，仅剥离）
    local_size, local_steps = _parse_local_opt(low)

    # -ensure / -e：审查类报错时自动加安全措辞重试；数字可选（如 -e -20 = 试 20 次，默认 10 次）
    ensure_total: int | None = None
    if re.search(r"-ensure|-e(?![a-z0-9])", low):
        m_n = re.search(r"(?:-ensure|-e(?![a-z0-9]))\s*(-?\d+)", low)
        ensure_total = abs(int(m_n.group(1))) if m_n else _ENSURE_DEFAULT

    text_src = raw
    if ensure_total is not None:
        # 把 -ensure / -e 及其紧随的次数参数从文本里剥掉（并折叠多余空白）
        text_src = " ".join(
            re.sub(r"(?:-ensure|-e(?![a-z0-9]))(?:\s*-?\d+)?", " ", raw, flags=re.IGNORECASE).split()
        )
    text = (
        text_src.replace("-GLM", "").replace("-glm", "")
        .replace("-qwen", "").replace("-seedp", "").replace("-SEEDP", "").replace("-seed", "").replace("-anime", "")
        .replace("-gemini31", "").replace("-gemini3.1", "").replace("-GEMINI31", "").replace("-GEMINI3.1", "")
        .replace("-gemini", "").replace("-GEMINI", "").replace("-sense", "").replace("-SENSE", "")
        .replace("-local", "").replace("-LOCAL", "").replace("-nsfw", "").replace("-NSFW", "")
        .replace("-o", "", 1)
        .strip()
    )
    # 剥掉 -local 专属参数及其值（-s 宽x高 / -step 次数），避免混进提示词
    text = re.sub(r"(?i)(?:-s(?![a-z0-9])|-size(?![a-z0-9]))\s*\d{2,5}\s*[xX*×]\s*\d{2,5}", " ", text)
    text = re.sub(r"(?i)-steps?\s*\d{1,3}", " ", text)
    text = text.strip()
    if not text:
        hint = "给我一句描述呀～比如：/生图 一只猫在月光下看星星\n" \
               "引用一张图片再发 /生图 可以按图生图。可选机型参数：\n" \
               "-anime 动漫（nano-banana）；-seed 即梦Seedream4.0；-seedp Seedream5.0；-GLM CogView；" \
               "-qwen 万相Wan；-gemini GeminiPro预览；-gemini3.1 GeminiFlash；-sense 商汤U1.5海报/信息图；" \
               "-o 不用我加的默认提示词；\n" \
               "-ensure（或 -e）审查报错时自动加安全措辞重试，默认 10 次，可带数字指定次数（如 -e -20 表示尝试 20 次）；\n" \
               "-local 本机 SD1.5 慢速出图（默认 384x384/15 步，可加 -s 宽x高 改规模、-step 次数 改步数，\n" \
               "  如：-local -s 512x768 -step 30）；-nsfw 跳过内容审查（仅根管理员，须配合 -local）。"
        try:
            await send_forward_text(bot, event, hint, name="分院帽·生图提示")
        except Exception:
            await image_cmd.finish(hint)
        await image_cmd.finish()

    # -local：本机 SD1.5 CPU 推理（与云端机型互斥，单独走完整流程）
    if is_local:
        if is_nsfw and event.user_id != OP_SEED:
            await image_cmd.finish("-nsfw 仅根管理员可用；普通用户的本地出图会先经内容审查。", at_sender=True)
        await _local_generate(bot, event, text, allow_nsfw=is_nsfw, size=local_size, steps=local_steps)
        return

    # 选择机型：默认 / -anime / -seed / -seedp / -GLM / -qwen / -gemini / -gemini3.1（一次只取一个）
    img_size = ai_config.image_size
    if glm:
        api_key, base_url, model = ai_config.paratera_api_key, ai_config.paratera_base_url, ai_config.image_glm_model
        anime_style = False
    elif qwen:
        api_key, base_url, model = ai_config.paratera_api_key, ai_config.paratera_base_url, ai_config.image_qwen_model
        anime_style = False
    elif seedp:
        api_key, base_url, model = ai_config.paratera_api_key, ai_config.paratera_base_url, ai_config.image_seedp_model
        anime_style = False
        img_size = "1920x1920"   # Seedream 5.0-lite 要求总像素 ≥3686400
    elif seed:
        api_key, base_url, model = ai_config.paratera_api_key, ai_config.paratera_base_url, ai_config.image_seed_model
        anime_style = False
    elif gemini:
        api_key, base_url, model = ai_config.moyuu_gemini_api_key or ai_config.moyuu_api_key, ai_config.moyuu_base_url, ai_config.image_gemini_model
        anime_style = False
    elif gemini31:
        api_key, base_url, model = ai_config.moyuu_gemini_api_key or ai_config.moyuu_api_key, ai_config.moyuu_base_url, ai_config.image_gemini31_model
        anime_style = False
    elif sense:
        # SenseNova U1.5 系列为文生图模型（仅 text 输入），独立 key/base；不传 size 用平台默认
        api_key, base_url, model = ai_config.sensenova_api_key, ai_config.sensenova_base_url, ai_config.image_sense_model
        anime_style = False
        img_size = None
    elif anime:
        api_key = ai_config.moyuu_anime_api_key or ai_config.moyuu_api_key
        base_url, model, anime_style = ai_config.moyuu_base_url, ai_config.image_anime_model, True
    else:
        api_key, base_url, model = ai_config.moyuu_api_key, ai_config.moyuu_base_url, ai_config.image_model
        anime_style = False
    if not api_key:
        await image_cmd.finish("生图功能未配置 API Key，请管理员在 .env 中填写相应 Key。")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=180.0)

    placeholder = await _send_hint(bot, event)
    refs = await _reply_ref_images(bot, event)
    if sense and refs:
        await _recall_hint(bot, placeholder)
        await image_cmd.finish("-sense（商汤 U1.5）是纯文生图模型，不支持引用图片图生图，直接发描述即可。", at_sender=True)
    edit_file: Path | None = None
    try:
        if refs:
            ref = refs[0]
            tmp = await _save_ref_image(ref)
            if no_opt:
                prompt = text
            elif anime_style:
                prompt = text + ANIME_I2I_PROMPT
            else:
                prompt = text + DEFAULT_I2I_PROMPT
            if tmp is None:
                await _recall_hint(bot, placeholder)
                await image_cmd.finish("引用的图片读取失败了，换一张试试？")
            edit_file = tmp
        else:
            if no_opt:
                prompt = text
            elif anime_style:
                prompt = text + ANIME_T2I_PROMPT
            else:
                prompt = text + DEFAULT_T2I_PROMPT

        # -ensure：审查类报错时，不直接报「画布被打翻」，而是逐轮追加安全措辞重试；
        # 原 prompt 主体、参考图等一律不变，仅每轮在末尾追加一句更安全的描述。
        if ensure_total is not None:
            resp = None
            last_err: Exception | None = None
            for attempt in range(ensure_total):
                cur_prompt = prompt if attempt == 0 else prompt + _ensure_suffix(attempt)
                try:
                    resp = await _images_call_with_retry(
                        client, edit=edit_file, prompt=cur_prompt, model=model, size=img_size,
                    )
                    break
                except Exception as e2:
                    last_err = e2
                    step = attempt + 1
                    if step % 5 == 0 and step < ensure_total:
                        # 每满 5 次尝试汇报一次进度，避免频繁刷屏
                        logger.warning(
                            "ensure 已尝试 %d/%d 次，加安全措辞重试: %s",
                            step, ensure_total, str(e2)[:150],
                        )
                        await _recall_hint(bot, placeholder)
                        placeholder = await _send_hint(
                            bot, event, f"⚠️ 遇到报错，再次尝试……（已尝试 {step}/{ensure_total} 次）"
                        )
                        await asyncio.sleep(1.0)
            if resp is None:
                assert last_err is not None
                raise last_err
        else:
            resp = await _images_call_with_retry(
                client, edit=edit_file, prompt=prompt, model=model, size=img_size,
            )
    except Exception as e:
        logger.warning("生图失败: %s", e)
        await _recall_hint(bot, placeholder)
        await image_cmd.finish("啊，画布被打翻了……请尝试修改 prompt 再试一次～")
    finally:
        if edit_file is not None:
            try:
                edit_file.unlink()
            except Exception:
                pass

    items = resp.model_dump().get("data") or []
    if not items:
        await _recall_hint(bot, placeholder)
        await image_cmd.finish("这个模型返回了空结果（平台渠道可能未接通或该模型暂不可用），换个机型试试？")
    item = items[0]
    b64 = item.get("b64_json") or ""
    url = item.get("url") or ""
    if url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
                b64 = base64.b64encode(r.content).decode("ascii")
        except Exception:
            b64 = ""
    if not b64:
        await _recall_hint(bot, placeholder)
        await image_cmd.finish("出图了但没送到手上（图片下载失败），稍后再试试？")

    fname = f"img_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
    (IMG_DIR / fname).write_bytes(base64.b64decode(b64))
    await _recall_hint(bot, placeholder)
    await image_cmd.send(MessageSegment.image(file=f"{IMG_DIR_CONTAINER}/{fname}"))


async def _send_hint(bot: Bot, event: MessageEvent, text: str = "🎨 画布铺开，灵感酝酿中……") -> int | None:
    try:
        if isinstance(event, GroupMessageEvent):
            sent = await bot.send_group_msg(group_id=event.group_id, message=text)
        else:
            sent = await bot.send_private_msg(user_id=event.user_id, message=text)
        mid = int((sent or {}).get("message_id") or 0)
        return mid or None
    except Exception:
        return None


async def _recall_hint(bot: Bot, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_msg(message_id=message_id)
    except Exception:
        pass


# ---- /生图 -local 本地推理工具 ----

def _local_llm_client() -> AsyncOpenAI | None:
    """本地流程用的 LLM 客户端：优先视觉通道（识图/审查），未配置则回退主对话通道。"""
    key = ai_config.ai_vision_api_key or ai_config.openai_api_key
    base = ai_config.ai_vision_base_url or ai_config.openai_base_url
    if not key:
        return None
    return AsyncOpenAI(api_key=key, base_url=base or None, timeout=90.0)


async def _local_prompt_en(text: str) -> str | None:
    """SD1.5 只懂英文；含中文等非 ASCII 的描述先翻译成英文。

    - 纯 ASCII 原样返回（跳过云端翻译，敏感内容可走英文直通）；
    - 翻译结果若命中明显的拒答话术则返回 None（供调用方中止，避免把废提示词喂给 SD 白跑几小时）；
    - 翻译通道异常时回退原文。
    """
    if not re.search(r"[^\x00-\x7f]", text):
        return text
    if not ai_config.openai_api_key:
        return text
    try:
        client = AsyncOpenAI(
            api_key=ai_config.openai_api_key,
            base_url=ai_config.openai_base_url or None,
            timeout=60.0,
        )
        resp = await client.chat.completions.create(
            model=ai_config.ai_model,
            messages=[
                {"role": "system", "content": "把用户的图像描述翻译成简洁地道的英文图像提示词，"
                                              "用逗号连接关键词短语，只输出译文。"},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=256,
        )
        en = (resp.choices[0].message.content or "").strip().strip('"')
        if not en:
            return None
        if _is_refusal_text(en):
            return None
        return en
    except Exception:
        logger.warning("本地生图提示词翻译失败，使用原文", exc_info=True)
        return text


def _is_refusal_text(en: str) -> bool:
    """粗判翻译输出是否为拒绝话术（短句 + 命中拒答关键词），避免误杀正常译文。"""
    if len(en) >= 200:
        return False
    low = en.lower()
    return any(
        k in low for k in (
            "i'm sorry", "i am sorry", "i cannot", "i can't",
            "cannot provide", "can't provide", "cannot assist",
            "unable to provide", "unable to assist", "not able to provide",
            "as an ai", "as a language model", "cannot generate",
            "won't generate", "refus",
            "抱歉", "对不起", "无法", "拒绝", "不能", "不会",
        )
    )


async def _audit_local_image(path: Path) -> tuple[bool | None, str]:
    """用视觉模型审查本地图片。返回 (是否安全, 原因)；审查通道不可用/异常时 safe=None。"""
    client = _local_llm_client()
    if client is None:
        return None, "未配置视觉审查通道"
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        resp = await client.chat.completions.create(
            model=ai_config.ai_vision_model or ai_config.ai_model,
            messages=[
                {"role": "system", "content": _NSFW_AUDIT_SYS},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ],
                },
            ],
            max_tokens=160,
        )
        out = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return None, "审查返回格式异常"
        data = json.loads(m.group(0))
        return bool(data.get("safe")), str(data.get("reason") or "")
    except Exception as e:
        logger.warning("本地图片审查失败: %s", e)
        return None, str(e)[:100]


async def _local_generate(
    bot: Bot, event: MessageEvent, text: str, allow_nsfw: bool,
    size: tuple[int, int] | None = None, steps: int | None = None,
) -> None:
    """/生图 -local：调用本机 sd.cpp（SD1.5）CPU 推理出图并发送。

    - 单张约占满双核，全局互斥防并发；
    - 默认 384x384 / 15 步，可用 -s 宽x高 改规模、-step 次数 改迭代步数，
      耗时按像素数 × 步数线性估算并写进提示语；--params-backend disk 控制峰值内存；
    - 非 -nsfw 时出图后经视觉模型审查，不通过则丢弃不发送；
    - 管理员可用 /停生图 打断（SIGTERM，8 秒未退出则 SIGKILL）。
    """
    global _LOCAL_SD_PROC, _LOCAL_SD_CANCEL
    if not Path(LOCAL_SD_SH).exists() or not Path(LOCAL_SD_MODEL).exists():
        await image_cmd.finish("本机 sd.cpp 尚未部署（缺 sd.sh 或模型），请联系根管理员。", at_sender=True)
    if await _reply_ref_images(bot, event):
        await image_cmd.finish("本地 SD1.5 暂只支持文生图，引用图片的图生图请换云端机型（去掉 -local）～", at_sender=True)
    if _LOCAL_SD_LOCK.locked():
        await image_cmd.finish("🖥️ 本地正在生成上一张（约 20-30 分钟/张），请稍后再来～", at_sender=True)

    width, height = size or (_LOCAL_SD_WIDTH, _LOCAL_SD_HEIGHT)
    step_n = steps or _LOCAL_SD_STEPS
    eta_min = _local_eta_min(width, height, step_n)

    # 全程持有互斥锁：占锁期间的任何 finish 都会随异常释放锁
    async with _LOCAL_SD_LOCK:
        _LOCAL_SD_CANCEL = False
        prompt_en = await _local_prompt_en(text)
        if prompt_en is None:
            await image_cmd.finish(
                "✋ 翻译中文描述的那台云端模型拒绝处理这条内容，没法译成英文给本地 SD。\n"
                "请直接用英文描述（纯英文会跳过翻译直通出图），或换个中文说法再试。",
                at_sender=True,
            )
        placeholder = await _send_hint(
            bot, event,
            f"🖥️ 本地画布铺开…… {width}x{height} / {step_n} 步，CPU 推理约需 {eta_min} 分钟，出图后我会发出来",
        )
        if _LOCAL_SD_CANCEL:  # 画布准备阶段收到打断 → 不启动 sd，直接退出
            await _recall_hint(bot, placeholder)
            await image_cmd.finish("🛑 本地生图已取消。", at_sender=True)
        out = IMG_DIR / f"img_local_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        log_path = Path("/tmp") / f"sd_local_{out.stem}.log"

        cmd = [LOCAL_SD_SH,
               "-m", LOCAL_SD_MODEL,
               "-p", prompt_en,
               "--cfg-scale", "7",
               "--steps", str(step_n),
               "-t", "2",
               "-W", str(width), "-H", str(height),
               "--params-backend", "disk",
               "--seed", str(random.randrange(1, 2 ** 31)),
               "-o", str(out)]
        if Path("/usr/bin/nice").exists():
            cmd = ["/usr/bin/nice", "-n", "10", *cmd]

        try:
            with log_path.open("wb") as lf:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=lf, stderr=asyncio.subprocess.STDOUT)
                _LOCAL_SD_PROC = proc
                await proc.wait()
        except Exception as e:
            _LOCAL_SD_PROC = None
            logger.warning("本地生图启动失败: %s", e)
            await _recall_hint(bot, placeholder)
            await image_cmd.finish("本地推理启动失败了……（服务端异常）", at_sender=True)

        if _LOCAL_SD_PROC is proc:
            _LOCAL_SD_PROC = None
        if _LOCAL_SD_CANCEL:  # 用户 /停生图 打断：丢弃残片后退出
            try:
                out.unlink()
            except OSError:
                pass
            await _recall_hint(bot, placeholder)
            await image_cmd.finish("🛑 本地生图已打断，内存已释放～", at_sender=True)

        if not out.exists() or out.stat().st_size == 0:
            await _recall_hint(bot, placeholder)
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="ignore")[-500:]
            except Exception:
                pass
            await image_cmd.finish(
                "本地出图失败了（可能内存不足导致进程被回收）。\n" + (f"末尾日志：\n{tail}" if tail else "")
            )

        if not allow_nsfw:
            ok, reason = await _audit_local_image(out)
            if not ok:
                try:
                    out.unlink()
                except Exception:
                    pass
                await _recall_hint(bot, placeholder)
                if ok is None:
                    await image_cmd.finish(
                        f"本地图已生成，但内容审查通道暂不可用（{reason}），为安全起见未发送，"
                        "可请根管理员用 -nsfw 直出。", at_sender=True,
                    )
                await image_cmd.finish(f"本地图已生成，但未通过内容审查（{reason}），已丢弃。", at_sender=True)

        await _recall_hint(bot, placeholder)
        await image_cmd.finish(MessageSegment.image(file=f"{IMG_DIR_CONTAINER}/{out.name}"))


# ---------- /停生图：打断正在进行的本地生图（仅管理员）----------
stop_cmd = on_command("停生图", aliases={"打断生图", "取消生图"}, priority=1, block=True)
register_help("/停生图", "打断正在进行中的本地生图（仅管理员）：发 /停生图 即可，几秒内终止并释放内存")
hide_help("/停生图")


async def _sd_ensure_dead(proc: asyncio.subprocess.Process, delay: float = 8.0) -> None:
    """SIGTERM 后若进程仍存活则延迟补 SIGKILL，防止 sd-cli 未及时响应。"""
    await asyncio.sleep(delay)
    if proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


@stop_cmd.handle()
async def stop_local_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await stop_cmd.finish("仅管理员可以打断本地生图哦。", at_sender=True)
    if not _LOCAL_SD_LOCK.locked():
        await stop_cmd.finish("当前没有正在进行中的本地生图～", at_sender=True)

    global _LOCAL_SD_CANCEL
    _LOCAL_SD_CANCEL = True
    proc = _LOCAL_SD_PROC
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        asyncio.create_task(_sd_ensure_dead(proc))
        await stop_cmd.finish("🛑 正在打断本地生图，预计几秒内终止并释放内存……", at_sender=True)
    await stop_cmd.finish("🛑 已登记打断，正在准备画布的任务会直接取消～", at_sender=True)