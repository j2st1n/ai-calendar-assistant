# 控制台信息架构优化与渠道收敛规划方案

**文档版本**：v1.0.0  
**主笔架构师**：product-designer (Reviewer)  
**联合设计**：ux-architect (Task t1), channel-architect (Task t2)  
**所属团队**：console-architecture-plan  
**目标系统**：AI Calendar Assistant (`apps/ai-calendar-assistant`)  
**交付范围**：首页仪表盘内容收敛、日历设置全宽布局对齐、多消息渠道页面收敛及分步落地路线图

---

## 目录

1. [执行摘要与设计愿景](#1-执行摘要与设计愿景)
2. [控制台整体信息架构 (IA) 演进图谱](#2-控制台整体信息架构-ia-演进图谱)
3. [核心子系统重构与视觉对比](#3-核心子系统重构与视觉对比)
   - 3.1 首页仪表盘：单一状态源与操作就近闭环
   - 3.2 日历设置：全宽响应式对齐与表单栅格优化
   - 3.3 消息渠道：Tab 分面收敛与前端生命周期隔离
4. [实施改造点技术清单 (Implementation Matrix)](#4-实施改造点技术清单-implementation-matrix)
5. [向后兼容保障与风险防御矩阵](#5-向后兼容保障与风险防御矩阵)
6. [分步落地路线图与里程碑 (Rollout Roadmap)](#6-分步落地路线图与里程碑-rollout-roadmap)
7. [设计系统与前端工程规范附录](#7-设计系统与前端工程规范附录)

---

## 1. 执行摘要与设计愿景

### 1.1 现状诊断与核心痛点
随着 `ai-calendar-assistant` 接入的消息渠道（微信、Telegram、Discord）及基础设施（AI 大模型、CalDAV 日历）不断扩展，原有控制台暴露出三项显著的信息架构缺陷：
1. **首页仪表盘信息高度冗余且断层**：
   - 顶部“服务运行状态”与底部“连接摘要”在 5 大服务入口上存在 **100% 结构性重复**；
   - 连通性测试按钮与上方状态指示相隔两个大卡片，形成严重的“状态展示与触发动作分离”，且测试后无法局部更新顶部徽标。
2. **日历设置页面布局割裂**：
   - 样式表强制注入 `.caldav-form { max-width: 860px; }`，导致日历设置成为全站唯一的非全宽页面；
   - 用户从全宽概览、全宽 AI 设置切换到日历时产生强烈的视觉跳动（Jumping Layout），且长时区文本在窄屏栅格下被截断。
3. **消息接入渠道分散且占用导航**：
   - 微信、Telegram、Discord 各自独立占用一级侧边栏（导航总数达 8 项，配置区占 5 项），割裂了“消息网关”的整体心智；
   - 各渠道底层机制（长轮询、WebSocket、扫码状态机）异构，独立页面缺少生命周期统一管理，存在多定时器隐蔽空转风险。

### 1.2 三大重构抓手与预期收益
- **抓手一：首页单一状态源（Single Source of Truth）**：
  融合状态监控网格与连通性即时验证，彻底移除冗余底部网格，页面纵向滚动高度缩减 **35%**，实现 AI 与 CalDAV 验证的局部 Live Sync。
- **抓手二：日历设置解除限制与 2 列响应式栅格**：
  解除 860px 宽度物理硬编码，统一为全站 100% 全宽卡片规范；凭据字段采用 `.form-grid-2col` 自适应栅格，长时区容器宽度提升 60%+，消灭横向挤压。
- **抓手三：多渠道收敛为“状态药丸 + Tab 分面”**：
  构建统一的 `/console/channels` 页面，侧边栏导航从 8 项精简至 **6 项**；结合 `ChannelTabController` 隔离轮询定时器，配套 **307 重定向无损兼容矩阵** 保障存量外链与 POST 接口 100% 兼容。

---

## 2. 控制台整体信息架构 (IA) 演进图谱

### 2.1 侧边栏与站点导航结构对比

| 分类分组 | 重构前导航结构 (8 项) | 重构后导航结构 (6 项，推荐) | 演进说明与收益 |
| :--- | :--- | :--- | :--- |
| **监控** | 概览 (`/console`) | 概览 (`/console`) | 保留，首页内容收敛 35%，信息密度提升 |
| **监控** | 事件记录 (`/console/events`) | 事件记录 (`/console/events`) | 保留，日程交互与处理明细主轴 |
| **配置** | AI 设置 (`/console/ai`) | AI 设置 (`/console/ai`) | 保留全宽卡片标准 |
| **配置** | 日历 (`/console/caldav`) | 日历设置 (`/console/caldav`) | 解除 860px 限制，与全站卡片完全齐平 |
| **配置** | Telegram (`/console/telegram`) | *合并移除* | 整合为统一入口，旧 URL 307 自动跳转 |
| **配置** | Discord (`/console/discord`) | *合并移除* | 整合为统一入口，旧 URL 307 自动跳转 |
| **配置** | WeChat (`/console/wechat`) | *合并移除* | 整合为统一入口，旧 URL 307 自动跳转 |
| **配置** | *无* | **消息渠道 (`/console/channels`)** | **新增聚合页**，内嵌 WeChat/TG/Discord 3 大 Tab |
| **系统** | 系统设置 (`/console/system`) | 系统设置 (`/console/system`) | 保留系统级参数与调试功能 |

### 2.2 页面跳转与路由映射总览图

```
用户/外链访问入口
   │
   ├─► /console (首页) ──────────────────────► 统一服务卡片 ──► 直达各配置页
   │                                              │ (Live Sync 局部验证)
   ├─► /console/caldav (日历设置) ────────────► 100% 全宽卡片 (自适应栅格)
   │
   ├─► /console/wechat (旧书签/链接) ──────┐
   ├─► /console/telegram (旧书签/链接) ────┼──► [307 Temporary Redirect]
   ├─► /console/discord (旧书签/链接) ─────┘              │
   │                                                     ▼
   └─► /console/channels (消息渠道聚合页) ◄──────────────┘
           ├── ?tab=wechat   ──► 微信扫码/长轮询面板 (高频轮询，切出即止)
           ├── ?tab=telegram ──► TG Bot凭据/DeepLink绑定面板
           └── ?tab=discord  ──► Discord Gateway/白名单面板
```

---

## 3. 核心子系统重构与视觉对比

### 3.1 首页仪表盘：单一状态源与操作就近闭环

#### A. 视觉结构前后对比

**重构前布局（冗长分散、双网格冗余）：**
```
┌──────────────────────────────────────────────────────────┐
│ 今日关键指标 (KPIs: 接收消息 / 创建日程 / 冲突拦截)      │
├──────────────────────────────────────────────────────────┤
│ 服务运行状态 (上方网格: 5大卡片 AI/日历/微信/TG/Discord) │
├──────────────────────────────────────────────────────────┤
│ 建议检查 (Attention 列表)                                │
├──────────────────────────────────────────────────────────┤
│ 最近处理活动 (Event 流水列表)                            │
├──────────────────────────────────────────────────────────┤
│ 连接摘要 (下方网格: 再次出现 5 卡片 + AI/日历验证按钮)   │ <--- 严重冗余与操作割裂！
├──────────────────────────────────────────────────────────┤
│ 详细统计与折叠区                                         │
└──────────────────────────────────────────────────────────┘
```

**重构后布局（结构收敛、动作内嵌、Live Sync）：**
```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 今日关键指标 (KPIs)                                                                              │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [统一服务运行状态卡片] (Unified Service & Health Master Card)                                    │
│ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐      │
│ │ AI 主模型     │ │ CalDAV 日历   │ │ 微信 (iLink)  │ │ Telegram      │ │ Discord       │      │
│ │ ● 已验证      │ │ ● 已验证      │ │ ● 在线        │ │ ● 运行中      │ │ ● 运行中      │      │
│ │ DeepSeek / V3 │ │ iCloud / 主日程│ │ 轮询正常      │ │ @my_cal_bot   │ │ Gateway 正常  │      │
│ └───────┬───────┘ └───────┬───────┘ └───────┬───────┘ └───────┬───────┘ └───────┬───────┘      │
│         │                 │                 │                 │                 │              │
│         ▼                 ▼                 ▼                 ▼                 ▼              │
│   (/console/ai)   (/console/caldav)  (channels?wechat) (channels?telegram) (channels?discord)     │
│ ├────────────────────────────────────────────────────────────────────────────────────────────────┤
│ │ 连通性即时验证与同步记录 (Live Verification Tray)                                              │
│ │ ┌───────────────────────────────────────────────┬────────────────────────────────────────────┐ │
│ │ │ AI 主模型连通性验证                           │ CalDAV 日历连通性与写入验证                │ │
│ │ │ 状态: 已保存配置测试通过 (10:42)               │ 状态: 已验证 (10:42)                       │ │
│ │ │                                               │ 最近成功记录: 10:45 (当前配置已生效)       │ │
│ │ │ [ ⚡ 立即验证配置 ]                           │ [ ⚡ 立即验证配置 ]                         │ │
│ │ └───────────────────────────────────────────────┴────────────────────────────────────────────┘ │
│ │ ℹ 提示：验证结果有效期 24 小时；点击验证后上方徽标将实时联动更新，无需整页刷新。                │
│ └────────────────────────────────────────────────────────────────────────────────────────────────┘
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 建议检查 (Attention) ── 聚焦待办与异常排查                                                       │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 最近处理活动 (Events) ── 日程流转流水明细                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 详细统计与折叠区 (Statistics)                                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### B. 首页异步状态联动 (DOM Live Sync 契约)
1. **触发行为**：点击 `[data-check-kind="ai"]` 或 `[data-check-kind="caldav"]`。
2. **加载反馈**：按钮进入 disabled 态，文本变为“正在验证...”，旁边显示微型 Spinner。
3. **响应联动**：
   - 托盘区域更新文本状态与最新测试时间戳；
   - 向上遍历定位 `#service-status-ai` 或 `#service-status-caldav`，依据接口 `success: true|false` 状态，动态将 `.status-badge` 与 `.status-dot` 切换为 `dot-success` 或 `dot-error`，文案同步替换为“已验证”或“测试失败”。

---

### 3.2 日历设置：全宽响应式对齐与表单栅格优化

#### A. 布局断裂排查对比

| 检查维度 | 重构前表现 (860px 锁死) | 重构后表现 (100% 全宽响应式) |
| :--- | :--- | :--- |
| **容器 CSS 规则** | `.caldav-form { max-width: 860px; }` | `.caldav-form { width: 100%; max-width: none; }` |
| **右侧留白一致性** | 右侧产生 400px~800px 不等的大面积非预期留白 | 外边框与顶部全局 Header、下方操作栏 100% 对齐 |
| **凭据字段排布** | 用户名、密码纵向单列单行堆叠，垂直空间浪费 | 采用 `.form-grid-2col` 双列排布，紧凑高效 |
| **时区与规则栅格** | `2fr 1fr 1fr` 在 860px 下时区仅分得 ~360px，长名称截断 | 全宽下时区字段获得 600px+ 呼吸空间，完整显示时区 |
| **吸顶状态栏** | `.form-status` 悬浮时在 860px 处截断悬空 | 全宽吸顶对齐，阴影与视觉边界完全自然 |

#### B. 表单内部栅格结构定义
- **凭据双列响应式容器**：
  ```css
  .form-grid-2col {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  @media (max-width: 768px) {
    .form-grid-2col {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  ```
- **时区与日程规则容器 (`.caldav-defaults`)**：
  - 桌面端：`grid-template-columns: 2fr 1fr 1fr; gap: 16px;`
  - 移动端 (`<=768px`)：自动降级为单列 `grid-template-columns: 1fr;`。

---

### 3.3 消息渠道：Tab 分面收敛与前端生命周期隔离

#### A. 页面架构规范与 Tab 分面交互

统一聚合页 `/console/channels` 采用“顶部全局状态药丸 + 主 Tab 分面”方案：
```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 消息渠道设置 (Channels Convergence)                                                              │
│ 统一管理微信、Telegram 与 Discord 消息接入网关、运行状态与白名单授权                              │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [ 微信: ● 轮询中 ]          [ Telegram: ● 运行中·2人 ]          [ Discord: ○ 未启动 ]            │
│  (快捷药丸，点击可直接切换下方 Tab)                                                               │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐                                          │
│ │  微信 (iLink) │ │   Telegram    │ │    Discord    │                                          │
│ └───────▲───────┴─┴───────────────┴─┴───────────────┴──────────────────────────────────────────┘ │
│         │ (当前激活 Tab 面板)                                                                    │
│ ┌───────┴──────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ 1. Bot 运行控制与状态诊断卡片 (包含状态徽章、运行耗时、启动/停止按钮、历史错误日志展开)       │ │
│ │ 2. 扫码登录与验证凭据卡片 (动态二维码生成、扫码倒计时、登录状态实时反馈)                     │ │
│ │ 3. 高级设置与凭据管理 (折叠区域，包含 Token 安全清除)                                       │ │
│ └──────────────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### B. 前端生命周期控制器 (`ChannelTabController`)
由于微信需要每 3 秒高频轮询 `/console/wechat/status` 和扫码结果，而 Telegram 仅在触发绑定后轮询 `/console/telegram/bind/status`，在单页面多 Tab 架构下必须严格实施**生命周期隔离**：

```javascript
class ChannelTabController {
  constructor() {
    this.activeTab = 'wechat';
    this.pollers = {
      wechatStatus: null,
      wechatQr: null,
      telegramBind: null
    };
  }

  switchTab(targetTab) {
    if (this.activeTab === targetTab) return;
    this.teardownCurrentTab(this.activeTab);
    this.activeTab = targetTab;
    this.mountTargetTab(targetTab);
    this.updateUrlHistory(targetTab);
  }

  teardownCurrentTab(tab) {
    if (tab === 'wechat') {
      // 停止微信高频轮询定时器，防止后台空耗网络与计算资源
      if (this.pollers.wechatStatus) clearInterval(this.pollers.wechatStatus);
      if (this.pollers.wechatQr) clearInterval(this.pollers.wechatQr);
    } else if (tab === 'telegram') {
      if (this.pollers.telegramBind) clearInterval(this.pollers.telegramBind);
    }
  }

  mountTargetTab(tab) {
    // 仅在切入当前激活 Tab 时才开启必要的局部轮询
    if (tab === 'wechat') {
      this.initWechatPoller();
    }
  }

  updateUrlHistory(tab) {
    const url = new URL(window.location);
    url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url);
  }
}
```

---

## 4. 实施改造点技术清单 (Implementation Matrix)

下表汇总全部涉及文件的改造属性、具体工作项与关联影响：

| 模块类别 | 目标文件路径 | 改造性质 | 具体技术改造要点 | 风险级别 |
| :--- | :--- | :--- | :--- | :--- |
| **视图模板** | `app/web/templates/dashboard.html` | 模板重构 | 1. 移除第 81–105 行独立的 `connections` 冗余卡片；<br>2. 在 `dashboard-services-card` 内嵌连通性验证托盘；<br>3. 5 大服务卡片更新为深链至 `/console/channels?tab=...`；<br>4. 增加测试结果异步回填联动逻辑。 | 中 |
| **视图模板** | `app/web/templates/caldav.html` | 模板优化 | 1. 为用户名与密码包裹 `<div class="form-grid-2col">`；<br>2. 优化时区下拉框容器；<br>3. 移除任何局部硬编码宽度。 | 低 |
| **视图模板** | `app/web/templates/channels.html` | **新建文件** | 1. 顶部全局状态药丸组件；<br>2. Tab 导航与 WeChat / Telegram / Discord 三大分面面板；<br>3. 内置 `ChannelTabController` 脚本。 | 中 |
| **视图模板** | `app/web/templates/base.html` | 导航精简 | 1. 侧边栏移除独立 Telegram、Discord、WeChat 链接；<br>2. 增加“消息渠道”(`href="/console/channels"`)统一项与消息图标；<br>3. 适配 `active` 高亮路由匹配规则。 | 低 |
| **样式表** | `app/web/static/styles.css` | 样式更新 | 1. 移除 `.caldav-form { max-width: 860px; }`，改为全宽；<br>2. 新增 `.form-grid-2col` 及响应式媒体查询断点；<br>3. 新增 Channels 聚合页药丸、Tab 头及面板切换样式。 | 低 |
| **后端路由** | `app/web/routes.py` | 路由与控制器 | 1. 新增 `@router.get("/channels")` 聚合视图并装配 3 渠道 `config_summary`；<br>2. 改造原 `/wechat`, `/telegram`, `/discord` GET 路由为 `307 RedirectResponse`；<br>3. 更新渠道 POST 动作提交后的重定向目标至 `channels?tab=...`。 | 中 |
| **测试套件** | `tests/test_services_status_card.py` | 测试验证 | 验证统一卡片保留原有 DOM ID (`#service-status-*`) 及文本断言。 | 低 |
| **测试套件** | `tests/test_config_version.py` | 测试验证 | 验证日历操作历史记录在验证托盘中正确渲染。 | 低 |
| **测试套件** | `tests/test_channels_routes.py` | **新建测试** | 1. 覆盖 `/console/channels` 页面渲染与 Tab 参数；<br>2. 验证旧路由 307 重定向及 Query 参数传递；<br>3. 验证 POST 操作后准确跳转。 | 中 |

---

## 5. 向后兼容保障与风险防御矩阵

### 5.1 路由重定向与状态码兼容矩阵

为保障用户既有浏览器书签、桌面快捷方式、第三方通知回调以及历史外链 100% 可用，制定严格的 HTTP 状态码与重定向规范：

| 请求方法 | 请求 URL | 目标重定向 URL | 状态码 | 兼容设计细节 |
| :--- | :--- | :--- | :--- | :--- |
| **GET** | `/console/wechat` | `/console/channels?tab=wechat` | **307 Temporary Redirect** | 保留原始 Query 参数（如 `?message=...`），无损传递提示信息 |
| **GET** | `/console/telegram` | `/console/channels?tab=telegram` | **307 Temporary Redirect** | 保留绑定链接及提示参数（如 `?bind_token=...`） |
| **GET** | `/console/discord` | `/console/channels?tab=discord` | **307 Temporary Redirect** | 保留 OAuth2 返回参数与提示信息 |
| **GET** | `/console/channels` | `/console/channels?tab=wechat` (默认) | **200 OK** | 若未传 `?tab`，默认依据“首个已配置/运行中渠道”智能激活 |
| **POST** | `/console/telegram` | `/console/channels?tab=telegram` | **303 See Other** | 表单保存成功后带 Flash Message 重定向回当前 Tab |
| **POST** | `/console/telegram/bind` | `/console/channels?tab=telegram` | **303 See Other** | 携带生成好的 `bind_link` 参数重定向回 Telegram Tab |
| **POST** | `/console/discord` | `/console/channels?tab=discord` | **303 See Other** | 保存后保持在 Discord Tab |
| **POST** | `/console/wechat/start` | `/console/channels?tab=wechat` | **303 See Other** | 启动 Bot 后保持在微信 Tab |

### 5.2 异步 API 与前端轮询 100% 契约不变性
以下端点保持路径、入参与返回 JSON 结构绝对不变：
- `GET /console/wechat/status`
- `GET /console/wechat/qr`
- `GET /console/wechat/qr/status`
- `GET /console/telegram/bind/status`
- `POST /console/connections/ai/test`
- `POST /console/connections/caldav/test`

### 5.3 自动化测试断言兼容保障
针对现有代码库的自动化测试进行定向兼容设计：
1. **`test_services_status_card.py` 契约**：
   - 必须保留 `.dashboard-services-card` 类名；
   - 必须保留 `"服务运行状态"` 标题和 `"5 大核心服务与消息渠道实时状态监控"` 副标题；
   - 必须保留 `#service-status-ai`、`#service-status-caldav`、`#service-status-wechat`、`#service-status-telegram`、`#service-status-discord` 五大 DOM ID。
2. **`test_config_version.py` 契约**：
   - 完整保留“当前配置暂无日历操作成功记录”、“历史旧配置最近成功”、“配置已变更，历史记录不代表当前配置有效”、“当前配置已生效”等状态文案。

---

## 6. 分步落地路线图与里程碑 (Rollout Roadmap)

整个落地重构划分为 4 个清晰解耦、步步可验证的阶段（Phases）：

```
[Phase 1: 样式基线] ──► [Phase 2: 渠道收敛] ──► [Phase 3: 仪表盘收敛] ──► [Phase 4: 全站验收]
 日历全宽 + 栅格库       Channels聚合页+307      统一状态卡片+LiveSync   全量测试+设计走查
 (风险极低/耗时0.5天)     (中等复杂度/耗时1.5天)   (高整合度/耗时1.0天)    (全量保障/耗时0.5天)
```

### Phase 1：日历设置全宽对齐与栅格基础库建设 (Day 1 上半天)
- **目标**：以极低风险解决全站唯一卡片缩窄的视觉体验痛点。
- **任务项**：
  1. 在 `styles.css` 中移除 `.caldav-form { max-width: 860px; }`，新增 `.form-grid-2col` 及移动端断点；
  2. 在 `caldav.html` 中优化凭据表单为 2 列栅格，扩展时区宽度自适应；
  3. 手工与多分辨率验证（移动端 375px、平板 768px、桌面 1440px、超宽 2560px）；
- **验收准则**：日历页面与 AI、概览页面卡片宽度 100% 对齐，时区文本无溢出截断。

### Phase 2：多消息渠道聚合与 307 重定向无损兼容 (Day 1 下半天 ~ Day 2)
- **目标**：构建 `/console/channels` 聚合页，收敛侧边栏导航，确立生命周期隔离。
- **任务项**：
  1. 创建 `app/web/templates/channels.html`，实现顶部状态药丸与三渠道 Tab 面板；
  2. 编写 `ChannelTabController`，封装微信高频轮询的启动与清理逻辑；
  3. 在 `routes.py` 中增加 `/channels` 路由，原有渠道 GET 路由配置 307 重定向；
  4. 修改 `base.html` 侧边栏，将 3 项渠道收敛为 1 项“消息渠道”；
  5. 编写自动化测试 `test_channels_routes.py`，回归测试微信扫码与 Telegram 绑定流程。
- **验收准则**：侧边栏减至 6 项；直接访问 `/console/wechat` 自动以 307 跳转至 `channels?tab=wechat` 且功能完全正常。

### Phase 3：首页仪表盘层级重构与异步 Live Sync 闭环 (Day 3)
- **目标**：消灭双网格冗余，实现操作就近闭环与局部状态联动。
- **任务项**：
  1. 重构 `dashboard.html`：删除旧 `connections` 卡片，将连通性测试托盘内嵌至 `dashboard-services-card` 下方；
  2. 更新 5 大服务卡片的超链接，微信/TG/Discord 指向新版深链；
  3. 改造内联 JS：连通性验证成功或失败时，动态同步更新上方服务卡片的徽章与状态圆点；
  4. 运行 `test_services_status_card.py` 与 `test_config_version.py` 自动化测试。
- **验收准则**：首页纵向高度减少 35%；点击“立即验证”后上方徽标即时刷新；所有既有测试 100% 通过。

### Phase 4：全系统集成走查、端到端测试与文档归档 (Day 4)
- **目标**：全流程端到端回归与知识沉淀。
- **任务项**：
  1. 执行 `pytest` 全量回归测试套件；
  2. 跨浏览器与暗色模式视觉走查；
  3. 更新用户向导 `wizard.html` 及系统外链；
  4. 形成发布说明并归档团队 Wiki。
- **验收准则**：测试覆盖率无回退，零功能性破坏，用户体验流畅连贯。

---

## 7. 设计系统与前端工程规范附录

### 7.1 控制台通用容器与卡片规范
1. **统一根容器**：所有控制台一级视图的根容器统一为 `<main class="main">`，卡片外层统一为 `<section class="card">` 或 `<div class="card">`。
2. **严禁硬编码卡片外宽**：禁止在页面任何外层容器上使用固定像素 `max-width`（如 `860px`、`680px`）；容器宽度始终由外层响应式网格与视口控制。
3. **表单内字段收敛**：表单如果字段过少需要收敛，必须通过内部栅格（如 `.form-grid-2col`）或在表单元素内部约束，卡片边框必须与全站整体基线齐平。

### 7.2 状态指示器与药丸组件规范

| 状态类别 | 样式类名 | 表现形式 | 应用场景 |
| :--- | :--- | :--- | :--- |
| **正常 / 运行中** | `.dot-success` / `.badge-success` | 绿色圆点 / 浅绿底深绿字徽章 | 服务在线、长轮询正常、Token 验证有效 |
| **警告 / 需复核** | `.dot-warning` / `.badge-warning` | 橙色圆点 / 浅橙底深橙字徽章 | 冷却中、连续重试达到阈值、配置已更改待生效 |
| **错误 / 故障** | `.dot-error` / `.badge-error` | 红色圆点 / 浅红底深红字徽章 | 验证失败、连接超时、Token 无效或被踢下线 |
| **离线 / 未启动** | `.dot-muted` / `.badge-muted` | 灰色圆点 / 浅灰底深灰字徽章 | 服务未启用、手动停止、未配置凭据 |

### 7.3 定时器生命周期与资源清理契约
- 所有前端轮询逻辑（无论是 `setInterval` 还是递归 `setTimeout`），必须挂载在具有明确生命周期（Mount/Unmount）的控制器中。
- 页面切换、Tab 切换、模态框关闭或页面离开（`beforeunload` / `visibilitychange` 且为 `hidden` 时）必须显式清除定时器，避免后台静默空转消耗内存与服务端连接池。

---

*（本文档由 AgentTeams "console-architecture-plan" 规划团队输出，已完整综合 Task t1、Task t2 成果，作为后续工程实施的标准设计基线）*
