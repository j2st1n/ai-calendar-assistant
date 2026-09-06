# 首页仪表盘层级重构与日历设置全宽布局对齐方案

**版本**：v1.0.0  
**作者**：ux-architect (Researcher)  
**所属团队**：console-architecture-plan  
**关联任务**：Task t1（首页仪表盘层级重构与日历设置全宽布局对齐方案）  
**协作任务**：Task t2（多消息渠道收敛架构与兼容性方案）  
**下游任务**：Task t3（综合输出控制台信息架构优化与实施路线图）

---

## 1. 首页仪表盘层级重构与内容收敛方案

### 1.1 现状诊断与重复冗余根因剖析

在当前控制台首页（`app/web/templates/dashboard.html`）中，存在严重的结构重叠与信息割裂。经过对模板与后端路由（`app/web/routes.py`）的完整审计，发现以下关键缺陷：

#### A. 元素重合度 100% 的双重服务网格
首页在两个完全不同的卡片中重复渲染了 5 大服务的跳转卡片：
1. **上方卡片**（第 33–54 行，`dashboard-services-card`）：
   - 标题：“服务运行状态（5 大核心服务与消息渠道实时状态监控）”
   - 网格容器：`class="dashboard-connections dashboard-services-grid"`（5 列栅格）
   - 包含卡片：`AI 主模型`、`CalDAV 日历`、`微信`、`Telegram`、`Discord`。每张卡片展示动态状态徽标（如“已验证”、“在线”、“运行中”等）及摘要文本。
2. **下方卡片**（第 81–105 行，`connections-title` / 连接摘要）：
   - 标题：“连接摘要”
   - 在展示了 AI 与 CalDAV 验证按钮及日历历史后，其底部（第 98–104 行）**再次硬编码渲染了另一套 5 列 `dashboard-connections` 网格**：
     - `AI`：展示“配置已保存”与模型名称
     - `日历`：展示“配置已保存”与日历名
     - `Telegram`、`Discord`、`微信`：展示“运行中 / 未启动”并附带“管理渠道 →”链接。
- **业务痛点**：用户在同一个页面上下滑动时，会先后看到两次几乎一模一样的 5 列卡片矩阵，产生严重的视觉冗余和困惑。

#### B. 验证动作与状态指示的空间割裂
- **操作断层**：用户在上方“服务运行状态”看到 AI 主模型或 CalDAV 显示黄色“待验证”或“需复核”状态徽标，直觉期望就近执行验证。然而，触发实际连通性验证的按钮（`[验证已保存配置]`）却深埋在页面底部的“连接摘要”卡片中，相隔了“建议检查”和“最近处理活动”两大复杂板块。
- **状态联动缺失**：底部点击“验证已保存配置”后，通过 AJAX 仅更新了底部的 `#check-ai` 或 `#check-caldav` 文本，而页面上方的 `#service-status-ai` 状态圆点和徽标根本无法动态刷新，必须强制用户整页刷新才能同步。

#### C. 信息层级流向混乱
目前首页的自上而下纵向流向为：
1. 概览标题与今日关键指标（KPIs）
2. 初次配置向导（条件展示）
3. **服务运行状态（5 服务卡片）**
4. 建议检查（待办指引）
5. 最近处理活动（事件流水明细）
6. **连接摘要（重复 5 卡片 + AI/CalDAV 验证按钮 + 日历成功记录）**
7. 详细统计与本地日程（折叠面板）
8. 更新说明

**根因**：“系统基础设施运行态”与“配置连通性验证”属于同一心智模型域（系统健康度与就绪态），却被活动流水（业务域）生硬拆分为头尾两截，导致用户认知路径来回跳跃。

---

### 1.2 最优层级结构重构方案

#### 核心设计原则
1. **Single Source of Status（单一状态源）**：一个服务在首页只允许存在一个主卡片入口，彻底剔除下方冗余的 5 渠道重复卡片。
2. **Action in Proximity（动作就近闭环）**：将主动验证动作（AI 模型连通性、CalDAV 日历连通性）与校验时间戳直接下沉内嵌到统一的“服务运行状态”卡片中。
3. **Progressive Disclosure（渐进式信息呈现）**：
   - 第一层（主视野）：5 大服务健康概览卡片，高频感知在线状态；
   - 第二层（紧随其下）：基础设施即时健康测试托盘（AI & CalDAV 验证），提供显式验证按钮与最近有效写入证据；
   - 第三层（微型注记）：保留时区、有效期 24 小时、权限边界等轻量辅助说明。

#### 统一服务状态卡片结构设计 (Unified Service & Connection Health Card)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│  服务运行状态                                                                  点击进入管理 →    │
│  5 大核心服务与消息渠道实时状态监控                                                              │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐      │
│ │ AI 主模型     │ │ CalDAV 日历   │ │ 微信          │ │ Telegram      │ │ Discord       │      │
│ │ ● 已验证      │ │ ● 已验证      │ │ ● 在线        │ │ ● 运行中      │ │ ● 运行中      │      │
│ │ DeepSeek / V3 │ │ iCloud / 主日程│ │ 微信长轮询   │ │ @my_cal_bot   │ │ Gateway 运行中│      │
│ └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘      │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│  连通性即时验证与同步记录 (Verification & Health Actions)                                        │
│ ┌─────────────────────────────────────────────────┬────────────────────────────────────────────┐ │
│ │ AI 主模型配置验证                               │ CalDAV 日历连通性与写入验证                │ │
│ │ 状态: 已保存配置测试通过 · 10:42                │ 状态: 已验证 · 10:42                       │ │
│ │                                                 │ 最近日历操作成功: 10:45 (当前配置已生效)   │ │
│ │ [ 验证已保存配置 ]                              │ [ 验证已保存配置 ]                         │ │
│ └─────────────────────────────────────────────────┴────────────────────────────────────────────┘ │
│                                                                                                  │
│ ℹ 验证时间按 Asia/Shanghai 划分；结果有效期 24 小时。主模型验证不覆盖识图，日历连接验证不覆盖写入权限。  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 前端异步状态联动机制 (DOM Live Sync)
为解决“验证后上方徽标不更新”的问题，重构内嵌的 JavaScript 验证逻辑：
1. 用户点击托盘内 `[data-check-kind="ai"]` 按钮；
2. 按钮禁用并显示 `正在验证...`，`#check-ai` 呈现加载态；
3. `POST /console/connections/ai/test` 响应后：
   - 更新 `#check-ai` 的文案及时间；
   - **联动更新**上方 `#service-status-ai` 元素内的 `.status-badge`：
     - 若验证成功：移除 `dot-warning` / `dot-error`，赋予 `dot-success`，徽标文字更新为“已验证”；
     - 若验证失败：移除 `dot-success`，赋予 `dot-error`，徽标文字更新为“测试失败”；
4. 同理，CalDAV 验证完成后联动更新 `#service-status-caldav`。实现操作与状态感知的实时闭环。

#### 页面整体信息流重组
重构后的首页信息流清晰聚焦：
```
1. [概览标题] + [今日关键指标 KPIs]
2. [初次配置引导向导] (未完成配置时醒目引导)
3. [统一服务运行状态卡片] (包含 5 渠道状态网格 + 即时验证托盘 + 日历写入成功历史)
4. [建议检查 Attention] (聚焦待办事项与今日失败恢复指引)
5. [最近处理活动 Events] (日程流水明细与详情展开)
6. [详细统计与本地日程] (折叠统计数据)
7. [更新说明] (CHANGELOG)
```
- **消除结果**：彻底删除旧版第 81–105 行独立的 `<section class="card" aria-labelledby="connections-title">`，页面纵向总长度减少约 35%，信息密度与逻辑连贯性显著提升。

---

### 1.3 现有自动化测试兼容性保障

为了确保改动后既有自动化测试 100% 稳定通过，对相关测试断言进行了全面审查：
1. **`tests/test_services_status_card.py`**：
   - 断言存在 `dashboard-services-card`；
   - 断言包含文本 `"服务运行状态"` 与 `"5 大核心服务与消息渠道实时状态监控"`；
   - 断言包含各服务元素 `id="service-status-ai"`, `id="service-status-caldav"`, `id="service-status-wechat"`, `id="service-status-telegram"`, `id="service-status-discord"`；
   - 断言各服务跳转链接 `href="/console/{sid}"`；
   - 断言包含 `dot-muted`。
   - **兼容结论**：重构方案完全保留上述容器类名、文字标题、DOM ID 与属性契约。
2. **`tests/test_config_version.py`**：
   - 断言包含 `"当前配置暂无日历操作成功记录"`、`"历史旧配置最近成功"`、`"配置已变更，历史记录不代表当前配置有效"`、`"当前配置已生效"`。
   - **兼容结论**：原“连接摘要”中的日历成功历史判断逻辑平移至统一卡片下方的验证托盘中，文案与逻辑 100% 保持一致。
3. **`tests/test_dashboard_stats.py`**：
   - 验证无记录时的空状态、时区显示与 KPI 计算。
   - **兼容结论**：完全无冲突。

---

## 2. 日历设置全宽布局对齐方案与排查分析

### 2.1 排查与根因定位

#### A. 核心问题定位
- **问题文件与行数**：
  - `apps/ai-calendar-assistant/app/web/static/styles.css` 第 799 行：
    ```css
    .caldav-form { max-width: 860px; }
    ```
  - `apps/ai-calendar-assistant/app/web/templates/caldav.html` 第 7 行：
    ```html
    <form method="post" action="/console/caldav" id="caldav-form" class="caldav-form">
    ```
- **结构机制**：在 `caldav.html` 中，整个表单 `<form class="caldav-form">` 包裹了 3 个主要的卡片区域：
  - `<section class="card stack"> 1. 连接日历服务 </section>`
  - `<section class="card stack"> 2. 选择目标日历 </section>`
  - `<section class="card stack"> 3. 默认日程规则 </section>`
  由于 `.caldav-form` 被强制赋予 `max-width: 860px`，导致其中的所有 `card` 宽度被硬件级锁死在 860px。

#### B. 全站表单与卡片宽度全景审计对比
我们对控制台所有核心页面的容器与宽度限制进行了全面排查：

| 页面 | 模板文件 | 容器结构与选择器 | 宽度限制规则 | 视觉呈现与对齐表现 |
| :--- | :--- | :--- | :--- | :--- |
| **控制台概览** | `dashboard.html` | `<main class="main">` > `<section class="card">` | **无限制 (100% 铺满)** | 卡片全宽对齐，呼吸感统一 |
| **AI 设置** | `ai.html` | `<main>` > `<div class="card">` > `<form class="stack">` | **无限制 (100% 铺满)** | 卡片全宽铺满，内部控件自适应 |
| **Telegram** | `telegram.html` | `<main>` > `<div class="card">` > `<form class="stack">` | **无限制 (100% 铺满)** | 卡片全宽铺满 |
| **Discord** | `discord.html` | `<main>` > `<div class="card">` > `<form class="stack">` | **无限制 (100% 铺满)** | 卡片全宽铺满 |
| **WeChat** | `wechat.html` | `<main>` > `<div class="card">` | **无限制 (100% 铺满)** | 卡片全宽铺满 |
| **事件记录** | `events.html` | `<main>` > `<form class="event-filters">` + `.event-list` | **无限制 (100% 铺满)** | 筛选栏与列表全宽铺满 |
| **系统设置** | `system.html` | `<main>` > `.system-settings` > `.card` > `.stack` | 卡片 100%，仅内部 `.system-settings .stack { max-width: 680px; }` | 卡片外框全宽对齐，内部字段收敛 |
| **日历设置** | `caldav.html` | `<main>` > `<form class="caldav-form">` > `.card` | **外部强制 `.caldav-form { max-width: 860px; }`** | **卡片外边框在 860px 处截断，右侧留白严重错位** |

#### C. 用户体验破坏点分析
1. **视觉节奏割裂（Jumping Layout）**：
   用户在左侧侧边栏切换时：
   `概览`（全宽）→ `AI 设置`（全宽）→ **`日历`（突然缩窄到 860px，右侧空旷）** → `Telegram`（全宽）→ `系统设置`（卡片全宽）。
   日历页面成为全站唯一的卡片尺寸畸形页面，带来强烈的粗糙感和未完工感。
2. **三列网格空间被压缩**：
   在卡片 3“默认日程规则”中，`.caldav-defaults` 定义为 `grid-template-columns: 2fr 1fr 1fr;`。在 860px 且扣除 padding 后，默认时区的输入控件过窄，导致类似 `America/Argentina/Buenos_Aires` 或 `Australia/Lord_Howe` 等长 IANA 时区名称显示不全；而在全宽下则具有完美的阅读与操作体验。

---

### 2.2 全宽响应式对齐实施方案

#### 1. CSS 宽度限制解除
在 `app/web/static/styles.css` 中，将 `.caldav-form` 的最大宽度解除，使其继承与全站所有页面一致的 100% 弹性全宽：
```css
/* 移除 max-width: 860px，改用全宽响应式规范 */
.caldav-form {
  width: 100%;
  max-width: none;
}
.caldav-form fieldset {
  border: 0;
  min-width: 0;
  padding: 0;
  margin: 0;
}
```

#### 2. 表单字段响应式排布优化 (Form Internal Layout)
解除外层卡片宽度限制后，为了防止在超宽屏幕（如 2K/4K 屏幕）下单个输入框被无意义横向拉伸过长，必须对日历表单的 3 个核心区块进行内部响应式布局精细化调整：

##### 卡片 1：连接日历服务
- **服务商预设** (`caldav-provider-select`)：保持清晰单行；
- **服务器地址** (`caldav_url`)：长 URL 保持独立单行；
- **凭据组响应式 2 列栅格**：将“用户名”(`caldav_username`) 与“密码 / 应用密码”(`caldav_password`) 组织为自适应 2 列栅格 `.form-grid-2col`：
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
- **SSL 复选框与提示文案**：保持自适应靠左排布；
- **动作按钮组**：`[测试连接] [测试写入权限] [拉取日历列表]` 在 `.row` 内自然排列，移动端自适应折行。

##### 卡片 2：选择目标日历
- **日历下拉框** (`#calendar-picker`)：全宽容器支持长日历名称完整显示；
- **手动配置抽屉** (`<details class="event-technical">`)：展开后的日历名称与地址同样自适应铺满。

##### 卡片 3：默认日程规则
- **`.caldav-defaults` 响应式栅格**：
  - 维持 `display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 16px;`；
  - 在全宽布局下，`2fr` 的时区选择框宽度从原有的 ~380px 扩展到 600px+，完美解决长时区截断痛点；
  - 移动端（`<=768px`）保持现有媒体查询折叠为单列：
    ```css
    @media (max-width: 768px) {
      .caldav-defaults {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    ```

##### 吸顶状态条对齐 (`.caldav-form .form-status`)
- 原有的 `position: sticky; top: 0; z-index: 2;` 保持不变，但宽度将自动与上方卡片完全对齐，不再产生悬浮截断问题。

---

### 2.3 控制台全站表单设计系统规范 (Console Design System Standards)

为防止未来新模块再次出现宽度割裂问题，沉淀如下设计规范：
1. **页面与卡片容器（Container Standard）**：
   - 所有一级控制台视图的根卡片容器必须由 `<main class="main">` 直接约束，宽度统一为 100%；
   - **严禁**在表单或卡片外层包裹设定具体像素值 `max-width`（例如 860px 或 680px）的限制容器。
2. **表单内字段自适应（Form Field Standard）**：
   - 简单表单字段默认 100% 填充所属卡片；
   - 具有逻辑关联的成对字段（如用户名与密码、协议与端口、日期起止）优先采用 `.form-grid-2col` 或 `.row`，并严格配置 `<=768px` 的单列折行断点；
   - 密集型参数配置（如时区/时长/提醒）采用比例栅格（如 `2fr 1fr 1fr`），确保首要文本字段占据绝对视觉比重。

---

## 3. 跨任务协同与上下游接口定义

### 3.1 与 Task t2（多消息渠道收敛）的协同与衔接
- **统一服务状态卡片兼容性**：
  - 当 Task t2 将微信、Telegram、Discord 整合为统一的 `/console/channels` 页面后：
  - 方案设计保证向下完全兼容：首页服务卡片可保持 5 个微型卡片网格，但链接无缝升级为深度锚点直达：
    - `微信` 卡片链接更新为：`/console/channels?tab=wechat`
    - `Telegram` 卡片链接更新为：`/console/channels?tab=telegram`
    - `Discord` 卡片链接更新为：`/console/channels?tab=discord`
  - 即使前端暂未升级，依赖 `channel-architect` 在 Task t2 中制定的 307 临时重定向矩阵，旧链接也能 100% 安全跳转到对应的 Tab，实现零时差解耦演进。

### 3.2 对 Task t3（综合架构规划与路线图）的交付清单
本任务向 `product-designer` (Task t3) 交付完整的实施改造清单：
1. **模板重构点**：
   - 修改 `app/web/templates/dashboard.html`：删除旧 `connections` 区块，将连通性验证托盘并入 `dashboard-services-card`；
   - 修改 `app/web/templates/caldav.html`：优化内部栅格结构，引入 `.form-grid-2col`。
2. **样式重构点**：
   - 修改 `app/web/static/styles.css`：删除 `.caldav-form` 的 `max-width: 860px`，添加 `.form-grid-2col` 及响应式样式。
3. **前端脚本重构点**：
   - 扩展 `dashboard.html` 内的异步验证脚本，加入状态徽标 live update 机制。
4. **自动化测试回归**：
   - 运行 `pytest tests/test_services_status_card.py tests/test_config_version.py tests/test_dashboard_stats.py` 进行 100% 回归验证。
