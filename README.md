# AI Calendar Assistant

自部署的私人 AI 日程管理助手。第一版通过 Telegram 接收自然语言日程，使用可配置 AI Provider 提取结构化事件，并通过 CalDAV 写入指定日历。

## 当前状态

当前仓库已按新版需求重置为可扩展骨架，完整需求见 [`docs/requirements.md`](docs/requirements.md)。业务实现会基于该文档继续推进。

## MVP 范围

- Web Console 只做配置、状态和记录查看。
- Telegram 是第一版唯一交互渠道。
- CalDAV 是第一版唯一日历写入目标。
- AI Provider 支持常见 OpenAI-compatible 服务和自定义 Base URL。
- Docker 本地部署，默认只暴露到 `127.0.0.1`。

## 本地启动

```bash
docker compose up -d
docker compose logs app
```

访问：

```text
http://127.0.0.1:9527
```

## 目录结构

```text
app/
  ai/          AI Provider、结构化提取 Schema
  calendar/    CalDAV 与重复规则
  channels/    Telegram 与消息处理
  core/        配置、加密、安全
  db/          SQLAlchemy 数据模型
  services/    业务服务
  web/         Web Console 路由、模板、静态资源
docs/
  requirements.md
data/
  app.db       运行后生成，本地持久化
```

## 安全说明

- `.env` 和 `data/app.db` 都是敏感文件。
- `APP_SECRET_KEY` 用于加密 AI Key、Telegram Token、CalDAV 密码。
- Web 默认仅本机访问；公网访问请自行配置 Caddy、Nginx、Tailscale 或 Cloudflare Tunnel。

## 下一步实现顺序建议

1. 数据库初始化与设置服务。
2. Web 登录和 System Settings。
3. AI Settings：Provider、模型拉取、测试连接。
4. CalDAV Settings：测试连接、拉取日历、保存目标日历。
5. Telegram Settings：Token 热重载、绑定链接、白名单。
6. 消息处理：创建、修改、删除、补全、已开始确认。
7. Event Records 查询、过滤、搜索和清空。
