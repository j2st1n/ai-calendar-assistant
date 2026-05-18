# AI Calendar Assistant

自部署的私人 AI 日程管理助手。通过 Telegram / Discord 对话自然语言，AI 自动提取、修改、删除日程并写入你的日历。

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/github/v/tag/j2st1n/ai-calendar-assistant)](https://github.com/j2st1n/ai-calendar-assistant/tags)

## 功能

- 📅 **自然语言提取** — Telegram / Discord 里说一句「明天下午 3 点和张三开会」，自动创建日程
- ✏️ **自然语言修改** — 回复日程消息即可修改时间、地点、提醒、描述等字段
- 🧭 **精准回复定位** — 按渠道和会话定位被回复的日程，避免改错最近一条
- 🤖 **自定义 AI 供应商** — 支持 OpenAI、DeepSeek、Anthropic、OpenRouter、Ollama 等任意 OpenAI 兼容接口
- 📆 **CalDAV 同步** — 已测试群晖和 iCloud，推荐使用 iCloud
- 🔐 **自部署、单用户** — 数据全在本地，不上传第三方
- 📸 **图片识别日程** — 发送照片自动识别文字后提取日程
- 🎛️ **Web 控制台** — 概览状态、配置 AI/日历/Telegram/Discord、查看事件记录
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

### 配置边界

- Web 控制台是系统配置入口。
- Telegram / Discord 只负责日程创建、查询、修改、删除和状态查看。

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

## 安全

- 日程记录和配置存储在本地 SQLite / 本地文件中
- 自然语言输入、图片内容和必要的日程上下文会发送给你配置的 AI 模型服务用于识别与修改
- 管理员密码使用 bcrypt 哈希存储
- AI API Key、Telegram Token、CalDAV 密码使用 `APP_SECRET_KEY` 加密存储
- Web 控制台默认只绑定 `127.0.0.1`，不暴露公网
- 公网部署建议配置 Nginx / Caddy 反代 + HTTPS
- 备份时需同时保存 `data/app.db` 和 `data/secrets.json`

## 升级

```bash
docker compose pull && docker compose up -d
```

数据在 `data/` 目录下持久化，升级不会丢失配置和记录。

## 目录结构

```text
app/
  ai/           AI Provider、日程提取 Schema、Prompt
  calendar/     CalDAV 客户端、重复规则
  channels/     消息处理、Bot 命令
  core/         配置、加密、安全、启动引导
  db/           SQLAlchemy 数据模型
  integrations/ Discord 适配器
  services/     业务服务（AI、CalDAV、Telegram、Discord、设置）
  web/          Web 控制台路由、模板、样式
docs/
  requirements.md
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
