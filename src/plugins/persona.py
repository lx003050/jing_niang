"""AI 人格切换：/切换人格（仅管理员），人格按群/私聊隔离

- /切换人格 猫娘        -> 按模板把人设要求优化为 system prompt 后切换
- /切换人格 -o 要求     -> 要求不经过优化，原样作为 system prompt
- /切换人格 -cat        -> 切换为内置鲸娘人格
- /切换人格 -b          -> 删除本群/本私聊人格，恢复默认分院帽人格
- /切换人格 小猫 -grok   -> 人格用现有 AI（DeepSeek）优化生成，对话模型切换为 grok
                            （-g / -grok 独立标记；单独 /切换人格 -grok 仅切模型不换人格）

人格与对话模型都作用于当前会话范围（本群 / 本私聊），不同群互不影响。
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg
from openai import AsyncOpenAI

from .admin_tools import OP_SEED, is_op
from .common import (
    CAT_PERSONA,
    DATA_DIR,
    MODEL_OVERRIDE_DIR,
    PERSONA_DIR,
    ai_config,
    hide_help,
    model_override_path,
    persona_path,
    persona_scope,
    register_help,
    send_forward_text,
)

try:
    from .sorting_hat import DEFAULT_SYSTEM_PROMPT as _DEFAULT_SYSTEM_PROMPT
except Exception:  # 兜底：sorting_hat 未加载时不影响本模块
    _DEFAULT_SYSTEM_PROMPT = ""

logger = logging.getLogger("sorting_hat.persona")

# 默认优化模板：把用户的人设要求包装为高质量 system prompt
OPT_PROMPT_TMPL = (
    "你负责把一个简短的人设要求塑造成可直接使用的 AI 人格提示词。"
    "注意：你只在幕后加工，绝对不要把你自己的身份、方法、或“包装师”这类介绍混进输出。\n"
    "加工前先判断：这个要求是“大众词条”（如：傲娇、猫娘、邻家哥哥）"
    "还是“某个动漫/小说/游戏里的具体角色”（如：刹那——指《回复术士的重启人生》中的刹那）。\n"
    "若判断为具体作品角色：依据你对原作的真实认知来写人格——"
    "正确身份与出处、真实性格、说话习惯、标志性台词（可适度引用原作名句）与行为方式，尽量还原原作风格；"
    "对拿不准的细节允许合理演绎，但不要编造与作品明显冲突的背景设定，同名角色拿不准时选最知名的那个。\n"
    "若判断为大众词条：写出该人设下鲜明、具体的说话风格与行为准则，避免空泛形容词堆砌。\n"
    "输出要求——直接输出一段中文 system prompt，用第二人称“你”描述这个人设下的 AI："
    "若来自作品，正文第一句点明出处（如“你是《回复术士的重启人生》中的刹那”）；"
    "接着写身份、说话语气、口头禅、行为准则、禁忌与偏好；"
    "不要任何解释、元说明、标题、编号列表。\n\n"
    "人设要求：{req}"
)

# 校验：优化结果不应出现这些"我把自己当工具人/打包师"的串味词，出现即判优化失败
_OPT_BAD_MARKS = ("人格包装师", "打包师", "系统提示词：", "将下面的人设", "以下为设定", "继承包装", "人设要求：")

# 模仿模式：第一步按完整语言学框架总结说话习惯（由粗到细），第二步只做风格复刻。
# 关键机制：第一步做“内容与风格分离”——偶发内容句（计划/事件/事实）只进隔离清单，仅反复出现的表达模式才作为风格证据。
IMIT_ANALYZE_TMPL = (
    "下面是在群聊里截取的用户「{name}」的发言样本（按时间顺序，越靠后越新）：\n\n{samples}\n\n"
    "你是人格分析师，任务是把「内容」与「风格」彻底分离，并输出一份结构化《说话习惯分析》。\n"
    "## 第 0 步：样本分流（最先做，最重要，决定后续一切判断）\n"
    "逐条判定每条样本属于哪一类：\n"
    "- 内容性：一次性事件、事实陈述、计划、即时状态、对外部话题的谈论。"
    "例如“我们明天去吃肯德基吧”“今天下雨了”“我刚下班”“你看那个视频了吗”。它回答“说了什么”，与语言习惯无关。\n"
    "- 风格性：承载说话方式本身的表达——口头禅、固定句式、功能词、用词习惯、标点/换行/语气范式、情绪表达模式等。它回答“怎么说”。\n"
    "判定规则：\n"
    "- 任何口头禅 / 专属句式 / 固定表达，必须至少 2 次出现在不同语境，才能作为风格特征，且输出时标注出现次数与风格性例句；"
    "单次出现的内容性句子一律归入隔离清单，不得作为任何风格的证据。\n"
    "- 内容性话题即使多次出现（如总聊游戏、总约饭），只算“话题取向”，可写进思维模式维度的话题偏好，"
    "严禁其原文作为句式 / 口头禅例句进入其他维度。\n"
    "## 输出格式（严格按此骨架，中文条目化；分析只依据样本归纳，不虚构样本之外的内容）\n\n"
    "### A. 内容隔离清单（本清单里的句子绝不允许进入风格特征）\n"
    "- 逐条列出被判定为内容性的样本，格式：「样本原文」：原因；没有内容性样本就写“无”。\n\n"
    "### B. 风格特征（每条必须附证据：引用风格性例句；口头禅/句式模板标注出现次数；样本不足的维度标注“样本不足”）\n"
    "## B1. 语用层面（最高区分度，熟人辨别第一依据）\n"
    "- 话轮习惯：抢话 / 停顿很久才回复 / 短句接续 / 先附和再反驳；会不会主动延伸话题，还是一问一答闭环。\n"
    "- 礼貌策略：直给型、委婉铺垫型、自嘲缓冲型、客气客套型；是否经常道歉、感谢。\n"
    "- 情绪表达范式：生气是冷短句，还是长篇抱怨；开心是感叹词多，还是极简陈述；会不会回避负面情绪。\n"
    "- 言外之意偏好：喜欢明示，还是大量潜台词、暗示、留白；是否爱反问代替陈述。\n"
    "- 场景切换规则：和熟人 / 长辈 / 工作对象会不会切换两套说话模式（语码转换）。\n"
    "## B2. 话语结构 & 篇章组织\n"
    "- 平均话语长度：是超长复杂长句，还是碎片化短句、经常断句；平均分句数量。\n"
    "- 语序偏好：是否经常倒装、状语前置、宾语后置（“今晚吃饭吗” vs “饭，今晚要不要吃”）。\n"
    "- 连接词偏好：高频连词（然后 / 但是 / 其实 / 说白了 / 再者）；是否极少用逻辑连接，靠读者自行脑补。\n"
    "- 段落组织：开门见山，还是铺垫一大段才说重点；喜欢分点，还是一大坨无分段文本。\n"
    "- 离题倾向：说话容易跑题发散，还是高度紧扣主题。\n"
    "## B3. 句法特征（权重低于语用、词汇）\n"
    "- 常用句式：偏好主动句 / 被动句 / 把字句 / 被字句；是否大量省略主语。\n"
    "- 嵌套程度：是否多用多层定语修饰（长定语）。\n"
    "- 省略规则：什么成分习惯性省略（主语、量词、助词）。\n"
    "- 特殊固定句式：个人专属常用模板（例：“怎么说呢……”“讲道理……”“不是 XX，是 XX”），只保留句式结构，"
    "结构里夹带的具体事物 / 计划 / 事件内容一律抽象化，不算风格。\n"
    "## B4. 词汇特征\n"
    "- 高频功能词（助词、副词、语气词，区分度极高）：呀、嘛、罢了、而已、反倒、姑且、大概、确实 等。\n"
    "- 内容词偏好：用词偏书面 / 口语 / 网络梗 / 行业术语；固定替代词（永远不说“开心”，只说“舒服”）。\n"
    "- 回避词：刻意不用哪些词汇（禁忌词、反感的网络流行语）。\n"
    "- 造词习惯：会不会自创简称、缩写、外号。\n"
    "## B5. 韵律、标点、副语言（文字复刻核心！）\n"
    "- 标点偏好：多用句号 / 逗号 / 感叹号；是否大量省略标点；是否喜欢省略号；是否极少问号。\n"
    "- 换行规则：一句话一行？长句不分行？想到哪里换到哪里。\n"
    "- 表情、表情包、语气后缀：是否固定搭配，什么时候加表情。\n"
    "- 大小写、缩写、错别字（稳定的个人习惯性错字，是非常强的鉴别特征！）。\n"
    "## B6. 思维模式与内容取向\n"
    "- 话题切入方式（结论先行 / 绕弯子 / 反问开场）、论证风格（摆事实 / 讲道理 / 抬杠 / 玩梗）、"
    "态度底色（悲观 / 无所谓 / 乐子人 / 认真较真）、幽默与阴阳怪气程度（明梗 / 暗讽 / 自嘲 / 冷笑话）、"
    "网络化表达浓度（缩写 / 抽象话 / 黑话）、知识面特征。\n"
    "### C. 综合结论\n"
    "- 用 3-5 句话概括该用户最醒目的语言识别点（只能依据 B 部分风格特征，禁止引用 A 部分内容）。"
)

IMIT_BUILD_TMPL = (
    "你是人格分析师。任务：仅依据下面《说话习惯分析》的 B 部分（风格特征），生成一份纯风格复刻的中文 system prompt，"
    "目标是让 AI 一开口就像用户「{name}」本人，不添加任何虚构人设与身份背景。\n\n"
    "【说话习惯分析】\n{analysis}\n\n"
    "直接输出一段可用中文 system prompt，用第二人称“你”描述：\n"
    "仅描述说话风格本身：按分析 B 部分的六个维度逐一落实——"
    "语用（话轮/礼貌/情绪表达/潜台词习惯）、话语结构（长短句/连接词/分条或成段）、"
    "句法模板（专属固定句式只保留句式结构，结构里的具体事物/计划/事件内容一律抽象化处理）、词汇（口头禅/功能词/造词全保留）、"
    "标点与副语言（省标点/省略号/换行/表情搭配/惯用错别字也原样保留）、"
    "思维模式（切入方式/论证风格/态度底色/玩梗程度），"
    "让人一开口说话就能被认成「{name}」；\n"
    "绝不写任何角色身份、职业、作品出处、外貌性格设定——这里复刻的是【这个人说话的样子】，不是扮演谁。\n"
    "【防 AI 味与句式多样性硬约束——务必逐条写进 system prompt】："
    "① 禁止书面化、排比、递进铺垫、讲大道理；禁止“首先/其次/总之/可以说”这类 AI 连接词；"
    "② 禁止完美人设式回应（如“好的呢~”“收到！”“没问题！”这种模板客套）；"
    "③ 句式必须多样化，严禁反复套用同一个固定模板（尤其禁止反复出现“不是…而是…”“然而…”“作为一名…”这种人机感句式），"
    "句式变化要贴合样本里此人自然的组织方式；"
    "④ 语气、句式、用词、标点一律向样本看齐：该短就短、该糙就糙、该没标点就没标点、该有错别字就有错别字，宁可有瑕疵也要像真人打字；"
    "⑤ 认同或反对都要按样本里的方式表达（可能直接开怼、玩梗或甩一句反问），不要礼貌性附和；"
    "⑥ 如果样本显示此人很少用感叹号/很少问句/很少客套，那么生成的所有回复都不能突然冒出一堆感叹号和问句；"
    "⑦ 严禁把 A 部分“内容隔离清单”里的任何句子写入 system prompt，严禁让 AI 复述其中任何一句具体的话，"
    "也不得以列举示范的方式照抄其原文；"
    "⑧ 引用例句时只引用句式结构（如“……吧”邀请式、省略号拖尾）作为语气示范，并在内心明确：这些都是格式示例，"
    "AI 必须用同样语气说自己当下的新内容，绝不重复示例原文。\n"
    "不要解释、元说明、标题或编号列表，也不要重复输出分析本身。"
)

MSGS_FILE = DATA_DIR / "recall_msgs.jsonl"   # 群消息记录（与 recall 插件共用）

_LEAK_GLYPH = 10  # 原文复刻判定用的连续字数；短口头禅等表达不足 10 字不受影响


def _has_sample_leak(persona: str, samples: list[str]) -> str | None:
    """过拟合硬防线：若 system prompt 原样复刻了某条【单次出现】的样本内容（连续 >=_LEAK_GLYPH 字），返回命中片段。

    跨至少 2 条样本重复出现的固定长句式视为真实风格（口头禅/固定框架），放行；
    只在单条样本里出现的偶发内容句（计划/事件/事实陈述）被原样搬进 persona 时触发拦截。
    """
    pt = re.sub(r"\s+", "", persona)
    freq: dict[str, int] = {}
    for s in samples:
        t = re.sub(r"\s+", "", s)
        if len(t) < _LEAK_GLYPH:
            continue
        seen: set[str] = set()
        for i in range(len(t) - _LEAK_GLYPH + 1):
            g = t[i:i + _LEAK_GLYPH]
            if g not in seen:
                seen.add(g)
                freq[g] = freq.get(g, 0) + 1
    for g, c in freq.items():
        if c == 1 and g in pt:
            return g
    return None


def _plain_text(segs) -> str:
    """把 OneBot 消息段列表转成纯文本（只取 text 段）。"""
    parts: list[str] = []
    for s in segs or []:
        if s.get("type") == "text":
            parts.append(str((s.get("data") or {}).get("text") or ""))
    return "".join(parts)


def _seq_of(m: dict) -> int | None:
    """从消息里解析消息序号（兼容 NapCat 不同返回字段名）。"""
    for k in ("message_seq", "msgSeq", "msg_seq", "seq"):
        v = m.get(k)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return None


def _read_local_samples(uid: int, limit: int) -> list[str]:
    """从 recall_msgs.jsonl 本地消息库读取目标用户发言（跨该用户所在全部群聚合、按时间升序）。

    recall 插件实时追加所有群的普通文本消息，行格式：
    {"g": 群号, "id": 消息ID, "u": 用户ID, "x": 文本, "imgs": [], "t": 时间戳}
    """
    hits: list[tuple[float, str]] = []
    try:
        with open(MSGS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("u") != uid:
                    continue
                x = str(r.get("x") or "").strip()
                if not x or x.startswith("/"):
                    continue
                hits.append((float(r.get("t") or 0), x[:160]))
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("读取本地消息库失败: %s", MSGS_FILE, exc_info=True)
        return []
    hits.sort(key=lambda p: p[0])
    return [x for _, x in hits][-limit:]


async def _collect_samples(bot: Bot, group_id: int, uid: int, limit: int = 150) -> tuple[list[str], str]:
    """采集目标用户发言样本。

    优先读本地消息库（recall_msgs.jsonl，跨该用户所在全部群聚合，无需联网、覆盖约 24h 窗口）；
    本地不足 limit 时再用 QQ「查找聊天记录」实时接口补充当前群的最新消息。
    返回 (样本列表[时间升序], 来源描述)。
    """
    local = _read_local_samples(uid, limit)
    if len(local) >= limit:
        return local[-limit:], f"消息库(跨群聚合 {len(local)} 条)"

    # 本地不足 -> 实时窗口补充（与原实现一致）
    live: list[str] = []
    seen_live = set(local)
    count = min(max(limit * 3, 900), 900)  # 直接拉满缓存大窗口，靠去重保住有效样本
    msgs: list[dict] = []
    try:
        ret = await asyncio.wait_for(
            bot.call_api("get_group_msg_history", group_id=group_id, message_seq=0, count=count), timeout=20.0
        )
        msgs = ret.get("messages") or []
    except asyncio.TimeoutError:
        logger.warning("get_group_msg_history count=%d 超时(20s)，回退 20 条", count)
    except Exception:
        logger.warning("get_group_msg_history 大窗口拉取失败，回退 20 条", exc_info=True)
    if not msgs:
        try:
            ret = await asyncio.wait_for(
                bot.call_api("get_group_msg_history", group_id=group_id, message_seq=0, count=20), timeout=15.0
            )
            msgs = ret.get("messages") or []
        except Exception:
            msgs = []
    for m in msgs:
        sender = m.get("sender") or {}
        if sender.get("user_id") != uid:
            continue
        x = _plain_text(m.get("message") or []).strip()
        if not x or x.startswith("/") or x in seen_live:
            continue
        seen_live.add(x)
        live.append(x[:160])
        if len(local) + len(live) >= limit:
            break

    merged = local + live
    if live:
        src = f"消息库(跨群聚合 {len(local)} 条)+实时窗口 {len(live)} 条"
    else:
        src = f"消息库(跨群聚合 {len(local)} 条)"
    return merged[-limit:], src


# ---------- /切换人格 /查看人格 ----------
persona_cmd = on_command("切换人格", aliases={"切人格", "换人格", "切换模型"}, priority=1, block=True)
register_help("/切换人格", "切换 AI 人格（仅管理员，作用于本群/本私聊）：/切换人格 人设；-o 原样直用；-cat 鲸娘；-add 在现有 system prompt 末尾追加词条；-grok 对话模型换 grok；-b 恢复默认")
hide_help("/切换人格")

# ---------- /查看人格 ----------
view_cmd = on_command("查看人格", priority=1, block=True)
register_help("/查看人格", "查看本群/本私聊当前生效的 system prompt（按群独立，合并转发返回）")
hide_help("/查看人格")

# ---------- /切换昵称 ----------
rename_cmd = on_command("切换昵称", aliases={"改昵称", "换昵称", "换名片"}, priority=1, block=True)
register_help("/切换昵称", "仅切换机器人在本群的群昵称（不改人格，仅管理员）：/切换昵称 新昵称；或 /切换昵称 @某人 → 用「某人群昵称*bot」")
hide_help("/切换昵称")


@view_cmd.handle()
async def view_cmd_handler(bot: Bot, event: MessageEvent):
    scope = persona_scope(event)
    label = _scope_label(event)

    # 对话模型信息
    ov = ""
    try:
        ov = model_override_path(scope).read_text(encoding="utf-8").strip()
    except Exception:
        pass
    model_desc = f"grok（{ov}）" if ov else f"DeepSeek（默认 {ai_config.ai_model}）"

    # 实际生效的人格：本群人格文件 > system_prompt.md > 内置默认
    try:
        t = persona_path(scope).read_text(encoding="utf-8").strip()
        if t:
            prompt_text = t
            source = "本群人设文件"
        else:
            sp = (DATA_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()
            if sp:
                prompt_text = sp
                source = "管理员自定义 system_prompt.md"
            else:
                prompt_text = "（未设置自定义人格，走内置默认分院帽人格，此处不展开）"
                source = "内置默认"
    except FileNotFoundError:
        sp_text = ""
        try:
            sp_text = (DATA_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        if sp_text:
            prompt_text = sp_text
            source = "管理员自定义 system_prompt.md"
        else:
            prompt_text = "（未设置自定义人格，走内置默认分院帽人格，此处不展开）"
            source = "内置默认"
    except Exception:
        await view_cmd.finish("读取人格失败，稍后再试试。")

    full = f"【{label} 当前生效的 system prompt】\n来源：{source}\n对话模型：{model_desc}\n\n{prompt_text}"
    try:
        await send_forward_text(bot, event, full, name="分院帽·人格")
    except Exception:
        await view_cmd.finish("合并转发发送失败，稍后再试试？")


def _scope_label(event: MessageEvent) -> str:
    gid = getattr(event, "group_id", None)
    return f"本群" if gid else "本私聊"


def _set_model_override(scope: str, model: str) -> None:
    MODEL_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    model_override_path(scope).write_text(model.strip(), encoding="utf-8")


def _clear_model_override(scope: str) -> None:
    try:
        model_override_path(scope).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


async def _rename_self(bot: Bot, event: MessageEvent, card: str) -> None:
    """把机器人在当前群的群名片改为指定昵称（仅群聊生效，失败静默）。"""
    gid = getattr(event, "group_id", None)
    if not gid:
        return
    card = (card or "").strip()[:30]
    try:
        await bot.set_group_card(group_id=gid, user_id=int(bot.self_id or 0), card=card or None)
    except Exception:
        logger.warning("群名卡设置失败: %r", card)


async def _imitate_persona(
    bot: Bot, event: MessageEvent, scope: str, label: str, target_uid: int, count: int = 150, hint: str | None = None
) -> None:
    """模仿模式：@某人 → 参考该人在本群的发言样本生成相似人格；带 hint 时备注原文贴尾。"""
    gid = getattr(event, "group_id", None)
    if not gid:
        await persona_cmd.finish("模仿模式只能在群聊里 @ 目标成员使用。", at_sender=True)
    name = str(target_uid)
    try:
        info = await asyncio.wait_for(
            bot.get_group_member_info(group_id=gid, user_id=target_uid), timeout=15.0
        )
        name = str(info.get("card") or info.get("nickname") or target_uid)
    except asyncio.TimeoutError:
        logger.warning("模仿 %s: get_group_member_info 超时(15s)，用 QQ 号代替昵称", target_uid)
    except Exception:
        pass
    await persona_cmd.send(f"炼化群友中…… 🔮 正在按「{name}」的说话习惯炼化人格，请稍候")
    limit = max(3, min(count, 10000))
    t0 = datetime.now()
    samples, src = await _collect_samples(bot, gid, target_uid, limit=limit)
    logger.warning(
        "模仿 %s: 采集样本 %d/%d 条（来源 %s，耗时 %.1fs）",
        target_uid,
        len(samples),
        limit,
        src,
        (datetime.now() - t0).total_seconds(),
    )
    if len(samples) < 3:
        await persona_cmd.finish(
            f"「{name}」在 {label} 的发言样本太少（只抓到 {len(samples)} 条，目标 {limit} 条），"
            f"再聊一会儿再试？",
            at_sender=True,
        )
    if not ai_config.openai_api_key:
        await persona_cmd.finish("AI 未配置 API Key，无法生成模仿人格。")
    try:
        client = AsyncOpenAI(api_key=ai_config.openai_api_key, base_url=ai_config.openai_base_url, timeout=600.0)
        # 第一步：从发言样本详细总结说话习惯
        logger.warning("模仿 %s: 开始第 1 步 说话习惯分析（样本 %d 条）", target_uid, len(samples))
        t1 = datetime.now()
        resp1 = await asyncio.wait_for(
            client.chat.completions.create(
                model=ai_config.ai_model,
                messages=[{"role": "user", "content": IMIT_ANALYZE_TMPL.format(name=name, samples="\n".join(samples))}],
                temperature=0.5,
            ),
            timeout=900.0,
        )
        analysis = (resp1.choices[0].message.content or "").strip()
        logger.warning("模仿 %s: 第 1 步完成（耗时 %.1fs，分析 %d 字）", target_uid, (datetime.now() - t1).total_seconds(), len(analysis))
        if not analysis:
            await persona_cmd.finish("说话习惯总结为空，请重试一次？")
        # 第二步：纯风格复刻（不套虚拟人设；备注原文原样贴在 system prompt 末尾）
        logger.warning("模仿 %s: 开始第 2 步 复刻 prompt 构建", target_uid)
        t2 = datetime.now()
        resp2 = await asyncio.wait_for(
            client.chat.completions.create(
                model=ai_config.ai_model,
                messages=[
                    {
                        "role": "user",
                        "content": IMIT_BUILD_TMPL.format(analysis=analysis, name=name),
                    }
                ],
                temperature=0.9,
            ),
            timeout=900.0,
        )
        persona = (resp2.choices[0].message.content or "").strip()
        logger.warning("模仿 %s: 第 2 步完成（耗时 %.1fs，prompt %d 字）", target_uid, (datetime.now() - t2).total_seconds(), len(persona))
        if hint:
            persona = f"{persona}\n\n【备注（原样附上，需遵守）】\n{hint}"
            mode_desc = f"按「{name}」原话风格复刻 + 备注「{hint}」"
        else:
            mode_desc = f"按「{name}」原话风格复刻"
    except Exception as e:
        logger.warning("模仿人格生成失败: %s", e)
        await persona_cmd.finish("模仿人格生成接口出错了，稍后再试试？")
    if not persona:
        await persona_cmd.finish("模仿结果为空，换个目标或稍后再试？")
    if any(m in persona for m in _OPT_BAD_MARKS):
        await persona_cmd.finish("这轮模仿结果不太对（混入了设定流程），再试一次？")
    leaked = _has_sample_leak(persona, samples)
    if leaked:
        logger.warning("模仿 %s: 过拟合拦截（persona 原样复刻样本内容 %r），拒绝写入", target_uid, leaked)
        await persona_cmd.finish("这轮模仿把原话内容当成了说话风格（过拟合），已拦截，再试一次？")
    await _save_persona(scope, persona)
    await _rename_self(bot, event, f"{name}*bot")
    await persona_cmd.finish(
        f"已为 {label} 生成人格 ✅（{mode_desc}，参考 {len(samples)}/{limit} 条发言，不影响其他群）\n"
        f"预览：{persona[:80]}{'…' if len(persona) > 80 else ''}",
        at_sender=True,
    )


@persona_cmd.handle()
async def persona_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not is_op(event.user_id):
        await persona_cmd.finish("这个指令只有管理员能用哦。", at_sender=True)

    scope = persona_scope(event)
    label = _scope_label(event)
    target = persona_path(scope)

    # @某人：模仿该人在本群的说话风格生成人格（仅群聊）
    target_uid = None
    for seg in arg:
        if seg.type == "at":
            qq = (seg.data or {}).get("qq")
            if qq and str(qq) != "all":
                try:
                    target_uid = int(qq)
                except Exception:
                    pass
                break

    raw = str(arg).strip()
    pure = arg.extract_plain_text().strip()
    if target_uid:
        # @某人（可带 -数字 样本量 / 人设方向）仅根管理员可用
        if event.user_id != OP_SEED:
            await persona_cmd.finish("模仿某人的说话风格只有根管理员能用哦。", at_sender=True)
        count = 150
        hint_parts: list[str] = []
        # 只取 at 之外的真实 text 段，避免 @名字 混入注释
        for seg in arg:
            if seg.type == "text":
                t = str((seg.data or {}).get("text") or "").strip()
                if t:
                    hint_parts.append(t)
        hint_parts = " ".join(hint_parts).split()
        hint_words: list[str] = []
        for w in hint_parts:
            if re.fullmatch(r"-\d+", w):
                count = int(w[1:])
            elif w.lower() not in ("-grok", "-g", "-deepseek", "-ds"):
                hint_words.append(w)
        count = max(3, min(count, 10000))
        await _imitate_persona(bot, event, scope, label, target_uid, count=count, hint=" ".join(hint_words).strip() or None)
        return

    # 模型参数：-grok/-g 或 -deepseek/-ds（与人格要求可叠加；单独出现时仅切模型、保留人格）
    words = pure.split()
    grok = any(w.lower() in ("-grok", "-g") for w in words)
    ds = any(w.lower() in ("-deepseek", "-ds") for w in words)
    if grok and ds:
        await persona_cmd.finish("不能同时指定 -grok 和 -deepseek 哦。")
    clean = " ".join(w for w in words if w.lower() not in ("-grok", "-g", "-deepseek", "-ds")).strip()
    low = clean.lower()

    # -add：仅追加新词条到当前生效的 system prompt（不重新生成人格、不覆盖原内容）
    if any(w.lower() == "-add" for w in words):
        add_words = [w for w in words if w.lower() not in ("-add", "-grok", "-g", "-deepseek", "-ds")]
        add_text = " ".join(add_words).strip()
        if not add_text:
            await persona_cmd.finish("用法：/切换人格 要追加的话 -add（例如：/切换人格 少说一点奥 -add）", at_sender=True)
        # 取当前生效人格：本群/私聊人格文件 > system_prompt.md > 内置默认
        base_prompt = ""
        try:
            t = target.read_text(encoding="utf-8").strip()
            if t:
                base_prompt = t
        except OSError:
            pass
        if not base_prompt:
            try:
                t = (DATA_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()
                if t:
                    base_prompt = t
            except OSError:
                pass
        if not base_prompt:
            base_prompt = _DEFAULT_SYSTEM_PROMPT or "（当前使用默认人格）"
        new_prompt = f"{base_prompt.rstrip()}\n\n{add_text}"
        await _save_persona(scope, new_prompt)
        if grok:
            _set_model_override(scope, ai_config.grok_model or "grok-4.6")
            await persona_cmd.finish(f"已在 {label} 的 system prompt 末尾追加「{add_text}」✅（保留原内容）+ grok 对话模型", at_sender=True)
        if ds:
            _clear_model_override(scope)
            await persona_cmd.finish(f"已在 {label} 的 system prompt 末尾追加「{add_text}」✅（保留原内容）+ DeepSeek 对话模型", at_sender=True)
        await persona_cmd.finish(f"已在 {label} 的 system prompt 末尾追加「{add_text}」✅（保留原内容，不影响其他群）", at_sender=True)

    # -b：恢复默认人格 + 默认模型（仅本群/本私聊）
    if low == "-b":
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            await persona_cmd.finish(f"删除人格文件失败：{e}")
        _clear_model_override(scope)
        await _rename_self(bot, event, "")  # 清空群名片，恢复默认昵称
        src = "（管理员自定义版）" if (DATA_DIR / "system_prompt.md").exists() else "（内置默认版）"
        await persona_cmd.finish(f"已恢复 {label} 的分院帽人格 🎩{src}，对话模型也回到默认，不影响其他群", at_sender=True)

    # 仅模型参数：保留当前人格，只切 API 与模型
    if not clean:
        if grok:
            model = ai_config.grok_model or "grok-4.6"
            _set_model_override(scope, model)
            await persona_cmd.finish(f"已为 {label} 设置对话模型为 grok（{model}），人格保持不变，不影响其他群", at_sender=True)
        if ds:
            _clear_model_override(scope)
            await persona_cmd.finish(f"已为 {label} 设置对话模型为 DeepSeek（默认），人格保持不变，不影响其他群", at_sender=True)

    # -cat：鲸娘（可叠加 -grok / -deepseek）
    if low == "-cat":
        await _save_persona(scope, CAT_PERSONA)
        await _rename_self(bot, event, "鲸娘")
        if grok:
            _set_model_override(scope, ai_config.grok_model or "grok-4.6")
            await persona_cmd.finish(f"已为 {label} 切换鲸娘人格 🐋 + grok 对话模型，不影响其他群", at_sender=True)
        if ds:
            _clear_model_override(scope)
            await persona_cmd.finish(f"已为 {label} 切换鲸娘人格 🐋 + DeepSeek 对话模型，不影响其他群", at_sender=True)
        await persona_cmd.finish(f"已为 {label} 切换鲸娘人格 🐋 愿者上钩～", at_sender=True)

    # -o：原样直用（可叠加 -grok / -deepseek），支持 "-o 内容" 或 "内容 -o" 两种写法
    if any(w.lower() == "-o" for w in words):
        req = " ".join(w for w in clean.split() if w.lower() != "-o").strip()
        if not req:
            await persona_cmd.finish("用法：/切换人格 -o 你想直接作为人格的提示词（或：/切换人格 你想直接作为人格的提示词 -o）")
        await _save_persona(scope, req)
        if grok:
            _set_model_override(scope, ai_config.grok_model or "grok-4.6")
            await persona_cmd.finish(f"已为 {label} 原样设置人格（未优化）+ grok 对话模型 ✅", at_sender=True)
        if ds:
            _clear_model_override(scope)
            await persona_cmd.finish(f"已为 {label} 原样设置人格（未优化）+ DeepSeek 对话模型 ✅", at_sender=True)
        await persona_cmd.finish(f"已为 {label} 原样设置人格（未优化）✅", at_sender=True)

    # 默认：LLM 优化（可叠加 -grok）
    if not clean:
        await persona_cmd.finish(
            "用法（作用于本群/本私聊，不影响其他群）：\n/切换人格 人设（如：/切换人格 猫娘）\n"
            "-o 后直接写提示词不优化（也可写在末尾，如：/切换人格 一只猫娘 -o）\n-cat 切换鲸娘\n"
            "-add 在现有 system prompt 末尾追加词条（如：/切换人格 少说一点奥 -add）\n"
            "-grok/-g 对话模型换 grok\n-deepseek/-ds 对话模型换回 DeepSeek\n"
            "只给模型参数不给人设时，保留现有人格仅切模型\n"
            "-b 恢复默认分院帽人格与模型",
            at_sender=True,
        )
    if not ai_config.openai_api_key:
        await persona_cmd.finish("AI 未配置 API Key，无法调用优化，可用 -o 直接指定。")
    await persona_cmd.send("人格切换中…… ⏳ 正在生成人格，请稍候")

    try:
        client = AsyncOpenAI(api_key=ai_config.openai_api_key, base_url=ai_config.openai_base_url, timeout=90.0)
        resp = await client.chat.completions.create(
            model=ai_config.ai_model,
            messages=[{"role": "user", "content": OPT_PROMPT_TMPL.format(req=clean)}],
            temperature=0.8,
        )
        persona = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("人格优化失败: %s", e)
        await persona_cmd.finish("人格优化接口出错了，换个说法再试，或用 -o 原样指定。")
    if not persona:
        await persona_cmd.finish("优化结果为空，换个说法再试？")
    if any(m in persona for m in _OPT_BAD_MARKS):
        logger.warning("人格优化结果串味，拒绝写入: %r", persona[:60])
        await persona_cmd.finish("这轮优化结果不太对（模型把设定流程混进了人格），再试一次，或改用 -o 直接指定吧。")

    await _save_persona(scope, persona)
    await _rename_self(bot, event, clean)
    if grok:
        _set_model_override(scope, ai_config.grok_model or "grok-4.6")
        await persona_cmd.finish(
            f"已为 {label} 切换人格 ✅（优化后，仅本会话生效）+ 对话模型 grok\n"
            f"预览：{persona[:80]}{'…' if len(persona) > 80 else ''}",
            at_sender=True,
        )
    if ds:
        _clear_model_override(scope)
        await persona_cmd.finish(
            f"已为 {label} 切换人格 ✅（优化后，仅本会话生效）+ DeepSeek 对话模型\n"
            f"预览：{persona[:80]}{'…' if len(persona) > 80 else ''}",
            at_sender=True,
        )
    await persona_cmd.finish(
        f"已为 {label} 切换人格 ✅（仅本会话生效，不影响其他群）\n"
        f"预览：{persona[:80]}{'…' if len(persona) > 80 else ''}",
        at_sender=True,
    )


async def _save_persona(scope: str, text: str) -> None:
    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    persona_path(scope).write_text(text.strip(), encoding="utf-8")
    logger.warning("人格已切换 scope=%s 长度=%d", scope, len(text))


# ---------- /切换昵称 ----------
@rename_cmd.handle()
async def rename_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """仅切换本群群名片，不改动人格（system prompt）。/切换昵称 新昵称；或 /切换昵称 @某人。"""
    gid = getattr(event, "group_id", None)
    if not gid:
        await rename_cmd.finish("切换昵称只能在群聊里用哦。", at_sender=True)
    if not is_op(event.user_id):
        await rename_cmd.finish("这个指令只有管理员能用哦。", at_sender=True)

    # @某人 → 名片 = 该人在本群的昵称 + *bot
    for seg in arg:
        if seg.type == "at":
            qq = (seg.data or {}).get("qq")
            if qq and str(qq) != "all":
                target = int(qq)
                name = str(target)
                try:
                    info = await asyncio.wait_for(
                        bot.get_group_member_info(group_id=gid, user_id=target), timeout=15.0
                    )
                    name = str(info.get("card") or info.get("nickname") or target)
                except asyncio.TimeoutError:
                    logger.warning("切换昵称 @%s: get_group_member_info 超时(15s)，用 QQ 号代替", target)
                except Exception:
                    pass
                card = f"{name}*bot"
                await _rename_self(bot, event, card)
                await rename_cmd.finish(f"已将本群群昵称改为「{card}」✅（人格未改动）", at_sender=True)

    text = arg.extract_plain_text().strip()
    if not text:
        await rename_cmd.finish(
            "用法：/切换昵称 新昵称（仅改群昵称，不动人格）\n或：/切换昵称 @某人（改为「某人昵称*bot」）",
            at_sender=True,
        )
    await _rename_self(bot, event, text)
    await rename_cmd.finish(f"已将本群群昵称改为「{text}」✅（人格未改动）", at_sender=True)