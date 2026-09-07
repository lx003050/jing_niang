"""sorting_hat 分院帽 QQ 机器人启动入口

运行方式: python bot.py
"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter

# 初始化 NoneBot（读取 .env 配置）
nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(Adapter)

# 加载定时任务插件（scheduler_tasks.py 依赖）
nonebot.load_plugin("nonebot_plugin_apscheduler")

# 加载业务插件
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
