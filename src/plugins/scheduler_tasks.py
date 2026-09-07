"""定时消息：从 data/schedule.json 读取 cron 定时任务

格式:
{
  "tasks": [
    {"name": "早安", "cron": "0 8 * * *", "groups": [群号1, 群号2], "message": "文本"}
  ]
}
"""
import logging

from apscheduler.triggers.cron import CronTrigger
from nonebot import get_bots
from nonebot_plugin_apscheduler import scheduler

from .common import DATA_DIR, load_json

logger = logging.getLogger("sorting_hat.scheduler")

SCHEDULE_FILE = DATA_DIR / "schedule.json"


async def _send_scheduled(groups: list[int], message: str) -> None:
    if not groups:
        return
    bots = list(get_bots().values())
    if not bots:
        logger.warning("定时消息：当前没有可用的 bot 连接")
        return
    bot = bots[0]
    for gid in groups:
        try:
            await bot.send_group_msg(group_id=gid, message=message)
            logger.info("定时消息已发送: group=%s", gid)
        except Exception:
            logger.exception("定时消息发送失败: group=%s", gid)


def setup_jobs() -> None:
    data = load_json(SCHEDULE_FILE)
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    for task in tasks:
        cron = task.get("cron", "")
        if not cron:
            logger.warning("定时任务 %s 缺少 cron 表达式，已跳过", task.get("name"))
            continue
        try:
            trigger = CronTrigger.from_crontab(cron)
        except Exception as e:
            logger.error("定时任务 %s 的 cron 解析失败: %s", task.get("name"), e)
            continue
        scheduler.add_job(
            _send_scheduled,
            trigger,
            args=[task.get("groups", []), task.get("message", "")],
            id=f"sorting_hat_{task.get('name', 'unnamed')}",
            replace_existing=True,
        )
        logger.info("已加载定时任务: %s @ %s", task.get("name"), cron)


setup_jobs()
