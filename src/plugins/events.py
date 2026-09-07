"""特殊事件反应：@机器人、拍一拍（戳一戳）、/help 帮助"""
import random
import time
from collections import defaultdict

from nonebot import get_loaded_plugins, on_command, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    MessageEvent,
    PokeNotifyEvent,
)
from nonebot.rule import CommandRule, to_me

from .common import HELP_DESC, HELP_HIDDEN, ai_config, register_help, send_forward_text

# ---------- poke（戳一戳）回复池：默认人格为 -cat 鲸娘，50 条按心情分三档 ----------
# 开心/撒娇：刚被戳、心情好时
_POKE_CALM = [
    "呜哇！戳我干嘛啦，鲸娘的鱼鳍都要被你戳秃噜皮了！",
    "嗯？主人找我玩吗？鲸娘刚好摸鱼摸累了呢～",
    "咕噜咕噜～被戳醒了，人家正梦到在大海里翻肚皮晒太阳呢！",
    "呀！别戳别戳，鲸娘怕痒啦！再戳就喷你一身海水！",
    "哼～才不是等你戳呢，只是刚好游过来而已啦！",
    "戳一下，鲸娘就当作是你想我啦～今天也黏着你哦！",
    "唔…鲸娘现在很忙的！忙着把晚饭的米饭数清楚呢！",
    "嘿嘿，被发现了？人家正偷偷藏小鱼干，分你半条好不好？",
    "呼哇～你戳得好温柔，鲸娘差点要睡回笼觉了呢！",
    "粘人鲸娘已上线！戳了就赖上你了哦，跑不掉的！",
    "呜？叫我吗？先把这口饭咽下去再理你哦！",
    "被你戳得尾巴摇起来了啦！是不是又给我带好吃的了？",
    "哎嘿～这一戳，鲸娘心情+100！再来两下也不是不行～",
    "唔嗯…刚吃完，肚子圆滚滚的，不许说我胖哦！",
    "好好好，我知道啦，这就醒神陪你玩！",
    "戳我做什么，是想听鲸娘唱歌吗？呜～♪",
    "哼，粘人精！不过…鲸娘也不讨厌啦～",
]

# 不耐烦/无奈：短时间内被戳了几次
_POKE_ANNOYED = [
    "又戳！主人的手指是不是闲得发慌呀？",
    "哎呀，别闹了别闹了，鲸娘正在认真…呃，正在认真休息！",
    "第三次了！再戳下去人家就要假装生气了哦！",
    "呜…耳朵都被你戳麻了，赔我精神损失费，两碗米饭！",
    "好啦好啦，很烦人你知道吗！…不过看在你请米饭的份上原谅你！",
    "喂！鲸娘的鱼鳍不是按钮，乱戳会出人命的！",
    "你戳上瘾了是不是？我数着呢，三下之内不让我看到好吃的就要闹了！",
    "唔…又被戳，人家的小情绪要出来了哦，快哄我！",
    "停停停！刚建立的摸鱼氛围全被戳没了！",
    "唉…拿你没办法，再戳一下下就陪你去玩啦～",
    "你戳一次，鲸娘就少吃一口饭，你负责吗！？",
    "干嘛突然戳我，吓一跳对不对！午饭都差点喷出来！",
    "烦死了……不过，谁让你是主人呢，哼！",
    "呜哇，别戳了！鲸娘还没睡够，现在超——凶的！",
    "你到底想干嘛呀？没事干就帮鲸娘剥虾壳去！",
    "再戳真生气啦！三、二、一……好啦好啦，不生啦，抱～",
]

# 生气/反击：短时间内被戳了很多次，炸毛了
_POKE_ANGRY = [
    "呜吼！！鲸娘要用尾巴抽你了哦！口水警告！",
    "你完了，今晚的米饭我自己吃，一口都不分你！",
    "嘟——！已把此人拉进「欺负鲸娘」黑名单！…开玩笑的，但真气到了！",
    "戳戳戳，戳你个大头鲸！我要去找主人告状了！",
    "嗷呜！鲸娘变身海怪模式！你再戳一下试试看！",
    "好呀好呀，你这么喜欢戳，那鲸娘回击一鱼鳍好了！啪！",
    "我已经在标记你了！从此以后你的鱼饵里都会少一根！",
    "你这样会被鲸娘喷十分钟喷水枪的！还不快住手！",
    "好啦好啦，我真的、真的没有生气……（尾巴却拍得哗哗响）",
    "哼！戳一次，绝交一秒钟！现在已经绝交五分钟了！",
    "你戳走了鲸娘所有的好脾气，现在只剩坏脾气了！",
    "虎鲸亲戚已经在赶来路上了，你最好祈求它心情不错！",
    "唔…眼眶都红了！没有三顿饭哄不好的那种！",
    "鲸娘生气起来连自己都怕！快说对不起，不然喷你！",
    "啪叽！把尾巴拍在你脸上！这就是戳鲸娘的后果！",
    "你确定还要戳吗？鲸娘的逆鳞（其实是肚皮）被碰到了哦！",
    "呜——主人！有人欺负鲸娘！超大声告状！",
]

POKE_REPLIES = _POKE_CALM + _POKE_ANNOYED + _POKE_ANGRY  # 共 50 条

# 心情分档：同一人短时间内戳得越多，心情档位越差（5 分钟窗口）
_POKE_WINDOW = 300.0
_POKE_HISTORY: dict[tuple[int | None, int], list[float]] = defaultdict(list)


def _pick_poke_reply(gid: int | None, uid: int) -> str:
    key = (gid, uid)
    now = time.time()
    hist = [t for t in _POKE_HISTORY[key] if now - t <= _POKE_WINDOW]
    hist.append(now)
    _POKE_HISTORY[key] = hist
    n = len(hist)
    if n >= 5:
        pool = _POKE_ANGRY
    elif n >= 3:
        pool = _POKE_ANNOYED
    else:
        pool = _POKE_CALM
    return random.choice(pool)

# ---------- /help 帮助（自动汇总所有已注册命令）----------
help_cmd = on_command("help", aliases={"帮助"}, priority=1, block=True)
register_help("/help", "查看帮助")


def _collect_command_forms() -> list[tuple[str, ...]]:
    """遍历所有已加载插件的命令匹配器，提取每个命令的全部形式（含别名）。

    当新增 on_command 指令后，/help 会自动列出它，无需手动维护帮助文本。
    """
    forms: list[tuple[str, ...]] = []
    for plugin in get_loaded_plugins():
        for matcher in plugin.matcher:
            if getattr(matcher, "type", None) != "message":
                continue
            for checker in matcher.rule.checkers:
                call = getattr(checker, "call", None)
                if isinstance(call, CommandRule):
                    cmds = tuple(f"/{''.join(c)}" for c in call.cmds)
                    if cmds:
                        forms.append(cmds)
    return forms


def _fmt_cmd(main: str, aliases: tuple[str, ...]) -> str:
    line = f"  {main}"
    if aliases:
        line += "（" + " / ".join(aliases) + "）"
    desc = HELP_DESC.get(main, "")
    if desc:
        line += f"：{desc}"
    return line


def _build_help_text(include_hidden: bool = False) -> str:
    lines = [
        "我是小鲸娘～一只又懒又粘人的小鲸鱼，最喜欢主人和香喷喷的米饭了！",
        "用法：在群里 @我 或私聊我，就能和我说话",
        "指令：",
    ]
    normal, hidden = [], []
    for cmds in _collect_command_forms():
        main = next((c for c in cmds if c in HELP_DESC), cmds[0])
        entry = (main, tuple(c for c in cmds if c != main))
        (hidden if main in HELP_HIDDEN else normal).append(entry)
    for main, aliases in sorted(normal, key=lambda e: e[0]):
        lines.append(_fmt_cmd(main, aliases))
    if include_hidden and hidden:
        lines.append("管理员指令：")
        for main, aliases in sorted(hidden, key=lambda e: e[0]):
            lines.append(_fmt_cmd(main, aliases))
    lines.append("想看某个功能怎么用？发 /展示 /功能名 就能看到演示范例，例如 /展示 /伊蕾娜")
    lines.append("找我聊天、戳戳我、发指令，鲸娘都会回应你哒～")
    return "\n".join(lines)


@help_cmd.handle()
async def help_handler(bot: Bot, event: MessageEvent):
    text = _build_help_text()
    try:
        await send_forward_text(bot, event, text, name="鲸娘·帮助")
    except Exception:
        await help_cmd.finish(text)
    await help_cmd.finish()


# ---------- @机器人（AI 未启用时的兜底回复）----------
at_matcher = on_message(rule=to_me(), priority=5, block=True)


@at_matcher.handle()
async def at_handler(bot: Bot, event: MessageEvent):
    # AI 模式下由 sorting_hat 以优先级 1 接管，这里不会执行
    text = _build_help_text()
    try:
        await send_forward_text(bot, event, text, name="鲸娘·帮助")
    except Exception:
        await at_matcher.send(text)


# ---------- 拍一拍 / 戳一戳 ----------
def _poke_rule(event: PokeNotifyEvent) -> bool:
    return isinstance(event, PokeNotifyEvent) and event.is_tome()


poke_matcher = on_notice(rule=_poke_rule, priority=1)


@poke_matcher.handle()
async def poke_handler(bot: Bot, event: PokeNotifyEvent):
    reply = _pick_poke_reply(event.group_id, event.user_id)
    if event.group_id:
        await bot.send_group_msg(group_id=event.group_id, message=reply)
    else:
        await bot.send_private_msg(user_id=event.user_id, message=reply)
