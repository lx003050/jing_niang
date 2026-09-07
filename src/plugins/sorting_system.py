"""今日分院系统

- /今日分院：每天一次分院。可选四大院或阿兹卡班
  - 接入 AI：由 AI 结合对话上下文判断学院
  - 未接 AI：程序随机（阿兹卡班 5%，其余四院概率相同）
  - 抽到阿兹卡班需服刑期满后才能再次分院（默认 3 天）
- /判刑 <天数> @某人：OP 将某人送入阿兹卡班（1~30 天）
- /赦免 @某人：OP 提前释放某人

分院状态持久化在 data/sorting_state.json；每次分院附带一张合成图片：
被分院人头像 + 五大标志（四大学院 + 阿兹卡班）。
"""
import asyncio
import json
import logging
import random
import re
import time
from datetime import date
from pathlib import Path

import httpx
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)
from PIL import Image, ImageDraw, ImageFont

from .admin_tools import OP_SEED, is_op
from .common import DATA_DIR, ai_config, hide_help, register_help
from .sorting_hat import _memory, _get_client, _load_system_prompt, _session_key

logger = logging.getLogger("sorting_hat.daily_sort")

STATE_FILE = DATA_DIR / "sorting_state.json"
AVATAR_DIR = DATA_DIR / "avatars"
CREST_DIR = DATA_DIR / "crests"

CARDS_DIR = DATA_DIR / "cards"          # 卡片实际保存目录（宿主机）
CARDS_MOUNT = "/app/napcat/cards"       # 容器内挂载路径（对应宿主机 CARDS_DIR）

# 四大学院 + 阿兹卡班的标示图（来自 Harry Potter Fandom 图库，透明底 PNG）
_CREST_FILES = {
    "格兰芬多": "gryffindor", "赫奇帕奇": "hufflepuff",
    "拉文克劳": "ravenclaw", "斯莱特林": "slytherin", "阿兹卡班": "azkaban",
}
_CREST_URLS = {
    "格兰芬多": "https://static.wikia.nocookie.net/harrypotter/images/2/28/Gryffindor_ClearBG2.png/revision/latest?cb=20160802131909",
    "赫奇帕奇": "https://static.wikia.nocookie.net/harrypotter/images/4/4f/Hufflepuff-crest-transparent.png/revision/latest?cb=20200824173153",
    "拉文克劳": "https://static.wikia.nocookie.net/harrypotter/images/7/71/Ravenclaw_ClearBG.png/revision/latest?cb=20161020182442",
    "斯莱特林": "https://static.wikia.nocookie.net/harrypotter/images/e/e7/Slytherin_House_Crest_transparent.png/revision/latest?cb=20200824173151",
    "阿兹卡班": "https://static.wikia.nocookie.net/harrypotter/images/5/55/Azkaban_%28digitally_altered%29.png/revision/latest?cb=20200531223213",
}
_CREST_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
_crests_ready = False

AZKABAN_DAYS_DEFAULT = 3    # 抽中阿兹卡班时的默认刑期（天）
AZKABAN_MAX_DAYS = 30       # 判刑上限
AZKABAN_RANDOM_P = 0.05     # 无 AI 时抽中阿兹卡班的概率

HOUSES = ["格兰芬多", "赫奇帕奇", "拉文克劳", "斯莱特林"]
ALL_HOUSES = HOUSES + ["阿兹卡班"]

HOUSE_INFO = {
    "格兰芬多": {"color": (174, 32, 41), "symbol": "狮", "desc": "勇敢、果决"},
    "赫奇帕奇": {"color": (232, 180, 26), "symbol": "獾", "desc": "诚实、忠诚"},
    "拉文克劳": {"color": (34, 79, 133), "symbol": "鹰", "desc": "聪慧、睿智"},
    "斯莱特林": {"color": (28, 84, 52), "symbol": "蛇", "desc": "野心、精明"},
    "阿兹卡班": {"color": (48, 48, 60), "symbol": "锁", "desc": "禁闭服刑"},
}

_lock = asyncio.Lock()


# ---------- 状态存取 ----------
def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"users": {}}
    except Exception:
        return {"users": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _user_state(state: dict, group_id: int, uid: int) -> dict:
    """获取 (群, 用户) 的分院状态；不同群各自独立。

    users 结构：{群号: {QQ号: {house, sorted_on, release_ts, sentence}}}。
    """
    users = state.setdefault("users", {})
    _migrate_users(users)
    groups = users.setdefault(str(group_id), {})
    return groups.setdefault(
        str(uid),
        {"house": "", "sorted_on": "", "release_ts": 0, "sentence": 0, "group_id": group_id},
    )


def _migrate_users(users: dict) -> None:
    """把旧格式 users[uid] -> 条目，一次性迁移为 users[群号][uid] -> 条目。"""
    sample = next(iter(users.values()), None)
    if isinstance(sample, dict) and "house" in sample:  # 旧格式（条目层有 house 字段）
        migrated: dict[str, dict] = {}
        for uid_str, entry in users.items():
            gid = str(entry.get("group_id") or 0)
            migrated.setdefault(gid, {})[uid_str] = entry
        users.clear()
        users.update(migrated)


# ---------- 分院逻辑 ----------
def _random_sort() -> tuple[str, int, str]:
    """无 AI：纯随机，阿兹卡班 5%，其余四院概率相同。返回 (学院, 刑期, 依据)。"""
    if random.random() < AZKABAN_RANDOM_P:
        return "阿兹卡班", AZKABAN_DAYS_DEFAULT, ""
    return random.choice(HOUSES), 0, ""


async def _ai_sort(event: MessageEvent) -> tuple[str, int, str]:
    """有 AI：结合对话上下文由 AI 判断并给出依据；失败则回退随机。"""
    client = _get_client()
    if client is None:
        return _random_sort()

    key = _session_key(event)
    history = _memory.get(key, [])[-8:]
    try:
        resp = await client.chat.completions.create(
            model=ai_config.ai_model,
            messages=[
                {"role": "system", "content": _load_system_prompt()},
                *history,
                {"role": "user", "content": (
                    "（今日分院请求）请结合我们刚才的对话，判断这位巫师应去哪个学院："
                    "格兰芬多、赫奇帕奇、拉文克劳、斯莱特林。\n"
                    "只有对方确实严重违规（如恶意刷屏、辱骂、骚扰）时，才可以判他去阿兹卡班"
                    "（默认服刑 3 天）。\n"
                    "回答格式：先写学院名，换行后再用一句话说明判断依据（30 字以内，自然一点）。"
                )},
            ],
            temperature=0.9,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("AI 分院失败，回退随机分院")
        return _random_sort()

    house = next((h for h in ALL_HOUSES if h in text), None)
    if house is None:
        return _random_sort()
    # 提取判断依据：去掉学院名后的剩余文字
    reason = text.replace(house, "", 1)
    reason = re.sub(r"^\s*[：:，,。.、\-\n]*\s*", "", reason)
    reason = re.sub(r"\s+", " ", reason).strip()[:80]
    days = AZKABAN_DAYS_DEFAULT if house == "阿兹卡班" else 0
    return house, days, reason


# ---------- /今日分院 /分院 ----------
sort_cmd = on_command("今日分院", aliases={"分院"}, priority=1, block=True)
register_help("/今日分院", "每天一次分院（含阿兹卡班）+ 全群总览图；消息提到『分院』也会触发")


async def _today_sort_flow(matcher, bot: Bot, event: MessageEvent) -> None:
    """执行 /今日分院 核心流程：判断 + 落库 + 发送总览图。"""
    uid = event.user_id
    today_str = date.today().isoformat()
    now = time.time()
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0

    early: tuple[str, str] | None = None  # (提示文本, 高亮学院)，非 None 表示今天不能新分
    async with _lock:
        state = _load_state()
        u = _user_state(state, group_id, uid)

        # 阿兹卡班服刑中
        if u.get("release_ts", 0) > now:
            left = max(1, int((u["release_ts"] - now) / 86400) + 1)
            early = (f"你还在阿兹卡班服刑中，剩余 {left} 天。刑满之前，鲸娘可不敢给你分院。", "阿兹卡班")
        else:
            if u.get("release_ts", 0):
                u["release_ts"], u["sentence"] = 0, 0  # 刑满自动释放
            # 每日一次
            if u.get("sorted_on") == today_str:
                cur = u.get("house") or ""
                early = (f"你今天已经分过啦：{cur}。想换学院的话，明天再来找我。", cur)

        if early is None:
            # 分院（AI 判断或随机）
            house, days, reason = (
                await _ai_sort(event) if ai_config.ai_enabled else _random_sort()
            )
            # 落库
            u["house"] = house
            u["sorted_on"] = today_str
            u["group_id"] = group_id
            if house == "阿兹卡班":
                u["release_ts"] = now + days * 86400
                u["sentence"] = days
            else:
                u["release_ts"], u["sentence"] = 0, 0
            _save_state(state)

            # 让 AI 对话记住分院结果
            hist = _memory.setdefault(_session_key(event), [])
            note = house + (f"（依据：{reason}）" if reason else "")
            if house == "阿兹卡班":
                note += f"（服刑 {days} 天）"
            hist.append({"role": "user", "content": "（今日分院）"})
            hist.append({"role": "assistant", "content": note})
            if len(hist) > ai_config.ai_max_history:
                _memory[_session_key(event)] = hist[-ai_config.ai_max_history :]

    # 早退分支：今天不能新分，仍给出总览图
    if early:
        img_path = await _make_sorting_image(group_id, uid, early[1])
        await matcher.finish(
            MessageSegment.text(early[0] + "\n") + MessageSegment.image(str(img_path)),
            at_sender=True,
        )

    # 正常流程：合成总览图（本群所有已分院成员）
    img_path = await _make_sorting_image(group_id, uid, house)

    nickname = event.sender.card or event.sender.nickname or str(uid)
    if house == "阿兹卡班":
        text = f"{nickname} 被关进了阿兹卡班！服刑 {days} 天，好好反省吧。"
    else:
        text = f"{nickname} 今日分院结果：{house}！"
    if reason:
        text += f"\n判断依据：{reason}"
    await matcher.finish(
        MessageSegment.text(text + "\n") + MessageSegment.image(str(img_path)),
        at_sender=True,
    )


@sort_cmd.handle()
async def today_sort_handler(bot: Bot, event: MessageEvent):
    await _today_sort_flow(sort_cmd, bot, event)


# ---------- 模糊触发：@机器人 的消息里提到「分院」即触发 ----------
def _fuzzy_sort_rule(event: MessageEvent) -> bool:
    # 仅当 @机器人（鲸娘）时才做模糊识别；文本里单纯出现「分院帽/鲸娘」名字不算
    if not isinstance(event, GroupMessageEvent) or not event.to_me:
        return False
    text = event.get_plaintext().strip()
    if text.startswith("/"):
        return False
    return "分院" in text.replace("分院帽", "").replace("鲸娘", "")


fuzzy_sort_matcher = on_message(rule=_fuzzy_sort_rule, priority=0, block=True)


@fuzzy_sort_matcher.handle()
async def fuzzy_sort_handler(bot: Bot, event: MessageEvent):
    await _today_sort_flow(fuzzy_sort_matcher, bot, event)


# ---------- 阿兹卡班服刑中：静默忽略其所有指令（根管理员除外） ----------
def _is_serving(uid: int, group_id: int = 0) -> bool:
    """用户当前是否正在该群阿兹卡班服刑（各群独立）。"""
    state = _load_state()
    users = state.get("users", {})
    _migrate_users(users)
    u = users.get(str(group_id), {}).get(str(uid))
    return bool(u and u.get("release_ts", 0) > time.time())


def _prison_block_rule(event: MessageEvent) -> bool:
    if event.user_id == OP_SEED:  # 根管理员不受限
        return False
    text = event.get_plaintext().lstrip()
    if not text.startswith("/"):
        return False
    group_id = getattr(event, "group_id", 0)
    return _is_serving(event.user_id, group_id)


prison_block = on_message(rule=_prison_block_rule, priority=0, block=True)


@prison_block.handle()
async def prison_block_handler(bot: Bot, event: MessageEvent):
    pass  # 静默忽略：不回复、不处理


# ---------- AI 自主判刑（供 sorting_hat 调用）----------
AI_SENTENCE_COOLDOWN = 600   # AI 判刑同一目标的冷却（秒）
_ai_sentence_cd: dict[int, float] = {}


async def sentence_user(target_uid: int, days: int, group_id: int = 0) -> tuple[bool, str]:
    """把用户关进阿兹卡班。供 AI 自主调用，含根管理员保护与冷却。返回 (是否成功, 说明)。"""
    if target_uid == OP_SEED:
        return False, "根管理员不能被关进阿兹卡班"
    if target_uid <= 0:
        return False, "无效的 QQ 号"
    days = max(1, min(days, AZKABAN_MAX_DAYS))
    now = time.time()
    if now - _ai_sentence_cd.get(target_uid, 0) < AI_SENTENCE_COOLDOWN:
        left = int(AI_SENTENCE_COOLDOWN - (now - _ai_sentence_cd[target_uid]))
        return False, f"该用户刚被关过，约 {left} 秒后才能再判"
    _ai_sentence_cd[target_uid] = now
    async with _lock:
        state = _load_state()
        u = _user_state(state, group_id, target_uid)
        u["house"] = "阿兹卡班"
        u["release_ts"] = time.time() + days * 86400
        u["sentence"] = days
        if group_id:
            u["group_id"] = group_id
        _save_state(state)
    return True, f"已将 QQ {target_uid} 关进阿兹卡班服刑 {days} 天"


# ---------- OP：/判刑 ----------
sentence_cmd = on_command("判刑", priority=1, block=True)
register_help("/判刑", "判入阿兹卡班 N 天（仅管理员）")
hide_help("/判刑")


@sentence_cmd.handle()
async def sentence_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await sentence_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target, days = _parse_at_and_number(event)
    if target is None:
        target = event.user_id  # 缺省 @ 时默认判自己
    if days <= 0:
        await sentence_cmd.finish("用法：/判刑 <天数> @某人（不 @ 则判自己）", at_sender=True)
    days = min(days, AZKABAN_MAX_DAYS)

    async with _lock:
        state = _load_state()
        u = _user_state(state, getattr(event, "group_id", 0), target)
        u["house"] = "阿兹卡班"
        u["release_ts"] = time.time() + days * 86400
        u["sentence"] = days
        _save_state(state)
    await sentence_cmd.finish(f"已判处 QQ {target} 阿兹卡班 {days} 天。", at_sender=True)


# ---------- OP：/赦免 ----------
pardon_cmd = on_command("赦免", priority=1, block=True)
register_help("/赦免", "提前释放某人（仅管理员）")
hide_help("/赦免")


@pardon_cmd.handle()
async def pardon_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await pardon_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认赦免自己
    if target == OP_SEED and event.user_id != OP_SEED:
        await pardon_cmd.finish("根管理员的判刑不能被别的管理员赦免。", at_sender=True)

    async with _lock:
        state = _load_state()
        u = _user_state(state, getattr(event, "group_id", 0), target)
        u["release_ts"], u["sentence"] = 0, 0
        _save_state(state)
    await pardon_cmd.finish(f"已赦免 QQ {target}，提前释放出狱。", at_sender=True)


def _extract_at(event: MessageEvent) -> int | None:
    """提取消息中的 @目标 QQ；缺省时返回 None。"""
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq and qq != "all":
                return int(qq)
    return None


def _parse_at_and_number(event: MessageEvent) -> tuple[int | None, int]:
    """从指令消息中提取 @目标 和 第一个数字。"""
    target = _extract_at(event)
    texts = "".join(seg.data.get("text", "") for seg in event.message if seg.type == "text")
    m = re.search(r"\d+", texts)
    days = int(m.group()) if m else 0
    return target, days


# ---------- OP：/重新分院 ----------
redistribute_cmd = on_command("重新分院", priority=1, block=True)
register_help("/重新分院", "给某人一次重新分院的机会（仅管理员）")
hide_help("/重新分院")


@redistribute_cmd.handle()
async def redistribute_handler(bot: Bot, event: MessageEvent):
    if not is_op(event.user_id):
        await redistribute_cmd.finish("你不是管理员，没有这个权限。", at_sender=True)
    target = _extract_at(event) or event.user_id  # 缺省 @ 时默认重新分院自己
    if target == OP_SEED and event.user_id != OP_SEED:
        await redistribute_cmd.finish("根管理员的事不用别的管理员操心。", at_sender=True)

    async with _lock:
        state = _load_state()
        u = _user_state(state, getattr(event, "group_id", 0), target)
        u["sorted_on"] = ""                    # 清除今日记录，可再次分院
        u["release_ts"], u["sentence"] = 0, 0  # 若在服刑，一并释放
        _save_state(state)
    await redistribute_cmd.finish(
        f"已给 QQ {target} 一次重新分院的机会，现在可以再次使用 /今日分院 了。",
        at_sender=True,
    )


# ---------- 图片合成 ----------
def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        # Windows
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        # Linux / Ubuntu（Noto Sans CJK、文泉驿微米黑等）
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _open_avatar(path: Path) -> Image.Image:
    """读取头像并去掉透明通道（透明区域用白色填充，避免黑底）。"""
    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


async def _download_avatar(uid: int) -> Image.Image | None:
    """下载并缓存 QQ 头像（缓存 24 小时）。"""
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = AVATAR_DIR / f"{uid}.png"
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        try:
            return _open_avatar(path)
        except Exception:
            pass
    url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        path.write_bytes(resp.content)
        return _open_avatar(path)
    except Exception:
        logger.warning("头像下载失败: uid=%s", uid)
        return None


def _paste_round_avatar(
    img: Image.Image, avatar: Image.Image | None, cx: int, cy: int, r: int
) -> None:
    """把头像以圆形粘贴到 (cx, cy)，r 为半径；无头像时画占位圆。"""
    if avatar is None:
        draw = ImageDraw.Draw(img)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(185, 175, 155), outline=(198, 160, 70), width=3)
        return
    av = avatar.resize((r * 2, r * 2))
    mask = Image.new("L", (r * 2, r * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, r * 2, r * 2), fill=255)
    img.paste(av, (cx - r, cy - r), mask)


def _crest_ok(house: str) -> bool:
    """院徽文件是否存在且是合法图片。"""
    path = CREST_DIR / f"{_CREST_FILES[house]}.png"
    if not path.exists():
        return False
    try:
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


async def _ensure_crests() -> None:
    """确保五大标示图已下载到本地（缺失或损坏时自动从图库拉取）。"""
    global _crests_ready
    if _crests_ready:
        return
    CREST_DIR.mkdir(parents=True, exist_ok=True)
    missing = [h for h in ALL_HOUSES if not _crest_ok(h)]
    if missing:
        try:
            async with httpx.AsyncClient(headers=_CREST_UA, timeout=30,
                                         follow_redirects=True) as client:
                for h in missing:
                    try:
                        resp = await client.get(_CREST_URLS[h])
                        resp.raise_for_status()
                        path = CREST_DIR / f"{_CREST_FILES[h]}.png"
                        path.write_bytes(resp.content)
                        if not _crest_ok(h):
                            path.unlink(missing_ok=True)
                            logger.warning("院徽内容无效，已删除: %s", h)
                    except Exception:
                        logger.warning("院徽下载失败: %s", h)
        except Exception:
            logger.warning("院徽批量下载失败")
    _crests_ready = True


def _get_crest(house: str) -> Image.Image | None:
    """读取本地缓存的标示图（RGBA，并裁剪掉四周透明留白），缺失返回 None。"""
    path = CREST_DIR / f"{_CREST_FILES[house]}.png"
    if path.exists():
        try:
            im = Image.open(path).convert("RGBA")
            bbox = im.getchannel("A").getbbox()
            if bbox:
                im = im.crop(bbox)
            return im
        except Exception:
            return None
    return None


def _draw_house_box(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    avatars: dict[int, Image.Image | None],
    member_uids: list[int],
    house: str,
    bx: int, by: int, bw: int, bh: int,
    selected: bool,
) -> None:
    """画一个学院象限圆角框：左上角为院徽，框内以 3 列网格展示成员头像。"""
    color = HOUSE_INFO[house]["color"]
    fill = tuple(min(255, c + 78) for c in color)
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=18,
                           fill=fill, outline=color, width=3)
    if selected:
        draw.rounded_rectangle([bx - 4, by - 4, bx + bw + 4, by + bh + 4],
                               radius=22, outline=(198, 160, 70), width=4)

    # 院徽（缺失时回退为手绘盾牌）
    crest = _get_crest(house)
    name_x = bx + 18
    if crest:
        crest_h = 64
        w, h = crest.size
        cw = max(1, int(w * crest_h / h))
        if cw > 110:  # 过宽时按宽度压缩，避免超出圆角框
            cw = 110
            crest_h = max(1, int(h * cw / w))
        crest = crest.resize((cw, crest_h), Image.LANCZOS)
        img.paste(crest, (bx + 18, by + 12), crest)
        name_x = bx + 18 + cw + 14
    else:
        cy = by + 44
        sh = 54
        pts = [
            (bx + 18, cy - sh * 0.62), (bx + 18, cy + sh * 0.05),
            (bx + 18 + 42, cy + sh * 0.80), (bx + 18 + 84, cy + sh * 0.05),
            (bx + 18 + 84, cy - sh * 0.62), (bx + 18 + 42, cy - sh * 0.84),
        ]
        draw.polygon(pts, fill=color)
        draw.polygon(pts, outline=(255, 244, 205), width=2)
        symbol = HOUSE_INFO[house]["symbol"]
        f_sym = _find_font(46, bold=True)
        draw.text((bx + 60 - draw.textlength(symbol, font=f_sym) / 2, cy - 26),
                  symbol, font=f_sym, fill=(255, 255, 255))

    f_h = _find_font(28, bold=True)
    draw.text((name_x, by + 26), house, font=f_h, fill=color)
    if selected:
        f_tag = _find_font(20, bold=True)
        tag = "★ 你在这里"
        draw.text((bx + bw - draw.textlength(tag, font=f_tag) - 14, by + 12),
                  tag, font=f_tag, fill=(140, 30, 30))

    max_show, r, row_gap = 9, 26, 64
    start_y = by + 96
    cols = 3
    col_x = [bx + bw * (i + 1) / (cols + 1) for i in range(cols)]
    for i, uid in enumerate(member_uids[:max_show]):
        col, row = i % cols, i // cols
        _paste_round_avatar(img, avatars.get(uid), int(col_x[col]),
                            start_y + r + row * row_gap, r)
    if len(member_uids) > max_show:
        f_m = _find_font(24, bold=True)
        extra = f"+{len(member_uids) - max_show}"
        draw.text((bx + (bw - draw.textlength(extra, font=f_m)) / 2,
                   start_y + 3 * row_gap + 2), extra, font=f_m, fill=color)


async def _make_sorting_image(group_id: int, trigger_uid: int, trigger_house: str) -> Path:
    """合成「本群分院总览」图：
    四大学院各占一个象限圆角框（框内是本院全部已分院成员的圆头像），
    触发者的学院金色高亮；底部一条阿兹卡班监牢（铁栏造型）。
    """
    # 收集本群已分院成员（阿兹卡班只统计服刑中的）
    await _ensure_crests()
    async with _lock:
        state = _load_state()
        users = state.setdefault("users", {})
        _migrate_users(users)
        by_house: dict[str, list[int]] = {h: [] for h in ALL_HOUSES}
        now = time.time()
        for uid_str, u in users.get(str(group_id), {}).items():
            if not u.get("house"):
                continue
            h = u["house"]
            if h == "阿兹卡班":
                if u.get("release_ts", 0) <= now:
                    continue  # 已刑满，等待重新分院
                by_house["阿兹卡班"].append(int(uid_str))
            elif h in by_house:
                by_house[h].append(int(uid_str))

    # 并行下载头像
    all_uids = {uid for uids in by_house.values() for uid in uids}
    avatars_list = await asyncio.gather(*(_download_avatar(u) for u in all_uids))
    avatars = dict(zip(all_uids, avatars_list))

    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (244, 238, 224))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, W - 8, H - 8], outline=(60, 42, 16), width=6)
    draw.rectangle([18, 18, W - 18, H - 18], outline=(198, 160, 70), width=3)

    # 标题
    f_title = _find_font(46, bold=True)
    title = "本群分院总览"
    draw.text(((W - draw.textlength(title, font=f_title)) / 2, 30),
              title, font=f_title, fill=(60, 42, 16))

    # 2x2 象限
    gx, gy, gap, box_w, box_h = 24, 88, 14, (1000 - 48 - 14) // 2, 300
    positions = {
        "格兰芬多": (gx, gy),
        "赫奇帕奇": (gx + box_w + gap, gy),
        "拉文克劳": (gx, gy + box_h + gap),
        "斯莱特林": (gx + box_w + gap, gy + box_h + gap),
    }
    for h in HOUSES:
        bx, by = positions[h]
        _draw_house_box(draw, img, avatars, by_house[h], h,
                        bx, by, box_w, box_h, h == trigger_house)

    # 底部阿兹卡班监牢
    az_x, az_w = gx, box_w * 2 + gap
    az_y = gy + box_h * 2 + gap * 2 + 8
    az_h = 80
    az_uids = by_house["阿兹卡班"]
    selected = trigger_house == "阿兹卡班"
    draw.rounded_rectangle([az_x, az_y, az_x + az_w, az_y + az_h], radius=14,
                           fill=(34, 34, 42), outline=(120, 120, 132), width=3)
    if selected:
        draw.rounded_rectangle([az_x - 4, az_y - 4, az_x + az_w + 4, az_y + az_h + 4],
                               radius=18, outline=(198, 160, 70), width=4)

    # 阿兹卡班标示图（左上角，圆角裁剪）
    mark = _get_crest("阿兹卡班")
    title_x = az_x + 18
    if mark:
        mark_h = 56
        w, h = mark.size
        mw = max(1, int(w * mark_h / h))
        mark = mark.resize((mw, mark_h), Image.LANCZOS)
        mmask = Image.new("L", (mw, mark_h), 0)
        ImageDraw.Draw(mmask).rounded_rectangle((0, 0, mw, mark_h), radius=10, fill=255)
        img.paste(mark, (az_x + 14, az_y + 12), mmask)
        title_x = az_x + 14 + mw + 14

    f_az = _find_font(26, bold=True)
    draw.text((title_x, az_y + 10), "阿兹卡班", font=f_az, fill=(214, 208, 220))
    f_az2 = _find_font(20)
    draw.text((title_x, az_y + 44),
              f"囚犯 {len(az_uids)} 名" + (" · ★ 你在其中" if selected else ""),
              font=f_az2, fill=(160, 155, 170))

    # 囚犯头像（标题区右侧）
    avatar_start = max(title_x + 180, az_x + 300)
    strip_x = avatar_start
    for uid in az_uids[:8]:
        _paste_round_avatar(img, avatars.get(uid), strip_x, az_y + az_h // 2, 22)
        strip_x += 58
    if len(az_uids) > 8:
        f_m = _find_font(22, bold=True)
        draw.text((strip_x + 2, az_y + az_h // 2 - 14),
                  f"+{len(az_uids) - 8}", font=f_m, fill=(200, 195, 210))

    # 铁栏（只覆盖头像区，做出监牢效果）
    bar_top, bar_bot = az_y + 4, az_y + az_h - 4
    for bx2 in range(avatar_start + 4, az_x + az_w - 6, 22):
        draw.rectangle([bx2, bar_top, bx2 + 5, bar_bot], fill=(92, 92, 104))
    draw.rectangle([avatar_start + 4, az_y + az_h - 16, az_x + az_w - 6, az_y + az_h - 11],
                   fill=(70, 70, 80))

    # 保存到宿主机目录，返回容器内可访问的路径（NapCat 在容器中读取）
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"overview_{group_id}_{int(time.time())}.png"
    img.save(CARDS_DIR / filename)
    return f"{CARDS_MOUNT}/{filename}"
