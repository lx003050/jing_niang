"""伊蕾娜图库：/伊蕾娜 随机发一张「今日份屑屑」图库中的图

- 默认发送压缩图（data/qa_images/irena_*.jpg）
- /伊蕾娜 -o 发送无损原图（data/qa_images/irena_orig/irena_*.*）

原图由 fetch_irena.py 爬取，压缩图由 compress_irena.py 生成。
"""
import random
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from .common import QA_IMG_DIR, register_help

QA_IMG_MOUNT = "/app/napcat/qa_images"  # 对应宿主机 QA_IMG_DIR

irena_cmd = on_command("伊蕾娜", priority=1, block=True)
register_help("/伊蕾娜", "随机发一张「今日份屑屑」图库中的伊蕾娜图；-o 发送无损原图")


async def pick_irena_path(use_orig: bool = False) -> Path | None:
    """随机取一张伊蕾娜图（use_orig=True 取无损原图），图库为空返回 None。"""
    if use_orig:
        files = sorted((QA_IMG_DIR / "irena_orig").glob("irena_*.*"))
    else:
        files = sorted(QA_IMG_DIR.glob("irena_*.*"))
    return random.choice(files) if files else None


@irena_cmd.handle()
async def irena_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    use_orig = "-o" in arg.extract_plain_text().lower()
    f = await pick_irena_path(use_orig)
    if f is None:
        await irena_cmd.finish("图库还是空的，等主人爬取「今日份屑屑」动态吧～")
    prefix = "irena_orig/" if use_orig else ""
    await bot.send(event, Message(MessageSegment.image(f"{QA_IMG_MOUNT}/{prefix}{f.name}")))
