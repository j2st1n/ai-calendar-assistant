# AI Calendar Assistant

自部署的私人 AI 日程管理助手。通过 Telegram / Discord / WeChat 对话自然语言与图片输入，AI 自动提取、智能修改、限时撤销并写入 CalDAV 日历。提供现代 Web 控制台、初次配置向导、多渠道集中管理、服务监控仪表盘与只读日历时间轴。

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version: v1.21.0](https://img.shields.io/badge/version-v1.21.0-blue)](https://github.com/j2st1n/ai-calendar-assistant/releases/tag/v1.21.0)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/j2st1n/ai-calendar-assistant/pkgs/container/ai-calendar-assistant)

---

## ✨ 核心特性矩阵

### 1. 智能日程处理
- 📅 **自然语言提取**：在 Telegram / Discord / WeChat 对话中发送「明天下午 3 点和张三开会」，AI 自动提取时间、地点、标题、描述并同步至 CalDAV 日历。
- 📸 **图片识别日程**：发送日程截图、海报或会议通知照片，系统调用多模态识图模型 OCR 提取日程；支持 WeChat 加密图片本地下载与解密。
- ✏️ **Diff 变更比对**：回复或引用日程确认消息即可修改（如「改到下午 4 点」），Bot 输出直观的前后变化对比（`原值 ➔ 新值`），避免盲改。
- 🧭 **精准回复定位与歧义阻断（Ambiguity Guard）**：按渠道与会话精准锁定上下文目标；当存在多条歧义候选日程时，坚决阻断模糊盲猜，转入交互式选择状态机并引导用户输入序号确认。
- ⏪ **限时撤销与 ETag 防覆盖**：支持在 10 分钟内发送「撤销 / undo / 恢复 / 回退」一键回滚最近的创建、修改或删除操作；内置全链路快照与 CalDAV 远端 ETag / sha256 校验，并发状态下外部有新变动时自动阻断，避免数据覆盖。
- 📦 **多日程批量聚合与精准局部重试**：单次输入包含多项日程时，聚合为单条结构化批处理报告；基于 RFC 4122 UUIDv5 算法派生确定性 UID，幂等防重；重试时仅重试失败项，杜绝重复写入日历。

### 2. 现代 Web 控制台（Web Console）
- 🚀 **开箱向导 (`/console/wizard`)**：4 步极简向导（AI 供应商 → CalDAV 日历 → 聊天渠道 → 首条日程端到端实测验证），新实例 2 分钟完成全链路闭环配置。
- 📊 **服务运行状态仪表盘**：首页全量呈现 5 大核心服务（AI 主模型、CalDAV 日历、微信、Telegram、Discord）实时健康指示灯，直观展示连接与会话状态，支持一键直达对应管理面板。
- 💬 **统一消息渠道管理 (`/console/channels`)**：采用类似 macOS / Linear 的现代分段胶囊 Tab 容器（Segmented Control），整合 WeChat、Telegram、Discord 状态与配置面板；微信扫码接入卡片平铺直观，支持运行时自愈与实时状态轮询。
- 🎛️ **AI / 日历双栏管理架构**：
  - **AI 设置 (`/console/ai`)**：左栏配置模型参数，右栏集中展示模型探针、Schema 严格遵从度校验、识图模型测试与协议指引。
  - **日历设置 (`/console/caldav`)**：左栏表单配置，右栏实时测试 CalDAV 连接、日历列表发现、只读写入权限探针与自动回滚清理，支持服务商预设、IANA 时区选择与状态双向复核。
- 📅 **只读日历时间轴 (`/console/calendar`)**：实时拉取 CalDAV 日程数据，支持按天/周/月/全部跨度筛选，配置 30 秒内存短缓存，提供带来源标示与时区感知的轻量日程时间线。
- 📥 **失败收件箱与事件记录 (`/console/events`)**：手机卡片化展示，支持按渠道、状态与处理日期多维度检索；历史失败智能分类（AI / CalDAV / Channel）并提供针对性处置建议；支持单条与批量事件局部防重重试（UID 幂等锁）。
- 🎨 **多主题与可访问性 (a11y)**：支持跟随系统、浅色 (Light)、深色 (Dark) 三种现代设计主题平滑切换；WCAG AA 级高文本对比度与全局焦点环设计，适配全平台触控体验。

### 3. 连接与兼容生态
- 🤖 **自定义 AI 供应商**：支持 OpenAI、DeepSeek、Anthropic、OpenRouter、Ollama、SiliconFlow 等任意兼容 OpenAI 协议的模型，支持独立配置识图模型。
- 📆 **CalDAV 日历广泛兼容**：深度适配 iCloud、群晖 Synology Calendar、163/QQ 邮箱日历、Nextcloud 及通用 CalDAV 服务；支持多日历发现与自动主体溯源。
- 💬 **主流通讯平台覆盖**：
  - **Telegram**：基于 Long Polling 机制，支持群组、私聊、防串号绑定授权与 Bot 命令体系。
  - **Discord**：支持 Bot Token 接入、私聊、Thread 以及频道内 `@Bot` 互动。
  - **WeChat**：支持原生微信个人扫码接入（基于 iLink 协议），支持二维码实时刷新、自动重连与会话冷却自愈。

### 4. 企业级隐私与安全
- 🔐 **自部署、单用户完全隔离**：日程数据与私钥保存在本地宿主机，绝不上传第三方。
- 🛡️ **现代登录防护**：支持 WebAuthn Passkeys 通行密钥（指纹/人脸）、TOTP 双因素认证（配有一次性灾备恢复码）、Cloudflare Turnstile 人机验证。
- 🔒 **数据加密与安全基线**：密码基于 bcrypt 哈希，API Token 与凭据基于系统密钥 AES 加密存储；默认只监听 `127.0.0.1` 本地回环，支持公网模式（安全 Cookie、HSTS、CSP、同源写校验与防爆破限流）。

---

## 🚀 快速开始

### 1. 使用 Docker Compose 一键启动

创建 `docker-compose.yml`：

```yaml
services:
  app:
    network_mode: host
    image: ghcr.io/j2st1n/ai-calendar-assistant:v1.21.0
    environment:
      PUBLIC_ORIGIN: ${PUBLIC_ORIGIN:-}
      WEBAUTHN_RP_ID: ${WEBAUTHN_RP_ID:-}
      TRUSTED_HOSTS: ${TRUSTED_HOSTS:-127.0.0.1,localhost}
      TRUST_PROXY_HEADERS: ${TRUST_PROXY_HEADERS:-false}
      SECURE_COOKIES: ${SECURE_COOKIES:-false}
    user: "1000:1000"
    restart: unless-stopped
    volumes:
      - ./data:/app/data
    command: uvicorn app.main:app --host 127.0.0.1 --port 9527
```

拉取并启动：

```bash
# 启动服务
docker compose up -d

# 查看初始管理员随机密码
docker compose logs app
```

控制台初次启动日志会输出：

```text
==================================================
AI Calendar Assistant initialized
Web UI: http://127.0.0.1:9527
Username: admin
Password: xxxx-xxxx-xxxx-xxxx
Please change this password in System Settings.
==================================================
```

### 2. 访问控制台与开箱向导

在浏览器中访问 `http://127.0.0.1:9527`，使用日志中的初始密码登录。
登录后推荐直接进入 **初次配置向导 (`/console/wizard`)**，按照清晰指引 4 步完成系统接入：
1. **配置 AI 供应商**：填入 Base URL、API Key，拉取并选择主模型。
2. **连接 CalDAV 日历**：选择日历服务商预设（如 iCloud），填写账号凭据并选择日程写入的目标日历。
3. **选择启用消息渠道**：按需扫码绑定 WeChat，或填入 Telegram / Discord Bot 凭据。
4. **端到端首条日程验证**：在向导中输入一条测试日程指令，直接触发 AI 解析与 CalDAV 真实写入，验证全链路联通。

---

## ⚙️ 系统配置指南

### 1. AI 设置 (`/console/ai`)
采用双栏响应式架构：
- **主模型配置**：输入 Base URL（如 `https://api.deepseek.com/v1` 或 `https://api.openai.com/v1`）和 API Key。点击「拉取模型列表」一键同步可用模型并选择。
- **识图模型**：支持配置独立的 Vision 模型（如 `gpt-4o`、`gemini-1.5-pro`），专门负责图片 OCR 与会议通知图片解析。
- **右栏实时状态与探针**：
  - **基础连通性测试**：探测 API 连通性与鉴权有效性。
  - **Schema 结构化遵从度测试**：发送标准日程提取测试探针，验证所选模型是否完美符合 JSON Schema 输出规范。
  - **识图连通性测试**：验证 Vision 模型的 Base64 多模态解析能力。

### 2. 日历设置 (`/console/caldav`)
采用双栏布局与智能复核机制：
- **服务商预设**：内置 iCloud、群晖 Synology、163 邮箱、QQ 邮箱等主流服务商快速模板，自动填入推荐的 CalDAV 根地址。
- **时区与偏好**：支持标准的 IANA 时区（如 `Asia/Shanghai`），确保跨国/异地时区日程换算准确无误。
- **日历发现与拉取**：测试连接通过后，系统自动递归探测远端 CalDAV 主体并拉取日历清单，选择目标日历保存。
- **右栏状态卡片与权限探针**：
  - 支持「写入权限测试」：在远端写入一个带自动回滚标记的测试临时事件，并立即回读验证与强制删除清理，做到 0 脏数据残留。
  - 支持「一键复核 / 验证已保存配置」：跨版本或长周期后一键核验连接状态，双向联动首页与日历徽标指示灯。

### 3. 统一消息渠道管理 (`/console/channels`)
集成现代分段胶囊 Tab 设计，统一集中管理各大聊天平台：

#### 微信接入 (WeChat)
- **平铺式扫码接入**：敞开式展示二维码卡片，直接点击「获取二维码」，使用手机微信扫码。
- **iLink 协议运行时**：扫码成功后系统自动保存 Token 并拉起后台长轮询任务接收消息。
- **自愈重连机制**：当 Token 失效时，控制台自动显示状态预警横幅与重新扫码抽屉，扫码后自动平滑切回在线状态，无需重启容器。

#### Telegram 接入
- 从 [@BotFather](https://t.me/BotFather) 获取 Bot Token，填入并保存。
- 点击「生成绑定链接」，在 Telegram 中打开链接点击 Start 完成一对一安全身份绑定，有效防范未授权访问。

#### Discord 接入
- 在 Discord Developer Portal 创建 Bot 并获取 Bot Token，填入控制台保存。
- 启动后在控制台添加授权的用户或频道 ID。在私聊、Thread 中可直接自然语言对话；群组频道中 `@Bot` 即可触发日程解析。

### 4. 只读日历时间轴 (`/console/calendar`)
- 实时与 CalDAV 服务端保持只读同步，在 Web 端以时间线卡片呈现近期的全部待办与会议。
- 支持按「本周 / 本月 / 全部」范围切换，内置 30 秒短周期内存缓存机制，既保障信息新鲜度，又避免高频刷新打满远端 CalDAV 速率限制。

### 5. 失败收件箱与重试机制 (`/console/events`)
- **失败结构化打标**：自动对失败记录打上阶段标记（AI 提取失败、CalDAV 写入超时、渠道消息发送中断等），并给出清晰的中文处置建议。
- **局部精准重试**：对于偶发网络抖动或 CalDAV 拥堵导致的写入失败，支持在 Web 界面点击「重试写入」；重试接口受并发锁与 UID 幂等校验保护，绝对不会生成重复日程。

---

## 💬 交互与使用范式

### 1. 单条日程创建与自然语言修改
```text
用户：明天下午 3 点和李总在会议室 A 讨论季度预算
Bot ：✅ 日程已安排好啦！
      📌 标题：和李总讨论季度预算
      🕒 时间：2026-09-10 15:00 - 16:00
      📍 地点：会议室 A

用户：[引用上述消息] 改到下午 4 点半，地点改到第二会议室
Bot ：✅ 日程已更新！
      🔄 修改对照：
      • 时间：15:00 - 16:00 ➔ 16:30 - 17:30
      • 地点：会议室 A ➔ 第二会议室
      📌 标题：和李总讨论季度预算
```

### 2. 智能歧义阻断 (Ambiguity Guard)
当用户指令匹配到多条候选日程时，系统主动拦截盲猜行为：
```text
用户：帮我取消明天的项目会
Bot ：⚠️ 发现 2 条相关的日程，请回复序号确认要操作哪一项：
      [1] 明天 10:00 - 11:00 项目周会 (会议室 A)
      [2] 明天 14:00 - 15:00 跨部门项目评审 (线上)
      （回复「取消」可终止本次操作）

用户：1
Bot ：🗑️ 已成功删除日程：项目周会 (2026-09-09 10:00 - 11:00)
```

### 3. 限时撤销机制 (Undo & ETag Check)
```text
用户：删除刚才的会议
Bot ：🗑️ 已删除日程：产品规划评审 (2026-09-15 14:00 - 15:00)

用户：撤销
Bot ：↩️ 已成功撤销先前的操作！日程已恢复：产品规划评审 (2026-09-15 14:00 - 15:00)
```
*注：撤销支持「撤销 / undo / 恢复 / 回退」，有效窗口为 10 分钟。执行撤销前会自动对比 CalDAV 远端 ETag，若检测到该日程已被其他客户端外部修改，将主动阻断以防覆盖。*

### 4. 批量日程聚合与局部重试
```text
用户：下周一上午 9 点部门晨会，周三下午 2 点技术分享会，周五下午 5 点周总结
Bot ：📦 批量日程处理完成 (共 3 项，成功 3 项)：
      1. ✅ 部门晨会 (2026-09-14 09:00 - 10:00)
      2. ✅ 技术分享会 (2026-09-16 14:00 - 15:00)
      3. ✅ 周总结 (2026-09-18 17:00 - 18:00)
```
*注：批量处理采用 RFC 4122 UUIDv5 确定性算法；若部分项因网络原因失败，重新发送或在控制台重试仅会处理失败项，绝不产生重复日程。*

### 5. 机器人常用指令

| 命令 | 说明 |
|---|---|
| `/help` | 查看详细的使用帮助与自然语言范例 |
| `/list [天数]` | 查询未来日程清单，默认 7 天，最多可查 14 天（每条单独推送，支持直接引用修改） |
| `/latest` | 快速查看最近的一条有效日程 |
| `/status` | 检查当前 AI 模型、识图模型与 CalDAV 日历的实时联通状态 |

---

## 🛡️ 安全与生产部署

### 1. 安全基线
- **凭据加密**：所有敏感密钥（AI API Key、Telegram Token、WeChat Token、CalDAV 密码、TOTP 种子、Turnstile Secret）均采用系统主密钥强加密隔离落库。
- **会话与认证**：管理员密码经高强度 bcrypt 加密；支持 Passkeys (WebAuthn) 硬件认证与 TOTP 2FA，登录支持一次性灾备恢复码。
- **网络边界**：默认仅绑定 `127.0.0.1`。需暴露公网时，请配合可信反向代理（如 Nginx、Caddy）并配置 HTTPS。

### 2. 公网生产环境配置 (HTTPS + 通行密钥)

创建 `.env` 文件：

```env
APP_VERSION=v1.21.0
PUBLIC_ORIGIN=https://calendar.example.com
WEBAUTHN_RP_ID=calendar.example.com
TRUSTED_HOSTS=calendar.example.com,127.0.0.1,localhost
TRUST_PROXY_HEADERS=true
SECURE_COOKIES=true
```

环境变量说明：

| 变量 | 示例 | 说明 |
|---|---|---|
| `APP_VERSION` | `v1.21.0` | 指定镜像版本标签，生产环境建议固定版本 |
| `PUBLIC_ORIGIN` | `https://calendar.example.com` | 控制台公网完整 Origin，末尾切勿加 `/` |
| `WEBAUTHN_RP_ID` | `calendar.example.com` | 通行密钥 RP ID，仅填域名，无协议/端口/路径 |
| `TRUSTED_HOSTS` | `calendar.example.com,127.0.0.1,localhost` | 允许的主机头白名单 |
| `TRUST_PROXY_HEADERS` | `true` | 置于可信反代之后时开启，确保客户端 IP 限流生效 |
| `SECURE_COOKIES` | `true` | 仅允许通过 HTTPS 传输 Cookie，公网部署必须开启 |

以 Caddy 反向代理为例：

```caddyfile
calendar.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:9527
}
```

部署启动：
```bash
docker compose up -d
```

---

## 🔄 版本升级与数据备份

### 升级命令

```bash
# 升级至最新版本
docker compose pull && docker compose up -d

# 或指定版本升级 / 回滚
APP_VERSION=v1.21.0 docker compose pull app
APP_VERSION=v1.21.0 docker compose up -d --force-recreate app
```

### 备份与恢复
系统所有核心数据均持久化保存在 `./data` 目录中：
- `data/app.db`：SQLite 数据库（日程快照、事件记录、通行密钥、用户配置）
- `data/secrets.json`：AES 加密主密钥

在 Web 控制台的「系统设置 ➔ 数据与备份」中支持一键打包下载备份文件。在发生灾难恢复时，只需将备份文件还原至 `data/` 目录即可无缝冷启动恢复。

---

## 🏛️ 系统架构与设计

### 架构示意

```text
+-----------------------------------------------------------------------------------+
|                                用户交互与接入层                                    |
|   +--------------------+   +---------------------+   +------------------------+   |
|   |  WeChat (iLink)    |   |  Telegram (Polling) |   |  Discord (Gateway/API) |   |
|   +---------+----------+   +----------+----------+   +-----------+------------+   |
|             |                         |                          |                |
|             +-------------------------+--------------------------+                |
|                                       |                                           |
|                                       v                                           |
|   +---------------------------------------------------------------------------+   |
|   |                  统一消息处理器 (Unified MessageProcessor)                |   |
|   |  * Diff 变更对照引擎              * Ambiguity Guard 歧义阻断状态机        |   |
|   |  * Undo Manager 限时撤销引擎      * UUIDv5 批量日程确定性幂等调度器       |   |
|   +-----------------------------------+---------------------------------------+   |
+---------------------------------------|-------------------------------------------+
                                        |
         +------------------------------+------------------------------+
         |                                                             |
         v                                                             v
+------------------------------------+        +-------------------------------------+
|         AI 智能解析与视觉层        |        |          CalDAV 同步适配层          |
|  * AIService (多 Provider 抽象)    |        |  * CalDAVClient (RFC 4791 / 5545)   |
|  * VisionService (图片多模态 OCR)  |        |  * 远端 ETag / sha256 并发比对      |
|  * JSON Schema 严格校验与提取器    |        |  * 自动回滚权限探测与时区转换计算   |
+-----------------+------------------+        +------------------+------------------+
                  |                                              |
                  +----------------------+-----------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                             Web 控制台与核心服务层                                |
|   +---------------------------------------------------------------------------+   |
|   |   FastAPI Web 核心:                                                       |   |
|   |   * 初次配置向导 (/console/wizard)       * 统一渠道中心 (/console/channels)   |   |
|   |   * 运行状态仪表盘 (/console)            * 只读日历时间轴 (/console/calendar) |   |
|   |   * 失败收件箱与防重重试 (/console/events)* AI/日历双栏配置卡片               |   |
|   |   * Passkeys / TOTP 2FA / Turnstile 安全中间件与登录限流                  |   |
|   +---------------------------------------------------------------------------+   |
|   |   数据持久化与安全管理:                                                   |   |
|   |   * SQLite (app.db): 状态机、审计事件记录、快照、ETag、确定性 Batch ID    |   |
|   |   * 凭据金库 (secrets.json): AES-256-GCM 主密钥加密存储                   |   |
+-----------------------------------------------------------------------------------+
```

### 目录结构

```text
app/
  ai/           AI Provider 抽象、提取器 (extractor.py)、Prompt 与 JSON Schema
  calendar/     CalDAV 客户端 (caldav_client.py)、重复规则计算 (recurrence.py)
  channels/     统一消息处理器 (message_processor.py)、TG/微信路由、Bot 命令体系
  core/         系统配置、AES 加密 (crypto.py)、安全中间件、系统启动自检
  db/           SQLAlchemy 数据库模型 (models.py)、增量版本迁移、会话管理
  integrations/ Discord REST/Gateway 适配器、微信 iLink 协议客户端
  services/     业务服务层 (AI、CalDAV、WeChat、Telegram、Discord、Settings)
  web/          FastAPI 控制台路由 (routes.py)、安全认证、连接检测状态机、模板与静态资源
data/
  app.db        SQLite 业务数据库（自动增量迁移）
  secrets.json  系统加密主密钥文件
```

### 本地开发与测试

```bash
# 1. 克隆代码仓库
git clone https://github.com/j2st1n/ai-calendar-assistant.git
cd ai-calendar-assistant

# 2. 本地虚拟环境运行测试
source .venv/bin/activate
pytest

# 3. 本地 Docker 开发构建
docker compose -f docker-compose.dev.yml up -d --build
```
