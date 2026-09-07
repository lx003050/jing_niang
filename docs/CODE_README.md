# 代码阅读引导

一个跑在 NoneBot2 + OneBot V11（NapCat）上的 QQ 群机器人工程，人格设定为"小鲸娘"。
本文按"入口 → 调度 → 各能力 → 数据/配置"的顺序带读源码，看完即可定位绝大多数功能。

---

## 1. 项目骨架

```
sorting_hat/
├─ bot.py                     # NoneBot 入口：注册驱动/适配器、加载 plugins
├─ requirements.txt
├─ .env.example               # 配置模板（复制为 .env 填真实值）
├─ .gitignore                 # 排除 .env/data/deploy/探针脚本等敏感物
├─ src/plugins/               # ★ 全部业务代码都在这里
│  ├─ __init__.py
│  ├─ common.py               # 共享底座：配置模型、帮助注册表、白名单闸门、鲸娘默认人格
│  ├─ admin_tools.py          # OP/管理：/op /deop /ophelp /白名单、数据存取工具
│  ├─ events.py               # /help 自动汇总、@机器人兜底、戳一戳
│  ├─ sorting_hat.py          # ★ AI 对话核心：成员识别、工具调用(invoke_feature)
│  ├─ persona.py              # /切换人格 管理
│  ├─ image_gen.py            # /生图：云端多机型 + -local 本机 sd.cpp + /停生图
│  ├─ irena.py  bandori.py    # 图库随机图（伊蕾娜/邦多利）
│  ├─ fun_tools.py            # /展示 /入典 /栽桩 等整活
│  ├─ mute.py                 # /禁言 /解除禁言
│  ├─ recall.py               # /查看撤回（群消息记录）
│  ├─ welcome.py              # 进群欢迎（20 条鲸娘文案）
│  ├─ mirror.py               # /镜像 网站镜像（需服务器侧脚本）
│  ├─ repeat.py  summary.py   # 复读 / 省流
│  ├─ keyword_reply.py        # 关键词回复
│  ├─ qa.py  request_approve.py  scheduler_tasks.py  sorting_system.py
└─ data/                      # 运行时数据（不入库，见第 5 节）
```

阅读顺序建议：`common.py → events.py → admin_tools.py → sorting_hat.py → image_gen.py → 其它`。

---

## 2. 入口与插件加载

- `bot.py` 创建 NoneBot App，加载 `nonebot_plugin_apscheduler` 与 `src.plugins` 下所有插件。
- 每个 `src/plugins/*.py` 是一个 NoneBot 插件模块，顶层 `on_command/on_message/on_notice` 即注册匹配器。

> 匹配优先级：值**越小越先执行**。例：白名单闸门 `priority=-1` 最先；各指令 `priority=1/5`。

---

## 3. 命令如何被识别与汇总

- 每条指令用 `on_command("名字", aliases={...}, priority=1, block=True)` 注册。
- `common.register_help("/命令", "说明")` 把命令写进帮助表 `HELP_DESC`；`hide_help()` 把它从普通 `/help` 隐藏（如 `/白名单`、`/停生图`）。
- `events._collect_command_forms()` 遍历所有已加载插件的 `CommandRule`，把主名+别名汇总成命令表。
- `/help` = 鲸娘人格文案 + 按命令表自动生成的清单（含别名）；`/ophelp` 额外列出隐藏的管理指令。

---

## 4. 各群功能白名单（读懂它就读懂了一半运维逻辑）

代码位置：`common.py` 底部 + `admin_tools.py` 底部 `/白名单`。

设计是 **默认放行、启用即管控**：

- 白名单存 `data/whitelist/g{群号}.json`（`features` 数组，存"每个命令的所有形式：主名+别名"）。
- `wl_features(group)`：没该文件 → 返回 `None` = 未管控、全功能可用；有文件 → 返回已启用集合。
- `common` 中三个 `priority=-1` 的"静默闸门"（消息/通知/请求）在**已管控群**里拦截一切未启用内容：
  - `_restrict_msg_rule`：命令按"原始词"匹配白名单；`chat` 项控制 @机器人 的 AI 对话；未启用时普通闲聊也被拦。
  - `_restrict_notice_rule` / `_restrict_request_rule`：拍一拍、进出群、撤回、加群申请等通知类全部不响应。
- 管理指令（`WL_MANAGE_CMDS`：/白名单 /op /deop /suop /ophelp /停生图）**永远放行**，避免把权限管理锁死。
- `/白名单`（仅 OP）：
  - 无参 → 列出本群已启用功能（按主名去重显示）
  - `/白名单 /功能名` → 启用该功能（会一次性写入主名+全部别名，如 `/白名单 /栽赃` 实际开的是 `栽桩` 整组）
  - 末尾加 `-o`（或 `-移除`）→ 整组移除
- `common.RESTRICT_GROUP`（.env: `RESTRICT_GROUP`）：旧硬编码管控群的"首次自动建档"来源，改文件后群行为不变。

> 排查"群里某功能不生效"：先看该群是否有 `data/whitelist/g{群}.json`，再确认目标命令的所有形式都在 features 里。

---

## 5. 运行数据与配置

### data/（不入库，各自用途）
| 文件/目录 | 用途 |
|---|---|
| `ops.json` | /op 任命的管理员 |
| `monkey.json` `pig.json` `keywords.json` | 关键词/彩蛋数据 |
| `whitelist/g{群}.json` | 各群功能白名单 |
| `personas/` `model_overrides/` | 按群/私聊隔离的人格与对话模型 |
| `recall_msgs.jsonl` `recalls.jsonl` | 撤回找回的记录 |
| `qa_images/` | 生图输出与问答图片（NapCat 挂载为 `/app/napcat/qa_images`） |
| `summary_state.json` `schedule.json` 等 | 调度/统计状态 |

### 配置（.env，见 .env.example）
- `OPENAI_* / AI_*`：主对话（翻译/QA）通道与模型。
- `MOYUU_* / PARATERA_* / SENSENOVA_*`：各生图平台的 Key/Base/模型参数。
- `AI_VISION_*`：出图后内容审查通道（本地生图用）。
- 隐私项 `ROOT_SEED / RESTRICT_GROUP / ROOT_ONLY_GROUPS / MIRROR_BASE`：替代曾经的硬编码 QQ/群号/服务器地址。

> 加配置：在 `common.AIConfig` 加一个字段（如 `my_flag: bool = False`），.env 写 `MY_FLAG=true`，代码里 `ai_config.my_flag` 取用。

---

## 6. AI 对话链路（sorting_hat.py）

1. 收到 @机器人/私聊 消息（priority=1 的 matcher）。
2. 组装上下文：`[昵称(QQ号)]` 前缀标识每条发言 → 不同成员在提示词里被当作独立的人（第 1 项"成员识别"增强点）。
3. 记忆注入：把近期历史 + 当前群人格（personas）拼成 system prompt。
4. 携带工具 `_AI_TOOLS` 调对话模型：
   - `send_to_azkaban`：老"判刑"通路
   - `invoke_feature`：AI 在对话里代触发 `/help /生图 /伊蕾娜 /邦多利`，**受当前群白名单约束**（`_feature_allowed`）
5. 模型返回 tool_call 时执行对应函数，把结果作为后续消息继续对话；白名单未开放的功能由 AI 婉拒。

> 想给 AI 加一个"对话中可用"的功能：在 `_FEATURE_CMDS` 登记主名+同义词，并在 `_ai_invoke_feature` 里写执行逻辑，再保证该功能在群白名单/或未管控群里开放。

---

## 7. 生图链路（image_gen.py）

- 入口 `/生图`（别名 文生图/图生图/生成图片），priority=5。
- 参数解析在 `image_handler`：`-glm/-qwen/-seed/-seedp/-anime/-gemini/-gemini3.1/-sense` 互斥选云端机型；`-o` 跳过默认提示词；`-local` 走本机推理；`-nsfw` 仅根管理员。
- 云端：`_images_call_with_retry` 自动重试渠道类错误；`-ensure/-e` 审查报错时自动追加安全措辞重试。
- 本地（`_local_generate`）：
  - 调 `/opt/sdrel/sd.sh`（sd.cpp，glibc 2.39 loader）跑 SD1.5 GGUF。
  - `-s 宽x高` 改尺寸（默认 384x384，32 对齐，夹 256–1024）、`-step 次数` 改步数（默认 15）。
  - 中文提示词先经云端 LLM 翻译成英文（`_local_prompt_en`）；翻译结果被判定为拒答（`_is_refusal_text`）则中止并提示换英文。
  - 非 `-nsfw` 时出图后经视觉模型审查（`_audit_local_image`），不过不发。
  - 全局互斥锁 + `_LOCAL_SD_PROC` 句柄；`/停生图`（OP）SIGTERM 打断、8 秒后 SIGKILL。
- 失败统一回复"画布被打翻了，请尝试修改 prompt"，细节只进日志。

---

## 8. 如何新增一个指令（三步走）

1. 在任一 `plugins/*.py`（新建文件需在顶部 `from .common import ...`）注册：
   ```python
   from nonebot import on_command
   from nonebot.adapters.onebot.v11 import MessageEvent
   from nonebot.params import CommandArg
   from .common import register_help, hide_help

   my_cmd = on_command("我的功能", priority=1, block=True)
   register_help("/我的功能", "说明")
   # hide_help("/我的功能")   # 仅管理员才显示时打开

   @my_cmd.handle()
   async def h(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
       await my_cmd.finish("结果")
   ```
2. `/help` 自动收录（无需手改）。
3. 该群若在白名单管控中：OP 发 `/白名单 /我的功能` 启用（或 `/白名单 /我的功能 -o` 移除）。

---

## 9. 部署要点（供维护者，非公开信息）

- 运行：`pm2 start sorting-hat`（工作目录为工程根，入口 `bot.py`）。
- 改代码后：`pm2 restart sorting-hat`；白名单 JSON 改完即生效（每次读取文件，无需重启）。
- 本机 sd 相关路径：`/opt/sdrel/sd.sh`、模型 `/opt/models/sd15-Q4_0.gguf`。
- 服务端内存仅 3.5GB：`-local` 建议 ≤512x512，超大尺寸会被系统 OOM 杀掉。

---

## 10. 一句话地图

| 你想找… | 去看… |
|---|---|
| 人格 / 默认 system prompt | `common.CAT_PERSONA`、`sorting_hat.DEFAULT_SYSTEM_PROMPT` |
| @机器人不回复 | 群白名单是否含 `chat`；`sorting_hat` 是否启用 |
| 功能被静默拦截 | `common._restrict_msg_rule`、`data/whitelist/g{群}.json` |
| /help 文案 | `events._build_help_text` |
| AI 代触发功能 | `sorting_hat._FEATURE_CMDS` / `_ai_invoke_feature` |
| 本地生图失败 | `/tmp/sd_local_*.log`（服务器） |
| 退群/进群发言 | `welcome.py`（只保留了进群欢迎） |
