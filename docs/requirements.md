# AI Calendar Assistant 需求文档

## 1. 产品定位

AI Calendar Assistant 是一个自部署的私人 AI 日程管理助手。它通过 Telegram 等消息渠道接收自然语言输入，使用可配置 AI Provider 提取结构化日程信息，并通过 CalDAV 写入用户指定的日历。

第一版定位为单用户自部署工具，不做 SaaS、多租户、用户注册或公网入口管理。反向代理、HTTPS、域名和公网暴露由部署者自行通过 Caddy、Nginx、Tailscale、Cloudflare Tunnel 等工具处理。

## 2. 第一版范围

### 包含

- Docker 本地部署。
- Web Console 仅用于配置、状态和记录查看。
- 默认仅绑定本机访问，`docker-compose` 暴露为 `127.0.0.1:9527:9527`。
- 默认不要求用户手动创建 `.env`，首次启动自动生成本地密钥和管理员强密码。
- 正式部署以 `docker compose up -d` 为目标；源码开发使用 `docker-compose.dev.yml` 构建。
- Telegram 和 Discord 作为第一版消息渠道。
- Telegram Bot Token 保存后支持热重载，不要求重启容器。
- Telegram 用户授权支持一次性绑定链接和手动 user_id 添加。
- AI Provider 可配置，内置常见 Provider preset，允许自定义 Base URL。
- API Key、Telegram Token、CalDAV 密码加密保存。
- AI 模型支持拉取列表后选择；拉取失败时允许手动输入。
- Prompt 不对用户开放，由系统内置并保证结构化输出。
- CalDAV 为第一版唯一写入目标。
- Event Records 仅查看、过滤、搜索和清空，不做手动重试或编辑。

### 不包含

- SaaS 用户注册和多租户隔离。
- Slack、飞书等其他渠道实现。
- Google Calendar API。
- 一次性 `.ics` 文件导入。
- iCal Feed 订阅。
- Web 创建/修改/删除日程。
- 复杂重复规则，例如每季度、每年、每月最后一个周五、节假日规则、排除日期。

## 3. 技术栈

- 后端：FastAPI。
- Web：Jinja2 + HTMX。
- 数据库：SQLite + SQLAlchemy。
- 迁移：Alembic。
- Bot：python-telegram-bot。
- 日历：CalDAV。
- AI：OpenAI-compatible Provider 为主，Anthropic native 单独适配。
- 加密：`APP_SECRET_KEY` + 应用层加密。
- 密码哈希：bcrypt。
- 部署：Docker + Docker Compose。

## 4. Docker 部署

### 正式用户启动方式

第一版的部署目标是零配置本地启动：

```bash
docker compose up -d
docker compose logs app
```

应用默认监听容器内 `9527`，并通过 Compose 只绑定宿主机本地地址：

```text
127.0.0.1:9527:9527
```

用户访问：

```text
http://127.0.0.1:9527
```

### 开发者启动方式

源码开发或本地构建使用：

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

### 首次启动初始化

首次启动时，如果 `data/` 中还没有初始化数据，系统自动完成：

- 创建 `data/secrets.json`，保存本地 `APP_SECRET_KEY`。
- 创建 `data/app.db`，初始化 SQLite 表结构。
- 创建默认管理员账号 `admin`。
- 生成随机强密码，并只在首次启动日志中打印一次。

日志示例：

```text
AI Calendar Assistant initialized
Web UI: http://127.0.0.1:9527
Username: admin
Password: <generated-password>
Please change this password in System Settings.
```

后续重启不再打印密码，只提示应用已经初始化。

### `.env` 策略

`.env` 不是必需文件。高级用户可以使用 `.env` 覆盖默认配置，例如端口、数据库路径、Session 天数等；普通用户可以不创建 `.env`。

### 镜像与 Compose 文件

- `docker-compose.yml`：正式部署入口，目标体验是 `docker compose up -d`。
- `docker-compose.dev.yml`：开发入口，从本地源码构建镜像。
- 当前源码阶段允许 `docker-compose.yml` 使用本地 build；发布镜像后可切换为预构建镜像。

### 容器用户与文件权限

- 容器内应用进程固定使用非 root 用户 `1000:1000`。
- `data/` 尽量设置为 `700`。
- `data/secrets.json` 尽量设置为 `600`。
- 如果宿主机上 `data/` 权限导致容器无法写入，用户可执行：

```bash
sudo chown -R 1000:1000 data
chmod 700 data
```

### 升级方式

发布镜像后，用户升级流程为：

```bash
docker compose pull
docker compose up -d
```

## 5. Web Console

Web Console 是系统配置入口。Telegram / Discord 只负责日程创建、查询、修改、删除和状态查看。

### 页面

1. Login。
2. Status。
3. AI Settings。
4. CalDAV Settings。
5. Telegram Settings。
6. Event Records。
7. System Settings。

### 登录

- 第一版只有一个管理员。
- 初始用户名为 `admin`，初始密码首次启动自动生成并打印到 Docker 日志；`.env` 可作为高级覆盖方式。
- 登录后使用 Session Cookie。
- 默认 Session 有效期为 7 天，可在 System Settings 修改。
- 管理员用户名和密码可在 Console 中修改。
- 密码使用 bcrypt 哈希保存。

### System Settings

- 修改管理员用户名。
- 修改管理员密码，要求输入当前密码、新密码和确认密码。
- 修改 Session 有效期。
- 修改 Event Records 保留数量，默认 500。
- 手动清空 Event Records。
- 显示 `APP_SECRET_KEY` 是否配置，不允许在页面修改。

## 6. 数据安全

- 自部署单用户，不上传数据到第三方服务，除非用户配置的 AI Provider 和 CalDAV 服务需要。
- SQLite 数据库存放于 `data/app.db`。
- `data/secrets.json` 中的 `APP_SECRET_KEY` 用于加密敏感字段。
- 加密字段包括 AI API Key、Telegram Bot Token、CalDAV 密码。
- 敏感字段不明文回显，只显示脱敏值。
- 日志中不得输出完整密钥、密码或 Token。
- `data/app.db`、`data/secrets.json` 和可选 `.env` 都视为敏感文件，由用户自行备份和保护。

## 7. AI Provider 配置

### Provider Preset

| Provider | 类型 | Base URL |
|---|---|---|
| OpenAI | OpenAI-compatible | `https://api.openai.com/v1` |
| DeepSeek | OpenAI-compatible | `https://api.deepseek.com/v1` |
| Moonshot / Kimi | OpenAI-compatible | `https://api.moonshot.cn/v1` |
| Qwen / DashScope | OpenAI-compatible | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| OpenRouter | OpenAI-compatible | `https://openrouter.ai/api/v1` |
| SiliconFlow | OpenAI-compatible | `https://api.siliconflow.cn/v1` |
| Ollama | OpenAI-compatible | `http://127.0.0.1:11434/v1` |
| Custom | OpenAI-compatible | 用户自定义 |
| Anthropic | Anthropic native | `https://api.anthropic.com` |

### 交互

- 用户选择 Provider preset。
- Base URL 可修改。
- API Key 加密保存。
- 点击“拉取模型列表”。
- OpenAI-compatible 调用 `GET /models`。
- Anthropic 使用内置 Claude 常见模型列表。
- 拉取失败时允许手动输入模型名。
- 保存时必须选择或输入模型。
- “测试连接”发起一次轻量 AI 请求，仅用于验证配置，不写 Event Records。

### Prompt

- Prompt 不暴露给用户配置。
- 系统内置创建、修改、删除、补全缺失字段的结构化 Prompt。
- 后端必须使用 Pydantic Schema 校验模型输出。

## 8. CalDAV 配置

### 配置流程

1. 填写 CalDAV Server URL、Username、Password / App Password。
2. 点击“测试连接”。
3. 点击“拉取日历列表”。
4. 选择目标日历。
5. 保存配置。

### 规则

- 第一版只支持一个目标日历。
- 如果只发现一个日历，自动选中。
- Username 可明文显示。
- Password 加密保存且不明文回显。
- Calendar URL 明文保存。
- 默认时区为 `Asia/Shanghai`。
- 默认提醒为提前 30 分钟。
- 默认事件时长为 1 小时。
- 第一版不做 Google Calendar API。

## 9. Telegram 配置与授权

### Bot Token

- 在 Web Console 中填写。
- 加密保存。
- 支持测试连接。
- 保存后热重载 Bot：停止旧 polling，使用新 Token 初始化并启动新 polling。
- 不要求重启 Docker 容器。

### 用户授权

- 主流程为一次性绑定链接。
- Web Console 生成绑定链接：`https://t.me/<bot_username>?start=bind_<token>`。
- 绑定 token 10 分钟过期，只能使用一次。
- 用户点击链接后，Bot 收到 `/start bind_<token>` 并加入授权列表。
- 保留手动添加 Telegram user_id 作为备用方式。
- 未授权用户访问时，Bot 返回自己的 user_id，并提示到 Console 生成绑定链接。

## 10. Telegram Bot 命令

- `/start`：查看简介或处理绑定链接。
- `/help`：查看帮助。
- `/status`：查看配置状态。
- `/latest`：查看最近一条日程。
- `/upcoming`：默认查看未来 7 天日程。
- `/upcoming <days>`：查看未来 N 天日程，最大 14 天。

## 11. 日程交互规则

### 创建

用户发送自然语言日程，系统直接提取并写入 CalDAV，不二次确认。成功后返回结果。如果用户不满意，可以回复修改。

### 修改

目标匹配优先级：

1. 用户 reply 某条 Bot 日程消息，则修改该消息对应的事件。
2. 未 reply，则修改该用户最近一条事件。
3. 最近事件上下文有效期为 24 小时。
4. 找不到上下文时，提示用户回复具体日程消息。

修改时 AI 只输出变更的字段（diff），系统将其与已有日程数据合并。

### 删除

支持“删除这条”“取消刚才那个日程”等表达。目标匹配规则与修改一致。

### 缺失字段补全

如果缺少 `title` 或 `start_time`，不写入 CalDAV，直接反问：

- `未识别到开始时间，请补充。`
- `未识别到日程标题，请补充。`

用户补充后，系统结合 pending draft 继续创建。pending draft 有效期 24 小时。

### 已开始日程

如果识别到日程已开始或开始时间早于当前时间，反问：

`识别到日程已开始，是否需要添加？回复“是”添加，回复“否”取消。`

确认有效期 24 小时。

## 12. AI Intent 与 Schema

### Intent

- `create_event`
- `update_event`
- `delete_event`
- `provide_missing_fields`
- `no_event`

### Event Schema

```json
{
  "intent": "create_event",
  "event": {
    "title": "产品评审",
    "start_time": "2026-05-15T15:00:00+08:00",
    "end_time": "2026-05-15T16:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "location": "线上会议",
    "description": "讨论新版日程助手方案，记得提前准备上一版文档",
    "reminders": [{"minutes_before": 30}],
    "recurrence": null,
    "is_all_day": false
  },
  "missing_fields": [],
  "unsupported_reason": null,
  "confidence": 0.9
}
```

## 13. 重复日程

### 支持

- 每天。
- 每周几。
- 每个工作日。
- 每月几号。
- 无结束重复事件。
- `until` 结束日期。
- `count` 重复次数。

### 暂不支持

- 每季度。
- 每年。
- 每月最后一个周五。
- 中国法定节假日。
- 排除日期。
- 复杂组合规则。

复杂重复规则提示：

`识别到重复日程，但当前仅支持：每天、每周几、每个工作日、每月几号。请改写后重试。`

## 14. 时间解析规则

- 默认时区：`Asia/Shanghai`，可配置。
- 当前时间和时区必须传给 AI。
- 未说年份时，使用最近的未来日期。
- 日期已过但年份缺失时，推到下一年。
- 上午默认 09:00。
- 中午默认 12:00。
- 下午默认 14:00。
- 晚上默认 19:00。
- 只有日期无时间默认 09:00。
- 默认时长 1 小时。
- 支持全天事件，例如生日、纪念日、放假、全天、整天。

## 15. Telegram 回复风格

第一版使用纯文本，不做按钮。回复可使用 emoji，但要保持清晰。

创建成功示例：

```text
✅ 日程已安排好啦！

📌 标题：和张三开会
🕒 时间：2026-05-14 15:00 - 16:00
📍 地点：会议室A
⏰ 提醒：提前 30 分钟

想改的话，直接回复这条消息：
“时间改成下午4点”
“地点改成线上会议”
“删除这条”
```

当前版本只有一个日历，成功回复不显示日历名。

## 16. Event Records

### 展示字段

- 时间。
- 来源。
- 用户。
- 操作。
- 标题。
- 开始时间。
- 是否重复。
- 状态。
- 错误。
- 原始输入。
- 结构化结果 JSON。
- CalDAV href / uid。

### 功能

- 按状态过滤：success / failed / pending。
- 按关键词搜索原始输入和标题。
- 默认保留最近 500 条。
- 超过保留数量时定期删除旧记录。
- 支持手动清空记录。
- 清空记录不删除 CalDAV 中已创建的日程。

## 17. 失败处理

- AI 未识别日程：`未识别到日程信息，请补充时间和事件内容。`
- 缺关键字段：直接说明缺什么并请求补充。
- CalDAV 写入失败：保留本地记录，状态为 failed，返回脱敏错误。
- 复杂重复规则：直接说明当前支持范围。

## 18. 项目结构

```text
ai-calendar-assistant/
├── app/
│   ├── main.py
│   ├── core/
│   ├── db/
│   ├── ai/
│   ├── calendar/
│   ├── channels/
│   ├── integrations/
│   ├── web/
│   └── services/
├── data/
├── docs/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```
