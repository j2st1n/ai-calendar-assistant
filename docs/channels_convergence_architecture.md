# 多消息渠道（微信 / Telegram / Discord）页面收敛与架构演进方案

**版本**：v1.0.0  
**作者**：channel-architect (Engineer)  
**所属团队**：console-architecture-plan  
**关联任务**：Task t2（多消息渠道收敛架构与兼容性方案）  
**前置任务**：无  
**下游任务**：Task t3（综合输出控制台信息架构优化与实施路线图）

---

## 1. 背景与现状剖析

### 1.1 现状与痛点
在 `ai-calendar-assistant` 当前控制台结构中，消息接入渠道被分散在 3 个一级侧边栏菜单与独立模板中：
- `/console/wechat`（WeChat 设置，对应 `wechat.html`）
- `/console/telegram`（Telegram 设置，对应 `telegram.html`）
- `/console/discord`（Discord 设置，对应 `discord.html`）

这种布局带来了显著的问题：
1. **侧边栏结构臃肿分散**：当前导航菜单共包含 8 个主入口，其中“配置”分组下就挤占了 5 项（AI 设置、日历、Telegram、Discord、WeChat）。消息渠道独占了 3 个席位，导致左侧导航信息密度失衡。
2. **心智模型割裂**：对管理员而言，无论是微信、Telegram 还是 Discord，其本质都是系统接收日程消息与交互的“消息接入网关（Inbound Channels）”。用户往往希望在同一界面下一览各渠道的连接状态、启用情况与快捷运维操作，而不是在不同页面间频繁切换。
3. **异构机制混杂**：三大渠道在底层通信机制、鉴权生命周期、状态诊断和前端轮询上有巨大差异，现有分散页面导致状态指示器样式不一、定时器未做生命周期管理等技术债。

---

## 2. 三大消息渠道底层机制深度对比

为了在收敛为统一页面后保持各渠道业务逻辑的完整性与高稳定性，必须精确理清三大渠道在底层运行时的异构特性：

| 维度 | 微信 (WeChat / iLink) | Telegram | Discord |
| :--- | :--- | :--- | :--- |
| **底层协议 / 运行时** | HTTP 长轮询 (`WechatBotRuntime` + iLink API) | 异步长轮询 (`TelegramBotRuntime` + python-telegram-bot) | WebSocket 网关 (`DiscordBotRuntime` + discord.py) |
| **持久化凭据** | `wechat_bot_token` (AES 加密存储) | `telegram_bot_token` (AES 加密), `telegram_bot_username` | `discord_bot_token` (AES 加密), `discord_application_id` |
| **登录 / 鉴权模式** | 动态二维码扫码登录（生成 QR、轮询登录态、写回 Token） | 静态 Token 配置，支持热重载 | 静态 Token 配置，支持热重载 |
| **授权 / 白名单体系** | 单用户会话（扫码绑定的账号） | 动态 Deep-Link 绑定 (`/bind`) + `TelegramIdentity` 表白名单 | 手动输入 User ID (`DiscordIdentity` 表) + OAuth2 邀请链接 |
| **状态机与诊断** | 丰富状态机：`stopped`, `starting`, `polling`, `retrying`, `paused`, `error`；包含连续失败次数、上次轮询/消息时间戳、下次重试/冷却截止时间戳 | 二元状态：`running` / `stopped`，记录 `last_error` 与被拦截的未授权用户列表 (`rejected_users`) | 二元状态：`running` / `stopped`，记录 `last_error` |
| **前端动态轮询行为** | 专有定时器：每 3 秒高频轮询 `/console/wechat/status` 局部更新 DOM 指标；扫码时轮询 `/console/wechat/qr/status` | 绑定流程触发时：每 3 秒轮询 `/console/telegram/bind/status`，限时 40 次 | 无前端定时轮询，纯静态表单与操作 |

### 关键架构约束：
- **前端定时器生命周期隔离**：在收敛为单页面时，微信的状态轮询和 Telegram 的绑定状态轮询**绝不能**在非激活 Tab 或后台无限制空转，否则会浪费服务器连接与资源。
- **状态快照聚合**：后端路由渲染时需同时装配三大渠道的配置摘要（`WechatService.config_summary`, `TelegramService.config_summary`, `DiscordService.config_summary`），或支持按 Tab 懒加载。

---

## 3. 页面收敛方案评估与交互架构设计

### 3.1 交互架构选型：Tab 分面 (Tabs) vs 纵向分块 (Vertical Stack)

| 评估维度 | 方案 A：Tab 分面模式 (推荐) | 方案 B：纵向分块瀑布流 |
| :--- | :--- | :--- |
| **页面垂直高度** | 极佳（视口高度保持在 600~900px 内，无需大量滚动） | 极差（微信扫码大图 + 诊断折叠 + TG 绑定面板 + Discord 列表纵向堆叠，高度将突破 3000px） |
| **视觉与注意力聚焦** | 单一时间聚焦一个渠道的操作与配置，表单边界清晰 | 多渠道的保存按钮、状态圆点、错误横幅交织，易造成误操作 |
| **前端脚本与轮询管理** | 优秀：切换 Tab 时精准挂起/恢复对应的轮询定时器 | 困难：多个定时器并发轮询，且隐藏折叠区难以界定启停时机 |
| **URL 深度链接能力** | 优秀：通过 `?tab=wechat|telegram|discord` 支持精准直达与历史前进/后退 | 较弱：只能通过锚点跳转，仍伴随页面剧烈跳动 |
| **结论** | **采纳方案 A 作为核心交互架构** | **否决** |

### 3.2 统一页面结构规范 (`/console/channels`)

统一页面采用“**顶部全局状态药丸 (Status Pills) + 下方 Tab 面板 (Tab Panels)**”的标准控制台架构：

```
+-------------------------------------------------------------------------------+
| 消息渠道设置                                                                  |
| 管理微信、Telegram 与 Discord 消息接入与授权用户                                 |
+-------------------------------------------------------------------------------+
| [微信: 轮询中 (绿点)]  [Telegram: 运行中·2人 (绿点)]  [Discord: 未启动 (灰点)]    |  <- 顶部状态药丸/快速切换
+-------------------------------------------------------------------------------+
| [ 微信 (WeChat) ]   [ Telegram ]   [ Discord ]                               |  <- 主 Tab 导航
+-------------------------------------------------------------------------------+
| (激活的 Tab 内容区域 - 例如微信面板)                                            |
|                                                                               |
|  1. Bot 运行状态卡片 (状态徽章、运行/停止按钮、详细时间戳、折叠的诊断日志)            |
|  2. 扫码登录 / 重新验证卡片 (二维码获取与展示、扫码轮询反馈)                       |
|  3. 凭据管理 (折叠的安全清除区域)                                                |
+-------------------------------------------------------------------------------+
```

#### A. 顶部状态概览栏 (Global Channels Header)
- 渲染 3 个横向并列的小型状态药丸：
  - **微信**：在线状态指示灯、状态文案（“轮询中” / “离线” / “冷却中”）、快捷切换。
  - **Telegram**：在线指示灯、授权人数统计（如“运行中 · 2人授权”）。
  - **Discord**：在线指示灯、Bot 状态与授权人数。
- 点击任意药丸，平滑激活下方对应的 Tab，并更新浏览器 URL 查询参数或 Hash。

#### B. Tab 面板与按需生命周期
- **Tab 路由绑定**：支持 `/console/channels?tab=wechat`、`/console/channels?tab=telegram`、`/console/channels?tab=discord`。未传参数时，默认激活首个已配置或正在运行的渠道。
- **前端生命周期隔离控制器 (`ChannelTabController`)**：
  - 当 `wechat` Tab 处于激活状态时：启动每 3 秒的 `/console/wechat/status` 状态刷新轮询；
  - 当离开 `wechat` Tab 时：调用 `clearInterval` 彻底停止状态轮询，避免静默开销；
  - 当切入 `telegram` Tab 且存在活跃的绑定 token 时：启动绑定状态监听；切出时暂停。

---

## 4. 路由无损兼容与平滑重定向方案

为了保障旧书签、自动化脚本、首页仪表盘链接、向导流程以及已有表单 POST 提交 100% 稳定无损，制定完整的重定向与兼容矩阵：

### 4.1 GET 请求向后兼容矩阵

| 原 GET 路径 | 处理方式 | 目标新路径 | HTTP 状态码 | 客户端行为 |
| :--- | :--- | :--- | :--- | :--- |
| `/console/wechat` | 兼容重定向 | `/console/channels?tab=wechat` | 307 Temporary Redirect | 无缝直达微信 Tab |
| `/console/telegram` | 兼容重定向 | `/console/channels?tab=telegram` | 307 Temporary Redirect | 无缝直达 Telegram Tab |
| `/console/discord` | 兼容重定向 | `/console/channels?tab=discord` | 307 Temporary Redirect | 无缝直达 Discord Tab |
| `/console/channels` | 核心聚合视图 | 默认根据配置状态激活 Tab | 200 OK | 展示聚合主页 |

*注：采用 307 Temporary Redirect（或 302）可确保传递 query parameters（例如消息提示 flash message `?message=...` 或绑定参数 `?bind_link=...`）不丢失。*

### 4.2 POST 动作路由兼容策略

现有的 POST 操作完全保留其原有路由，仅微调内部操作完成后的回跳地址，确保用户提交表单后精准留在当前 Tab，避免跳回首页或默认 Tab：

```python
# Telegram 设置保存
@router.post("/telegram")
async def update_telegram_settings(...):
    ...
    # 原逻辑: return redirect("/console/telegram")
    # 新逻辑:
    target = redirect_path or "/console/channels?tab=telegram"
    set_flash(request, "Telegram Bot 已保存并重载。")
    return redirect(target)

# Telegram 生成绑定链接
@router.post("/telegram/bind")
async def generate_bind_link(...):
    ...
    return redirect_with_query("/console/channels?tab=telegram", bind_link=link, bind_token=token)

# Discord 设置保存
@router.post("/discord")
async def update_discord_settings(...):
    ...
    return redirect("/console/channels?tab=discord")

# 微信 启动/停止/清除 Token
@router.post("/console/wechat/start")
...
    return redirect("/console/channels?tab=wechat")
```

### 4.3 异步 API 端点保持 100% 原样不变
以下前端交互 API 保持现有路径和返回格式完全一致，零迁移成本：
- `GET /console/wechat/status`
- `GET /console/wechat/qr`
- `GET /console/wechat/qr/status`
- `GET /console/telegram/bind/status`

---

## 5. 侧边栏与全站联动精简方案

### 5.1 侧边栏导航精简对比

**重构前（8 项，配置区分散）**：
```html
<!-- 配置 -->
<div class="sidebar-section-label">配置</div>
<a href="/console/ai">AI 设置</a>
<a href="/console/caldav">日历</a>
<a href="/console/telegram">Telegram</a>   <-- 冗余
<a href="/console/discord">Discord</a>     <-- 冗余
<a href="/console/wechat">WeChat</a>       <-- 冗余
```

**重构后（6 项，清晰紧凑）**：
```html
<!-- 配置 -->
<div class="sidebar-section-label">配置</div>
<a href="/console/ai">AI 设置</a>
<a href="/console/caldav">日历设置</a>
<a href="/console/channels" class="sidebar-item {% if request.url.path.startswith('/console/channels') or request.url.path in ['/console/wechat', '/console/telegram', '/console/discord'] %}active{% endif %}">
  <!-- 统一采用消息对话图标 -->
  <svg viewBox="0 0 16 16" fill="none"><path d="M2.5 3h11a1.5 1.5 0 011.5 1.5v6a1.5 1.5 0 01-1.5 1.5H5.5L2 14.5V4.5A1.5 1.5 0 012.5 3z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
  消息渠道
</a>
```

### 5.2 全站外链更新点清单
1. **首页仪表盘 (`dashboard.html`)**：
   - 将原底部渠道卡片链接：
     - `/console/telegram` -> `/console/channels?tab=telegram`
     - `/console/discord` -> `/console/channels?tab=discord`
     - `/console/wechat` -> `/console/channels?tab=wechat`
   - 与 `t1` 任务协同：首页“服务运行状态”卡片中，点击微信/TG/Discord 统一深链进入对应 Tab。
2. **新手引导向导 (`wizard.html`)**：
   - 引导流程第 3 步“任选聊天渠道”中的按钮更新为跳转 `/console/channels?tab=wechat` 等。
3. **事件记录页 (`events.html`)**：
   - 渠道筛选与来源展示无需调整，后端逻辑完全兼容。

---

## 6. 工程实施与改造代码清单

实施落地的文件改动规划如下：

1. **新建模板 `app/web/templates/channels.html`**：
   - 包含 Tab 头部导航、顶部状态条。
   - 子 Tab 既可直接集成，也可拆解为 `_channel_wechat.html`、`_channel_telegram.html`、`_channel_discord.html` 模块化片段以提升可读性。
   - 内置客户端 Tab 控制脚本与生命周期 Hook。
2. **修改 `app/web/routes.py`**：
   - 新增 `@router.get("/channels")` 聚合视图，调用 `_wechat_service()`, `_telegram_service()`, `_discord_service()` 的 `config_summary`，注入模板。
   - 原 `/wechat`, `/telegram`, `/discord` 的 GET 处理函数改造为 `RedirectResponse("/console/channels?tab=...", status_code=307)`。
   - 更新所有渠道 POST 动作中的跳转路径为 `/console/channels?tab=...`。
3. **修改 `app/web/templates/base.html`**：
   - 精简侧边栏，将三渠道合并为“消息渠道”单一入口，并更新高亮判定逻辑。
4. **自动化测试套件适配**：
   - 在 `tests/test_channels_routes.py` 中增加对 `/console/channels` 页面渲染、Tab 切换、旧路由 307 重定向的完整覆盖。
   - 现有 `test_wechat_routes.py` 将客户端请求重构后验证 307 重定向与 API 原生兼容性。

---

## 7. 交付结论与对 Task t3 的输入

本方案提供了多消息渠道从**底层机制、交互体验、向前向后兼容、全站联动到具体工程实施**的完整闭环方案：
1. 确立了**Tab 分面**作为最佳交互形态，成功规避了长页面滚动与前端定时器资源泄漏隐患；
2. 制定了无痛的 **307 重定向 + 保留原有 POST/API 路径** 策略，保障系统稳定性 100%；
3. 侧边栏菜单项从 8 项收敛为 6 项，为 `product-designer` 在 Task t3 中输出完整的控制台信息架构优化与实施路线图提供了坚实支撑。
