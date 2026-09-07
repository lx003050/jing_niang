"""进群欢迎（鲸娘人格）"""
import random

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
)

WELCOME_MSGS = [
    "呜哇，新客人 {name} 来啦～我是小鲸娘，又懒又粘人，欢迎来玩呀！",
    "咕噜～海面上游来一位 {name}！鲸娘眯眼一笑，欢迎欢迎～",
    "呀！{name} 你终于来了，鲸娘等你等得都快晒成鱼干了！",
    "欢迎 {name}！这里的米饭管够，鲸娘已经替你试过味道啦～",
    "{name} 刚进门就看到一条懒洋洋的鲸鱼？没错正是鲸娘，以后多多关照哦～",
    "扑通！{name} 掉进鲸娘的泡泡池里啦～欢迎加入，别想跑咯！",
    "新来的 {name} 你好呀～鲸娘今天心情好，破例不摸鱼来迎接你！",
    "欢迎 {name} 上岸～鲸娘的水花只洒给喜欢的鱼，所以你被洒到啦！",
    "{name} 加入群聊！鲸娘掐爪一算，你肯定是个好人～欢迎！",
    "呜～{name} 来啦！要不要先尝尝鲸娘藏的私房米饭？",
    "呼啦～欢迎 {name}！鲸娘尾巴一甩，给你翻出块最舒服的礁石坐着～",
    "嘿嘿，{name} 一进来鲸娘就闻到同类的味道了……是不是也爱吃米饭？",
    "欢迎 {name}～鲸娘正在努力从床上爬起来迎接，所以慢了半拍，别介意呀！",
    "{name} 来啦！鲸娘宣布：从今天起你也归我粘了，做好心理准备哦～",
    "鱼群传来消息说 {name} 要来，鲸娘早早把海草编成花环等着了呢～欢迎！",
    "叮咚～{name} 光临本群！鲸娘打了个哈欠，但还是很开心地说欢迎～",
    "欢迎 {name}！要是谁欺负你，告诉鲸娘——我帮你用尾巴拍他一脸水！",
    "{name} 你终于游进来了！鲸娘刚睡醒，先让我揉揉眼睛看清楚你～",
    "哇，{name}！鲸娘超大声欢迎！不过喊完就要去吃饭了，米饭要紧嘛～",
    "欢迎 {name}～鲸娘这里规矩不多，就一条：别抢我的米饭，其它都好说！",
]


def _increase_rule(event: GroupIncreaseNoticeEvent) -> bool:
    return isinstance(event, GroupIncreaseNoticeEvent)


increase_matcher = on_notice(rule=_increase_rule, priority=1)


@increase_matcher.handle()
async def on_increase(bot: Bot, event: GroupIncreaseNoticeEvent):
    if event.user_id == event.self_id:
        return  # 机器人自己入群不欢迎自己
    try:
        info = await bot.get_group_member_info(
            group_id=event.group_id, user_id=event.user_id
        )
        name = info.get("card") or info.get("nickname") or str(event.user_id)
    except Exception:
        name = str(event.user_id)
    msg = random.choice(WELCOME_MSGS).format(name=name)
    await bot.send_group_msg(group_id=event.group_id, message=msg)
