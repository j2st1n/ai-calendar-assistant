# AI Calendar Assistant

自部署的私人 AI 日程管理助手。通过 Telegram / Discord / WeChat 对话自然语言，AI 自动提取、修改、删除日程并写入你的日历。

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/github/v/tag/j2st1n/ai-calendar-assistant)](https://github.com/j2st1n/ai-calendar-assistant/tags)

License: MIT. See [LICENSE](LICENSE).

## 功能

- 📅 **自然语言提取** — Telegram / Discord / WeChat 里说一句「明天下午 3 点和张三开会」，自动创建日程
- ✏️ **自然语言修改** — 回复或引用日程消息即可修改时间、地点、提醒、描述等字段
- 🧭 **精准回复定位** — 按渠道和会话定位被回复或引用的日程，避免改错最近一条
- 🤖 **自定义 AI 供应商** — 支持 OpenAI、DeepSeek、Anthropic、OpenRouter、Ollama 等任意 OpenAI 兼容接口
- 📆 **CalDAV 同步** — 已测试群晖和 iCloud，推荐使用 iCloud
- 🔐 **自部署、单用户** — 数据全在本地，不上传第三方
- 📸 **图片识别日程** — 发送照片自动识别文字后提取日程
- 🎛️ **Web 控制台** — 概览状态、配置 AI/日历/Telegram/Discord/WeChat、查看事件记录
- 🛡️ **公网登录防护** — 支持 Cloudflare Turnstile、TOTP 两步验证、恢复码和 WebAuthn 通行密钥
- 🐳 **零配置 Docker 部署** — 不强制 `.env`，首次启动自动生成管理员密码
- 🔄 **一键升级** — `docker compose pull && docker compose up -d`

## 快速开始

```bash
# 1. 下载 docker-compose.yml
curl -O https://raw.githubusercontent.com/j2st1n/ai-calendar-assistant/main/docker-compose.yml

# 2. 启动
docker compose up -d

# 3. 查看初始管理员密码
docker compose logs app

# 4. 访问控制台
# http://127.0.0.1:9527
```

首次启动日志会输出：

```
==================================================
AI Calendar Assistant initialized
Web UI: http://127.0.0.1:9527
Username: admin
Password: xxxx-xxxx-xxxx-xxxx
Please change this password in System Settings.
==================================================
```

## 配置

登录控制台后，依次配置：

### 1. AI 设置

选择供应商（OpenAI / DeepSeek / Anthropic 等），填写 Base URL 和 API Key，拉取模型列表选择模型，测试连接通过后保存。

- 支持所有 OpenAI-compatible 接口，可自定义 Base URL
- 支持设置独立的识图模型，用于图片识别

### 2. 日历设置

填写 CalDAV 服务器地址、用户名、密码，测试连接后拉取日历列表，选择目标日历保存。

| 服务 | 地址示例 | 状态 |
|---|---|---|
| iCloud | `https://caldav.icloud.com` | ✅ 已测试，推荐 |
| 群晖 | `https://nas.example.com:5001/caldav/` | ✅ 已测试 |

### 3. Telegram 设置

填写 Bot Token 和 Bot Username（从 @BotFather 获取），保存重载后生成绑定链接，在 Telegram 中打开即可授权使用。

### 4. Discord 设置

填写 Discord Bot Token，保存启动后在控制台手动授权用户。频道中需要 @Bot，私聊和 Thread 可直接对话。

### 5. WeChat 设置

进入 WeChat 页面点击「获取二维码」，使用微信扫码登录 ClawBot / iLink。登录成功后系统会自动保存 Bot Token，并启动后台运行时接收消息。

更换 WeChat Bot 时，先清除当前 Token，再重新获取二维码扫码即可。

### 配置边界

- Web 控制台是系统配置入口。
- Telegram / Discord / WeChat 只负责日程创建、查询、修改、删除和状态查看。

## 使用

### Telegram 对话

```
你：明天下午 3 点和张三开会，地点会议室 A
Bot：✅ 日程已安排好啦！
      📌 标题：和张三开会
      🕒 时间：2026-05-15 15:00 - 16:00
      📍 地点：会议室 A

你：改成 4 点
Bot：✅ 日程已更新！
      📌 标题：和张三开会
      🕒 时间：2026-05-15 16:00 - 17:00

你：删
Bot：🗑️ 已删除日程：和张三开会
```

### Discord 对话

在已授权的 Discord 私聊、Thread 或频道中发送自然语言即可创建日程；频道中默认需要 @Bot。回复 Bot 发出的日程消息可以继续修改或删除该日程。

### WeChat 对话

扫码登录后，在微信里直接发送自然语言即可创建日程。引用 Bot 发出的日程确认消息后发送「改成 4 点」「删除」等指令，可以修改或删除被引用的日程。

发送图片时，系统会识别图片文字并继续按日程消息处理。WeChat 加密图片会在本地下载和解密后，再发送给已配置的识图模型。

WeChat 后台运行时会自动轮询新消息；控制台可查看运行状态并手动启动或停止。如果 WeChat Bot 无响应，通常重新扫码登录即可恢复，无需重启 Docker。

### 机器人命令

| 命令 | 说明 |
|---|---|
| `/help` | 查看使用帮助 |
| `/list [天数]` | 查看未来日程，默认 7 天，最多 14 天 |
| `/latest` | 查看最近一条日程 |
| `/status` | 查看 AI、识图模型和日历配置状态 |

`/list` 会把每条日程单独发出，直接回复某条 `/list` 结果也可以修改或删除对应日程。Discord 频道中使用命令或自然语言消息时需要 @Bot；私聊和 Thread 可直接发送。

## 安全

- 日程记录和配置存储在本地 SQLite / 本地文件中
- 自然语言输入、图片内容和必要的日程上下文会发送给你配置的 AI 模型服务用于识别与修改
- 管理员密码使用 bcrypt 哈希存储
- AI API Key、Telegram Token、WeChat Token、CalDAV 密码、Turnstile Secret Key 和 TOTP 种子使用 `APP_SECRET_KEY` 加密存储
- Web 控制台默认只绑定 `127.0.0.1`，不暴露公网
- 公网模式提供可信 Host、同源写请求校验、安全 Cookie、登录限流、HSTS、CSP 和禁止后台缓存等保护
- 备份时需同时保存 `data/app.db` 和 `data/secrets.json`

### 公网域名与通行密钥

通行密钥必须绑定具体的 HTTPS Origin 和 RP ID。源码没有写死项目维护者的域名，每个自部署实例都可以使用自己的域名。

在 `docker-compose.yml` 同目录创建 `.env`：

```env
APP_VERSION=v1.15.5
PUBLIC_ORIGIN=https://calendar.example.com
WEBAUTHN_RP_ID=calendar.example.com
TRUSTED_HOSTS=calendar.example.com,127.0.0.1,localhost
SECURE_COOKIES=true
```

各项含义：

| 变量 | 示例 | 说明 |
|---|---|---|
| `PUBLIC_ORIGIN` | `https://calendar.example.com` | 浏览器访问控制台的完整 Origin，不要添加末尾 `/` |
| `WEBAUTHN_RP_ID` | `calendar.example.com` | 通行密钥 RP ID，只填写域名，不包含协议、端口或路径 |
| `TRUSTED_HOSTS` | `calendar.example.com,127.0.0.1,localhost` | 允许访问应用的 Host 列表 |
| `SECURE_COOKIES` | `true` | 只通过 HTTPS 发送会话 Cookie；公网部署必须启用 |

修改后启动或重建容器：

```bash
docker compose up -d
```

以 Caddy 为例，应用仍只监听本机端口：

```caddyfile
calendar.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:9527
}
```

确认 `https://calendar.example.com/console/login` 可以正常访问后，登录控制台并进入“系统设置”中的“登录安全”区域：

1. 可选：填写该域名对应的 Cloudflare Turnstile Site Key 和 Secret Key。
2. 启用 TOTP 两步验证，并立即离线保存只显示一次的恢复码。
3. 添加通行密钥；注册时需要再次输入当前管理员密码。

Turnstile Site Key 和域名是公开信息；Turnstile Secret Key、TOTP 种子、恢复码及
`data/secrets.json` 必须保密，不应提交到 Git。通行密钥私钥始终保留在用户设备，
服务器只保存公钥。

不要在已有通行密钥后随意修改 `PUBLIC_ORIGIN` 或 `WEBAUTHN_RP_ID`。更换域名后，
旧域名注册的通行密钥通常不能继续使用，需要在新域名重新注册。域名与安全 Cookie
属于启动级安全边界，因此通过部署环境变量管理，不允许在 Web 页面中修改。

## 升级

```bash
docker compose pull && docker compose up -d
```

数据在 `data/` 目录下持久化，升级不会丢失配置和记录。

生产环境可固定到版本标签，避免 `latest` 变化：

```bash
APP_VERSION=v1.15.5 docker compose pull app
APP_VERSION=v1.15.5 docker compose up -d --force-recreate app
```

回滚时把 `APP_VERSION` 改为上一个版本并重复以上两条命令。容器可通过
`docker inspect` 查看内置健康检查状态，HTTP 探针地址为 `/health`。

## 目录结构

```text
app/
  ai/           AI Provider、日程提取 Schema、Prompt
  calendar/     CalDAV 客户端、重复规则
  channels/     消息处理、Bot 命令
  core/         配置、加密、安全、启动引导
  db/           SQLAlchemy 数据模型
  integrations/ Discord / iLink 适配器
  services/     业务服务（AI、CalDAV、Telegram、Discord、WeChat、设置）
  web/          Web 控制台路由、模板、样式
data/
  app.db        SQLite 数据库（运行时生成）
  secrets.json  加密密钥（运行时生成）
```

## 开发

```bash
git clone https://github.com/j2st1n/ai-calendar-assistant.git
cd ai-calendar-assistant
docker compose -f docker-compose.dev.yml up -d --build
```
