"""网站镜像：/镜像 网址 [-Nd]

- /镜像 example.com            建立 1 天镜像
- /镜像 https://example.com    同上（https 目标站）
- /镜像 https://example.com/a/b  镜像指定页面（保留路径）
- /镜像 example.com -3d        根管理员可指定时长（默认 1 天，最长 365 天）
- /镜像代理 <网址>             经 SOCKS5 节点镜像（用于被墙站点，如 YouTube）

建立后返回访问地址 http://8.130.54.147/<域名>/，过期自动删除。
"""
import asyncio
import logging
import re

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

from .admin_tools import OP_SEED
from .common import register_help

logger = logging.getLogger("sorting_hat.mirror")

MIRROR_SCRIPT = "/srv/add_mirror.sh"
MIRROR_BASE = "http://8.130.54.147"
SOCKS_MIRROR_SCRIPT = "/srv/add_socks_mirror.sh"
MAX_HOURS = 365 * 24  # 根管理员最长可指定时长

mirror_cmd = on_command("镜像", priority=1, block=True)
socks_mirror_cmd = on_command("镜像代理", priority=1, block=True)
register_help("/镜像", "建立网站镜像（默认1天，过期自动删除）；根管理员可 /镜像 网址 -3d 指定天数")
register_help("/镜像代理", "经 SOCKS5 节点镜像被墙站点（如 YouTube），用法同 /镜像")


def _parse_args(text: str, user_id: int) -> tuple[str | None, str | None, int | None, str | None, bool]:
    """解析参数，返回 (host, path, hours, scheme, is_root)。

    path 为 URL 中 host 之后的路径（去掉 query/fragment），如
    https://github.com/a/b  -> host=github.com path=a/b
    hours 为 None 表示用默认 1 天；普通用户即使带了 -Nd 也会被重置为 None。
    """
    s = text.strip()
    hours = None
    m = re.search(r"-(\d+)\s*([dh])", s)
    if m:
        num = int(m.group(1))
        hours = num * 24 if m.group(2) == "d" else num
        s = s.replace(m.group(0), "")
    is_root = user_id == OP_SEED
    if hours is not None and not is_root:
        hours = None  # 普通用户不能指定时长，强制默认 1 天

    url = (s.split() or [""])[0]
    if not url:
        return None, None, None, None, is_root
    scheme = "http"
    m = re.match(r"^(https?)://", url)
    if m:
        scheme = m.group(1)
        url = url[m.end():]
    host, _, path = url.partition("/")
    host = host.strip().rstrip(".")
    if not re.match(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$", host):
        return None, None, None, None, is_root
    # 路径去掉 query/fragment，仅保留安全字符
    path = path.split("?")[0].split("#")[0]
    if path and not re.match(r"^[A-Za-z0-9./_~%+@\-]+$", path):
        return None, None, None, None, is_root
    return host, path, hours, scheme, is_root


@mirror_cmd.handle()
async def mirror_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    await _create_mirror(bot, event, MIRROR_SCRIPT, arg.extract_plain_text())


@socks_mirror_cmd.handle()
async def socks_mirror_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    await _create_mirror(bot, event, SOCKS_MIRROR_SCRIPT, arg.extract_plain_text())


async def _create_mirror(bot: Bot, event: MessageEvent, script: str, text: str):
    host, path, hours, scheme, is_root = _parse_args(text, event.user_id)
    if not host:
        tip = (
            "用法：/镜像 网址 [-Nd]\n"
            "例：/镜像 example.com\n"
            "　 /镜像 https://example.com -3d（根管理员可指定天数）\n"
            "　 /镜像代理 https://www.youtube.com/watch?v=xxx（经 SOCKS5 节点）"
        )
        await mirror_cmd.finish(tip, at_sender=True)
    if hours is None:
        hours = 24
        tip_ext = ""
    else:
        if hours > MAX_HOURS:
            await mirror_cmd.finish("最长只能指定 365 天哦～", at_sender=True)
        tip_ext = "（根管理员指定）" if hours > 24 else ""

    try:
        proc = await asyncio.create_subprocess_exec(
            script, host, str(hours), scheme,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = (out or b"").decode("utf-8", "ignore").strip()
    except Exception:
        logger.exception("镜像脚本执行失败")
        await mirror_cmd.finish("镜像服务暂时不可用，稍后再试～", at_sender=True)
    if not output.startswith("OK"):
        await mirror_cmd.finish(f"镜像建立失败：{output or '未知错误'}", at_sender=True)

    url = f"{MIRROR_BASE}/{host}/" if not path else f"{MIRROR_BASE}/{host}/{path}"
    days = round(hours / 24, 1)
    await mirror_cmd.finish(
        f"镜像已建立：{url}\n有效期 {days} 天（过期自动删除）{tip_ext}",
        at_sender=True,
    )
