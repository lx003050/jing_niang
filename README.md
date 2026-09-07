# 鲸娘 QQ 机器人（NoneBot2）

一个运行在本机的 QQ 机器人，人格是又懒又粘人的小鲸娘**鲸娘**（自称"鲸娘"，最爱米饭）。基于 **NoneBot2 + NapCat (OneBot 11)**，支持接入任意 **OpenAI 兼容 API**（DeepSeek、通义千问、Moonshot、OpenAI 等）作为 AI 大脑。

## 功能一览

| 场景 | 说明 |
| --- | --- |
| AI 智能体 | 接入 AI 后自动进入「鲸娘」人设：@它 / 私聊它，就会以鲸娘的口吻对话、答疑 |
| 关键词对话 | 不接 AI 也能聊：命中 `data/keywords.json` 中的关键词即回复（支持热更新） |
| 简单问答 | 正则/包含匹配问答，见 `data/qa.json` |
| 拍一拍 | 被戳一戳会做出鲸娘风格的回应 |
| @机器人 | 不接 AI 时 @它 会给出帮助；接 AI 后交给 AI |
| 进群 | 新成员入群鲸娘式欢迎（随机 20 条文案） |
| 复读 | 连续 3 条相同消息复读一次（30 秒冷却） |
| 定时消息 | 按 cron 定时向指定群发送问候，见 `data/schedule.json` |
| 今日分院 | `/今日分院` 每天一次，四大院 + 阿兹卡班（AI 判断/随机 5%），附带结果合成图；抽到阿兹卡班需服刑期满再分 |
| 阿兹卡班 | `/判刑 N @某人`（管理员，1~30 天）、`/赦免 @某人`（管理员） |
| 指令 | `/help` 帮助、`/reset` 重置 AI 对话记忆 |

## 架构

```
QQ 账号 ──NapCat(协议端)──> OneBot 11 消息 ──> NoneBot2(机器人框架) ──> 各插件
                                                          │
                                                          └──> OpenAI 兼容 API（AI 鲸娘）
```

## 快速开始

### 1. 安装并启动 NapCat（协议端）

NapCat 已放在 `napcat/` 文件夹内，它需要注入**官方 QQ 客户端**运行：

1. 确认本机已安装最新版**官方 QQ 客户端**（[官网下载](https://im.qq.com/)）。
2. 双击运行 `napcat\napiLoader.bat`（或右键"以管理员身份运行"）。
3. NapCat 启动后会拉起 QQ 客户端，**扫码登录你的机器人 QQ 账号**。
4. 打开 NapCat WebUI（默认 `http://127.0.0.1:6099/webui`），在网络配置中开启 **正向 WebSocket 服务器**，端口保持 `3001`（默认即可）。
   - 若你选择用反向 WS：在 NapCat 新建「反向 WebSocket」连接 `ws://127.0.0.1:8080`，并在 `.env` 中改用 `ONEBOT_WS_REVERSE_*` 配置。

### 2. 安装依赖

```powershell
cd d:\sorting_hat
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/
```

> 本机已装 Python 3.12；如未安装，请先从 python.org 或 `winget install Python.Python.3.12` 安装。

### 3. 配置 `.env`

复制 `.env.example` 为 `.env`（已存在），至少确认：

```ini
# NapCat 正向 WS 地址（默认即可）
ONEBOT_WS_URLS=["ws://127.0.0.1:3001"]

# AI（可选，不填则运行无 AI 模式）
AI_ENABLED=true
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1
AI_MODEL=deepseek-chat
```

### 4. 启动

```powershell
.\.venv\Scripts\python bot.py
```

看到 `OneBot V11 | WebSocket | Connected` 即连接成功。

## 数据文件（可热编辑，无需重启）

| 文件 | 作用 |
| --- | --- |
| `data/keywords.json` | 关键词 -> 回复列表（随机取一条） |
| `data/qa.json` | 问答条目，`regex: true` 表示用正则匹配 |
| `data/schedule.json` | 定时任务，`cron` 为 5 段 cron 表达式，`groups` 填要发送的群号 |
| `data/system_prompt.md` | 鲸娘 AI 人设提示词（可随意修改） |

## AI 接入示例

- **DeepSeek**：`OPENAI_BASE_URL=https://api.deepseek.com/v1`，`AI_MODEL=deepseek-v4-flash`（或 `deepseek-chat`）
- **通义千问**：`OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`，`AI_MODEL=qwen-plus`
- **Moonshot**：`OPENAI_BASE_URL=https://api.moonshot.cn/v1`，`AI_MODEL=moonshot-v1-8k`
- **OpenAI 官方**：`OPENAI_BASE_URL=https://api.openai.com/v1`，`AI_MODEL=gpt-4o-mini`

## 常见问题

- **连不上 NapCat / 一直重连**：确认 NapCat 已启动且开启了正向 WS 服务器（端口 3001），且 `.env` 中的地址端口一致。
- **AI 不回话**：检查 `AI_ENABLED=true`、`OPENAI_API_KEY` 已填、`OPENAI_BASE_URL` 是否正确；群聊中需要 **@机器人**。
- **想让机器人参与所有对话**：目前 AI 模式只响应 @/私聊，避免打扰群聊；这是有意设计。
- **依赖安装失败**：国内网络可加镜像参数 `-i https://mirrors.cloud.tencent.com/pypi/simple/`。
