"""邦多利图库：/邦多利 从 bandori.party 实时随机抓取一张卡片原图

- 不预爬图库：每次请求实时抓取一张
- 全站等概率：先探测全站卡片总数（缓存 6 小时），再对全部卡片均匀随机取一张，
  不受网站"点击加载更多"分页影响
- 原图：i.bandori.party 的 PNG 原图（普通卡 / 觉醒卡随机取其一）
"""
import asyncio
import json
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.log import logger

from .common import DATA_DIR, QA_IMG_DIR, register_help

QA_IMG_MOUNT = "/app/napcat/qa_images"  # 对应宿主机 QA_IMG_DIR
API_URL = "https://kw.cocomi.eu.org/https://bandori.party/ajax/cards/"
HEADERS = {"User-Agent": "Mozilla/5.0", "accept-language": "ja"}
PAGE_SIZE = 12          # 网站每页卡片数
CACHE_TTL = 6 * 3600    # 全站总数缓存时长（秒）
TOTAL_CACHE_FILE = DATA_DIR / "bandori_total.json"

bandori_cmd = on_command("邦多利", priority=1, block=True)
register_help("/邦多利", "从邦多利图站随机抓一张卡片原图（全站等概率）")

_probe_lock = asyncio.Lock()

# 卡片块切分与图片 URL 提取
CARD_SPLIT_RE = re.compile(r'<div class="col-md-6" data-item="card"')
ID_RE = re.compile(r'data-item-id="(\d+)"')
NORMAL_RE = re.compile(r"card-image normal\" style=\"background-image: url\('//([^']+\.png)'\)")
TRAINED_RE = re.compile(r"card-image trained\" style=\"background-image: url\('//([^']+\.png)'\)")


def _parse_cards(html: str) -> list[dict]:
    """解析 ajax 返回的 HTML 片段，提取每张卡的 id 与图片 URL 列表（普通卡 + 觉醒卡）。"""
    cards = []
    for block in CARD_SPLIT_RE.split(html)[1:]:
        m = ID_RE.search(block)
        if not m:
            continue
        card_id = m.group(1)
        urls = []
        n = NORMAL_RE.search(block)
        t = TRAINED_RE.search(block)
        if n:
            urls.append("https://" + n.group(1))
        if t:
            urls.append("https://" + t.group(1))
        if urls:
            cards.append({"id": card_id, "urls": urls})
    return cards


async def _fetch_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    resp = await client.get(
        API_URL,
        params={"page": page, "ordering": "release_date,id", "reverse_order": "on"},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_cards(resp.text)


async def _load_total() -> dict:
    """读取缓存的全站卡片信息；过期则返回空。"""
    try:
        data = json.loads(TOTAL_CACHE_FILE.read_text("utf-8"))
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data
    except Exception:
        pass
    return {}


def _cleanup_old(fdir, max_age: float = 3600) -> None:
    """清理超过 max_age 秒的旧图片文件，避免无限堆积。"""
    now = time.time()
    try:
        for f in fdir.iterdir():
            if f.is_file() and now - f.stat().st_mtime > max_age:
                f.unlink(missing_ok=True)
    except Exception:
        pass


async def _probe_total(client: httpx.AsyncClient) -> dict:
    """指数搜索 + 二分查找最后一页，算出全站卡片总数。"""
    async def count(page: int) -> int:
        return len(await _fetch_page(client, page))

    lo, hi = 1, 1
    while hi <= 4096:
        if await count(hi) > 0:
            lo = hi
            hi *= 2
        else:
            break
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if await count(mid) > 0:
            lo = mid
        else:
            hi = mid
    last_count = await count(lo)
    data = {
        "ts": time.time(),
        "total": (lo - 1) * PAGE_SIZE + last_count,
        "last_page": lo,
        "last_count": last_count,
    }
    try:
        TOTAL_CACHE_FILE.write_text(json.dumps(data), "utf-8")
    except Exception as e:
        logger.warning(f"bandori: 总数缓存写入失败: {e}")
    return data


async def fetch_bandori_card() -> Path | None:
    """从 bandori.party 随机抓一张卡图并保存到 QA_IMG_DIR/bandori/，返回文件路径。"""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1. 获取全站卡片总数（缓存优先）
        total_data = await _load_total()
        if not total_data:
            async with _probe_lock:
                total_data = await _load_total()
                if not total_data:
                    total_data = await _probe_total(client)
        total = total_data.get("total", 0)
        if total <= 0:
            return None

        # 2. 全站等概率选一张卡，下载原图
        for attempt in range(3):
            k = random.randrange(total)
            page = k // PAGE_SIZE + 1
            idx = k % PAGE_SIZE
            try:
                cards = await _fetch_page(client, page)
            except Exception as e:
                logger.warning(f"bandori: 第{page}页抓取失败: {e}")
                await asyncio.sleep(1)
                continue
            if not cards:
                # 网站可能更新了卡片，重刷总数后重试
                async with _probe_lock:
                    total_data = await _probe_total(client)
                total = total_data.get("total", 0)
                if total <= 0:
                    break
                continue
            card = cards[idx] if idx < len(cards) else random.choice(cards)
            url = random.choice(card["urls"])
            try:
                fname = f"bandori_{card['id']}_{uuid.uuid4().hex[:6]}.png"
                fdir = QA_IMG_DIR / "bandori"
                fdir.mkdir(parents=True, exist_ok=True)
                _cleanup_old(fdir)  # 顺带清理超过 1 小时的旧图
                fpath = fdir / fname
                resp = await client.get(url, timeout=25)
                resp.raise_for_status()
                fpath.write_bytes(resp.content)
                return fpath
            except Exception as e:
                logger.warning(f"bandori: 图片下载失败: {e}")
                await asyncio.sleep(1)
    return None


@bandori_cmd.handle()
async def bandori_handler(bot: Bot, event: MessageEvent):
    fpath = await fetch_bandori_card()
    if fpath is None:
        await bandori_cmd.finish("邦多利图站请求失败，稍后再试～")
    # 保留文件以便 NapCat 读图，不立即删除（避免异步读图时文件已消失导致图片发送失败）
    msg_id = await bot.send(event, Message(MessageSegment.image(f"{QA_IMG_MOUNT}/bandori/{fpath.name}")))
    logger.info(f"bandori: 已发送卡片 -> {fpath.name} msg_id={msg_id}")
