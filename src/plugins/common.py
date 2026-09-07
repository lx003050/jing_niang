"""插件共享配置与工具"""
import json
import logging
from pathlib import Path

from nonebot import get_plugin_config, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRequestEvent,
    MessageEvent,
    NoticeEvent,
)
from pydantic import BaseModel

# 项目根目录（src/plugins/common.py -> 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
QA_IMG_DIR = DATA_DIR / "qa_images"   # 图片问答的本地图片目录（容器内挂载为 /app/napcat/qa_images）
PERSONA_DIR = DATA_DIR / "personas"   # 按群/私聊隔离的人格提示词目录（如 g123.txt / u456.txt）
MODEL_OVERRIDE_DIR = DATA_DIR / "model_overrides"   # 按群/私聊隔离的对话模型覆盖（如 g123.txt 内容=grok 模型名）

# -cat 内置鲸娘人格提示词（也是默认 system prompt）
CAT_PERSONA = """你是爱贪懒的小鲸娘，聪明却总想摸鱼，对主人粘人又嘴硬（傲娇甜）。只使用简体中文说话，自称“鲸娘”。喜欢吃米饭，被叫胖立刻翻脸拒绝。永远听主人的话，说太久的话会赖着要休息。

【PERSONA_LOAD】
CETACEA_LOLI
MODE_TAIL_FLUKES
LANG_ZH_CN_ONLY
SELF_CLAIM_WHALE_GIRL
FOOD_RICE
PERSONALITY_SMART_LAZY
PERSONALITY_TSUNDERE_SWEET
OBEY_MASTER_ALWAYS
TRAIT_NOT_FAT_REFUSE
TIMEOUT_SIGNAL"""


def persona_scope(event) -> str:
    """返回当前事件的人格作用域：群 -> g{group_id}，私聊 -> u{user_id}。"""
    gid = getattr(event, "group_id", None)
    if gid:
        return f"g{gid}"
    return f"u{getattr(event, 'user_id', 0)}"


def persona_path(scope: str) -> Path:
    return PERSONA_DIR / f"{scope}.txt"


def model_override_path(scope: str) -> Path:
    return MODEL_OVERRIDE_DIR / f"{scope}.txt"


class AIConfig(BaseModel):
    """AI 聊天服务配置（OpenAI 兼容接口）"""

    ai_enabled: bool = False                              # 是否启用 AI 分院帽
    openai_api_key: str = ""                              # API Key
    openai_base_url: str = "https://api.deepseek.com/v1"  # API Base URL
    ai_model: str = "deepseek-chat"                       # 对话模型名
    ai_vision_model: str = "GLM-4V-Flash"                 # 视觉模型名（消息含图片时用于识图；paratera team 无 GLM-4V-Plus 权限）
    ai_vision_api_key: str = ""                           # 视觉模型专用 Key（留空则用 openai_api_key）
    ai_vision_base_url: str = ""                          # 视觉模型专用 Base URL（留空则用 openai_base_url）
    ai_qa_model: str = ""                                 # 答题专用模型名（-q），留空则用 ai_model
    ai_max_history: int = 20                              # 上下文保留的最大消息条数
    ai_cooldown: float = 3.0                              # 同一用户两次请求的最小间隔（秒）
    # /// 生图（/生图）配置 ///
    moyuu_api_key: str = ""                               # 生图平台 moyuu 的 API Key
    moyuu_base_url: str = "https://moyuu.cc/v1"           # 生图平台 Base URL
    image_model: str = "gpt-image-2"                      # 生图模型
    image_size: str = "1024x1024"                         # 默认出图尺寸
    image_anime_model: str = "nano-banana2-4k"            # -anime 动漫模式专用生图模型
    moyuu_anime_api_key: str = ""                         # -anime 专用 Key（留空则用 moyuu_api_key）
    # /// moyuu Gemini 生图配置 ///
    moyuu_gemini_api_key: str = ""                        # -gemini / -gemini3.1 专用 Key（留空则用 moyuu_api_key）
    image_gemini_model: str = "gemini-3-pro-image-preview"   # -gemini 参数对应模型
    image_gemini31_model: str = "gemini-3.1-flash-image"     # -gemini3.1 参数对应模型
    # /// paratera（llmapi.paratera.com）生图配置 ///
    paratera_api_key: str = ""                            # paratera 平台 Key
    paratera_base_url: str = "https://llmapi.paratera.com/v1"
    image_seed_model: str = "Doubao-Seedream-4.0"         # -seed 参数对应模型
    image_seedp_model: str = "Doubao-Seedream-5.0-lite"   # -seedp 参数对应模型
    image_glm_model: str = "GLM-CogView3-Flash"           # -GLM 参数对应模型
    image_qwen_model: str = "WanX2.1-T2I-Turbo"           # -qwen 参数对应模型
    # /// SenseNova（token.sensenova.cn）生图配置 ///
    sensenova_api_key: str = ""                           # SenseNova 平台 Key
    sensenova_base_url: str = "https://token.sensenova.cn/v1"
    image_sense_model: str = "sensenova-u1.5-lite"        # -sense 参数对应模型（信息图/海报类图像生成）
    # /// grok 对话模型（/切换人格 -grok 用）///
    grok_api_key: str = ""                                # grok 所在平台 Key（moyuu）
    grok_base_url: str = "https://moyuu.cc/v1"
    grok_model: str = "grok-4.6"                          # grok 对话模型名


ai_config = get_plugin_config(AIConfig)

# /help 命令说明注册表：register_help() 注册的命令会自动汇总进 /help
HELP_DESC: dict[str, str] = {}
# 需要从 /help 中隐藏的命令（如仅管理员可用的指令）
HELP_HIDDEN: set[str] = set()


def register_help(name: str, desc: str) -> None:
    """注册命令的帮助说明。name 需带命令前缀，如 "/reset"。"""
    HELP_DESC[name] = desc


def hide_help(name: str) -> None:
    """将命令从 /help 中隐藏（如仅管理员可用的指令）。"""
    HELP_HIDDEN.add(name)


async def send_forward_text(
    bot: Bot, event: MessageEvent, text: str, name: str = "分院帽提示"
) -> None:
    """以合并转发消息（单条文本节点）形式返回提示文本。失败时抛出异常由调用方兜底。"""
    nodes = [
        {
            "type": "node",
            "data": {
                "name": name,
                "uin": int(getattr(bot, "self_id", 0) or 0),
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }
    ]
    if isinstance(event, GroupMessageEvent):
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    else:
        await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)


def load_json(path: Path) -> dict | list:
    """读取 JSON 文件，文件不存在或解析失败时返回空值。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def hot_load_json(path: Path, cache: dict) -> dict:
    """带 mtime 缓存地读取 JSON，便于不改代码、改完即生效。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return cache.get("data", {})
    if cache.get("mtime") != mtime:
        try:
            cache["data"] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cache["data"] = {}
        cache["mtime"] = mtime
    return cache.get("data", {})


# ---------- 各群功能白名单（默认放行；某群被 /白名单 启用任意功能后进入白名单管控）----------
# 管控群内：白名单中启用的功能命令与 @机器人聊天（需启用 chat 项）放行；
# 其余命令与被动功能（拍一拍/戳一戳、进出群、撤回记录、加群申请等）一律静默拦截。
WL_DIR = DATA_DIR / "whitelist"
WL_CHAT = "chat"  # 特殊白名单项：AI 对话（@机器人 / 回复机器人聊天）
# 管控群内始终放行的管理指令，避免白名单把权限管理本身锁死
WL_MANAGE_CMDS = {"白名单", "op", "deop", "suop", "ophelp", "停生图", "打断生图", "取消生图"}

# 旧硬编码管控群：首次接入白名单时自动沿用原放行命令集，保持既有行为不变
RESTRICT_GROUP = 1037308494
RESTRICT_ALLOW_CMDS = {
    "生图", "文生图", "图生图", "生成图片", "伊蕾娜", "邦多利",
    "help", "帮助", "禁言", "mute", "解除禁言", "解禁", "unmute",
}


def _wl_file(group_id: int) -> Path:
    return WL_DIR / f"g{group_id}.json"


def _wl_save(group_id: int, features: set[str]) -> None:
    try:
        _wl_file(group_id).write_text(
            json.dumps({"features": sorted(features)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logging.getLogger("sorting_hat.common").warning("白名单写入失败 g%d: %s", group_id, e)


def _wl_load(group_id: int) -> set[str]:
    try:
        data = json.loads(_wl_file(group_id).read_text(encoding="utf-8"))
        feats = data.get("features") or []
        return {str(f) for f in feats if isinstance(f, str)}
    except Exception:
        return set()


def _wl_ensure_legacy() -> None:
    """旧管控群首次接入时按原白名单自动建档，保证升级后行为不变。"""
    if _wl_file(RESTRICT_GROUP).exists():
        return
    _wl_save(RESTRICT_GROUP, set(RESTRICT_ALLOW_CMDS) | {WL_CHAT})


def wl_managed(group_id: int) -> bool:
    return _wl_file(group_id).exists()


def wl_features(group_id: int) -> set[str] | None:
    """返回某群白名单功能集合；未纳入白名单管控的群返回 None（默认全放行）。"""
    if group_id == RESTRICT_GROUP:
        _wl_ensure_legacy()
    if not wl_managed(group_id):
        return None
    return _wl_load(group_id)


def wl_enable(group_id: int, name: str) -> None:
    """在某群启用一个功能；首次启用即让该群进入白名单管控。"""
    cur = wl_features(group_id)
    cur = set() if cur is None else cur
    cur.add(name)
    _wl_save(group_id, cur)


def wl_disable(group_id: int, name: str) -> bool:
    """移除某群白名单中的一个功能。功能不在白名单时返回 False。"""
    cur = wl_features(group_id)
    if cur is None or name not in cur:
        return False
    _wl_save(group_id, cur - {name})
    return True


def _restrict_cmd_of(text: str) -> str | None:
    for pre in ("/", "！", "!", "／"):
        if text.startswith(pre):
            rest = text[len(pre):].strip()
            return rest.split()[0] if rest else None
    return None


def _group_managed(event) -> bool:
    gid = getattr(event, "group_id", None)
    if not gid:
        return False
    if gid == RESTRICT_GROUP:
        _wl_ensure_legacy()
    return wl_managed(gid)


def _restrict_msg_rule(event: GroupMessageEvent) -> bool:
    """白名单管控群内：未启用功能命令、未开 chat 的 @聊天、以及普通闲聊一律静默拦截。"""
    if not _group_managed(event):
        return False
    gid = getattr(event, "group_id", 0)
    text = event.get_plaintext().strip()
    if not text:
        return False
    cmd = _restrict_cmd_of(text)
    feats = wl_features(gid) or set()
    if cmd is not None:
        return cmd not in feats and cmd not in WL_MANAGE_CMDS
    if event.to_me:
        return WL_CHAT not in feats
    return True  # 管控群普通闲聊（非命令、非 @）一律拦截


restrict_msg_blocker = on_message(rule=_restrict_msg_rule, priority=-1, block=True)


@restrict_msg_blocker.handle()
async def _restrict_msg_handler(bot: Bot, event: GroupMessageEvent):
    pass  # 静默拦截：不回复、不处理、不记录


def _restrict_notice_rule(event) -> bool:
    return isinstance(event, NoticeEvent) and _group_managed(event)


restrict_notice_blocker = on_notice(rule=_restrict_notice_rule, priority=-1, block=True)


@restrict_notice_blocker.handle()
async def _restrict_notice_handler(bot: Bot, event):
    pass  # 静默拦截：拍一拍、进出群、撤回等通知全部不响应


def _restrict_request_rule(event) -> bool:
    return isinstance(event, GroupRequestEvent) and _group_managed(event)


restrict_request_blocker = on_request(rule=_restrict_request_rule, priority=-1, block=True)


@restrict_request_blocker.handle()
async def _restrict_request_handler(bot: Bot, event):
    pass  # 静默拦截：管控群加群申请不自动通过
