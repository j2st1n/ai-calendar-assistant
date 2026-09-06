# 事件记录优化验收

final result: passed

范围：现有事件记录页的响应式修复与中文失败反馈。不是整站重设计或像素级复刻。

## 视觉证据

- 变更前：`/Volumes/ext1/projects/artifacts/ai-calendar-audit-2026-09-05/10-events-mobile.png`
- 变更后：`/Volumes/ext1/projects/artifacts/ai-calendar-ui-2026-09-05/mobile.png`（390×844）
- 桌面：`/Volumes/ext1/projects/artifacts/ai-calendar-ui-2026-09-05/desktop.png`（1440×1000）

同一比较输入已打开变更前与变更后手机截图。主题、页面、视口与收起状态一致，数据有意使用演示数据，未进行逐字或像素精度比较。原先标题/结果不可见的问题已通过标题优先、允许换行的卡片解决；界面使用既有字体、背景、紫色操作色、8px 圆角及品牌资产，没有新增图片。桌面详情中的错误摘要、下一步和原始输入清晰分层；技术字段默认折叠。

## 交互与正确性

- 浏览器验证长标题换行、Enter 收起详情、中文原生状态选择、失败筛选、无匹配状态与清除筛选。
- 308 项 pytest 通过；覆盖历史 AI 错误、一般 429 与明确额度限制区分、CalDAV 429 不误判为 AI、缺字段/未知数据、损坏 JSON、内容转义和组合筛选。
- git diff --check 通过。

## 限制

预览只提供事件页和演示数据，不连接 Bot 或真实日历；设置链接保持实际产品路由，预览目标页仅说明环境。未部署。保留最近 100 条限制，未增加分页；完整读屏、全站视觉与聊天日程端到端验证不在本次范围内。

## 第二轮：CalDAV 与概览语义

final result: passed

- 现有日历页按连接、目标、默认规则组织；原生选择器按 URL 区分同名日历，保留高级手动配置。
- 首次检查发现反馈位于长表单底部；已改为表单顶部吸附状态栏，并重新截图验证。手机默认规则单列，SSL 复选框和标签同行。
- 浏览器验证：提醒改成 20 后，测试/拉取仍保留草稿；选择另一日历并保存、刷新后仍是该日历且提醒为 20。服务为演示替身，不访问真实 CalDAV。
- 概览零记录显示“— / 暂无已完成记录”；补配置状态、本地日程统计与时区说明。
- 自动验证：313 项 pytest 通过，覆盖探测无持久化、失败不返回凭据、换账号不复用密码、页内保存与校验、时区日界和待处理记录分母。
- 截图：产物目录下 `caldav-mobile.png`、`caldav-mobile-rules.png`、`dashboard-status.png`；均为本次本地界面。
- 既有深色主题/品牌、字号体系、圆角保持一致；新卡片与单列是针对审查问题的有意调整。原截图含真实配置，当前截图用演示数据，不作像素/文本等同声明。
- 范围限制：只改本地源码，未提交、未发布；完整概览重排、实时连接验证记录和其他功能见 TODO.md。

## 第三轮：概览层级与跨页时间（2026-09-06）

final result: passed

- 缺失配置和今日失败记录前置，明确历史失败可能已经重试成功；可选渠道未启动不标成故障。
- 最近处理活动限定最新 5 条，包含失败和删除，同时间按 ID 稳定排序；详情复用中文原因和建议。连接摘要链接到对应管理页，统计保留并默认折叠。
- 复核修正展开后的提示、未配置 AI 的摘要，以及事件页原先直接显示 UTC 导致的 8 小时时差。两页统一配置时区并标注。
- 316 项 pytest 通过，覆盖排序、失败/删除不丢失、空状态、错误时区回退、UTC 无时区/带时区时间转换。git diff --check 通过。
- 浏览器验证手机 390×844 卡片换行、键盘收起、统计展开、失败入口；桌面 1440×1000 检查通过。最终时区修复在浏览器跨页核对为同一时间。
- 桌面最终截图：`/Users/bin/.codex/visualizations/2026/09/05/01a06f28-64ae-7441-b311-a4ecff053801/dashboard-desktop.png`。手机布局截图：`/Volumes/ext1/projects/artifacts/ai-calendar-ui-2026-09-05/dashboard-mobile.png`、`dashboard-mobile-stats.png`，拍摄于日期格式统一之前。
- 保持既有深色主题；采用演示数据，未做真实远端日历操作、完整读屏或线上验收，未部署。

## 第四轮：记录分页、微信恢复、系统设置（2026-09-06）

final result: passed

- 记录每页 25 条，渠道/日期与状态、关键词组合筛选；按配置时区解释日期，翻页保留条件，越界页归入有效页，日期错误显示提示。
- 微信按当前 stale_token 错误显示重新登录入口；网络错误不误导用户换凭据，在线默认折叠扫码、诊断与清除凭据。二维码过期/请求失败后重新显示获取入口，重新获取前清理旧计时器。
- 系统设置分为账户安全、日程偏好、数据备份，按钮具名；偏好保存不会触发记录清理，保留数量单独保存不改每周起始日。补备份范围、清理影响、恢复与检查说明。
- 319 项 pytest 与 diff 检查通过。新增验证包含 101 条匹配记录分页、组合条件、时区日界、设置保存隔离，以及微信失效/普通网络错误/在线三种模板分支。
- 浏览器验证渠道筛选第二页及返回第一页，手机筛选与设置说明展开，微信失效演示状态。截图位于 `/Users/bin/.codex/visualizations/2026/09/05/01a06f28-64ae-7441-b311-a4ecff053801/`：`wechat-recovery.png`、`system-mobile.png`、`events-filters-mobile.png`。
- 维持既有深色主题与组件样式；预览使用模拟微信和内存数据库。预览未接入通行密钥接口，显示加载失败属于演示环境限制；未操作凭据、安全设置、真实扫码、备份恢复或生产部署。二维码过期按钮修复经源码检查，未进行真实二维码完整周期验证。

## 第五轮：已保存配置验证记录（2026-09-06）

final result: passed

- 概览增加主模型和日历连接的显式验证按钮，记录结果与时间。原设置页草稿探测仍不落库；本次记录只写验证元数据，不保存原始错误或复制明文凭据。
- 配置版本变化使旧测试失效，超过 24 小时提示复核；验证期间修改配置时仍标为旧版本结果。日历最后成功时间来自本地历史操作，明确可能使用旧配置。
- 323 项 pytest 与 diff 检查通过。浏览器验证模拟日历成功、刷新保留结果、未配置主模型反馈及 390 像素单列布局。
- 截图：`/Users/bin/.codex/visualizations/2026/09/05/01a06f28-64ae-7441-b311-a4ecff053801/connection-checks-mobile.png`。
- 范围限制：未调用真实模型/日历；未验证写入权限、识图或日程结构；业务成功归属具体配置版本尚待实现。未部署。

## v1.18.0 发布验收（2026-09-06）

- 发布工作流：https://github.com/j2st1n/ai-calendar-assistant/actions/runs/34003173641 ，测试与镜像构建成功。本地 323 项测试通过。
- 部署：andnode `/home/codex/docker/ai-calendar`，固定 APP_VERSION=v1.18.0，容器 healthy、0 重启，启动完成，无 traceback。
- 验证关键源码/模板/CSS 哈希与发布源码一致；SQLite quick_check 正常；内部健康接口和静态资源 HTTP 200，未登录控制台跳转登录页。
- 使用生产现有配置执行服务端 AI 响应与 CalDAV 连接探测，均成功，不输出凭据，不创建日程；该探测未写概览验证元数据。
- 升级前备份：`/home/codex/docker/ai-calendar/backups/pre-v1.18.0-20260906`，数据库完整性检查通过，含数据库、密钥及部署配置。保留旧镜像 v1.17.2；回滚时将 APP_VERSION 改回 v1.17.2 并执行 docker compose up -d --no-deps app，本次无业务 schema 迁移。
- 线上浏览器验收未完成：自动审批以工作区额度不足拒绝访问，不绕过。既有本地交互验证不能替代生产交互验收；真实扫码、日历写入、备份恢复未执行。

## v1.18.0 线上浏览器补验收（2026-09-06）

额度恢复后，线上登录会话可用。主模型/日历验证按钮成功，345 条历史记录分页与失败筛选正常；390 像素失败详情可读。日历提醒草稿临时从 15 改为 16，测试成功且草稿保留，另页读取保存值仍为 15；未提交保存。微信在线且诊断/登录区域折叠，系统通行密钥加载与备份说明展开正常。未执行真实日程写入、重登或恢复备份。

## v1.18.1 发布验收（2026-09-06）

- 部署状态：andnode `/home/codex/docker/ai-calendar`，APP_VERSION=v1.18.1，容器运行状态 healthy，0 重启，无 traceback 异常。升级前已在 `/home/codex/docker/ai-calendar/backups/pre-v1.18.1-20260906/` 备份配置与数据库，SQLite 完整性校验通过。
- 核心文件一致性：容器内 VERSION(v1.18.1)、styles.css、base.html、dashboard.html SHA256 哈希与源码完全匹配，数据库 PRAGMA quick_check 与 integrity_check 均为 ok。
- 服务端只读探测：使用生产已有配置完成 AI 模型与 CalDAV 连接只读探测，均返回 SUCCESS。不输出明文凭据，不持久化验证元数据，不修改/创建真实日程。
  - AI Probe: provider=openai_compatible, model=gemini-3.8-flash-high, base_url=https://api.3313107.xyz/v1 -> SUCCESS
  - CalDAV Probe: url=https://caldav.icloud.com, username=justinforgg@gmail.com, ssl_verify=True -> SUCCESS
- Web 控制台首屏与样式验收：
  - `styles.css?v=7` 正常加载（HTTP 200），包含新增 `.dashboard-kpis` 4 列及响应式网格样式。
  - 首屏正确渲染今日 4 大 KPI 卡片（今日处理、今日创建、今日失败、今日成功率）以及日界提示（按 Asia/Shanghai 划分；成功率不计入待处理记录）。
  - 概览标题右侧重复的“查看事件记录”链接已按设计移除。
- 业务连通性：内部健康检查与公网 https://cal.3313107.xyz/health 均返回 `{"status":"ok","version":"v1.18.1"}`；未登录访问 /console 正确 303 重定向至登录页。
- 范围限制：仅做服务端连接只读探测与首屏指标/资源渲染验证；未执行真实日程写入、未重置凭据或恢复备份。

## Phase 1 优化全量回归与安全探针验收（2026-09-06）

final result: passed

- **配置版本指纹与 EventRecord 表结构增量迁移**：
  - `SettingsService` 实现了 AI 与 CalDAV 核心配置的独立 SHA-256 哈希计算与组合指纹，以及严格单调递增的配置版本号 `_config_version`。配置或敏感密钥/密码变更时版本自增，非核心字段变更不影响版本。
  - `EventRecord` 表完成增量迁移，补全 `config_version`、`ai_config_hash`、`caldav_config_hash` 字段及索引，旧表兼容无损升级。
  - `MessageProcessor` 业务成功记录精准绑定生效配置版本；`routes.py` 概览页基于当前版本隔离统计，彻底消除“旧配置成功误判为当前配置有效”的假阳性。
- **配置能力分层探针与日历高级预设**：
  - AI 服务新增 `probe_schema_compliance` 探针（`/console/ai/schema-test`），抽取测试日程并严格校验 JSON Schema 规范性、日程标题与时间有效性。
  - CalDAV 服务新增 `probe_write` 探针（`/console/caldav/write-test`），创建临时 `[PROBE-TEST]` 日程并在 `finally:` 中确保自动回滚删除，实现 0 数据污染的安全验证。探针路由统一配置 `require_admin` 安全鉴权。
  - 日历界面集成主流服务商预设（Apple iCloud、Nextcloud、网易 163、QQ 邮箱、Fastmail、自定义）与 IANA 可搜索时区列表，辅助快捷填写与防错提示。
- **登录页面通行密钥体验优化与层级梳理**：
  - 界面层级重构：通行密钥置顶为主按钮样式，密码登录降级为二级样式，首次部署管理员密码指引收敛折叠入 `<details>`。
  - WebAuthn 常见 DOM 异常友好捕获：系统捕获 `NotAllowedError`（用户取消/拒绝）、`TimeoutError`（超时）、`NotSupportedError`（设备不支持）、`SecurityError`（非安全环境）等并呈现中文引导，增加 API 预检与防重复点击保护。
- **自动化测试与回归验证**：
  - 全量运行 pytest 测试套件，342 项测试 100% 通过（新增 19 项针对配置指纹、版本单调递增、EventRecord 增量迁移、AI Schema 探针、CalDAV 写入权限探针与安全回滚、日历服务商预设与 IANA 时区、通行密钥登录体验与异常友好提示）。
  - `git diff --check` 0 警告通过。
  - 静态资源版本与哈希一致性核验完成（`styles.css?v=7`，版本 `v1.18.1`）。
- **范围限制**：
  - 覆盖本地单元与集成测试、模板与路由逻辑；不涉及生产环境实机部署与外部真实凭据测试。

## Phase 2 优化全量回归与流程完善验收（2026-09-06）

final result: passed

- **失败收件箱状态机、防重幂等锁与重试写入接口**：
  - `EventRecord` 表完成轻量增量迁移，新增 `failure_phase`（`extraction` / `validation` / `write`）与 `retry_count` 字段及索引，并针对历史失败数据完成精准阶段回填。
  - `MessageProcessor` 处理链路在各阶段精确标记失败阶段：AI 超时/无日程意图归为 `extraction`；字段缺失/不支持规则/引用未找到归为 `validation`；日历写入/修改/删除失败归为 `write`。写入失败时预先分配稳定 `caldav_uid`，为重试幂等奠定基础。
  - `POST /console/events/{id}/retry` 接口实施严格权限控制（`require_admin`），拦截非法阶段、非创建/修改操作及已成功记录；基于内存并发互斥锁 `_retry_mutex` 与锁集合 `_retry_locks` 拦截重复重试请求（409 Conflict）；采用原有 `caldav_uid` 幂等写入 CalDAV，成功后原子更新状态为 `success`、清空错误并记录当前配置版本及哈希。
- **初次配置引导向导与首条测试日程端到端验证**：
  - 新增 `/console/wizard` 引导路由与向导步进模板 `wizard.html`（AI 模型设置 → 日历服务连接 → 选择聊天渠道 → 首条测试日程验证，每步独立测试、随时可跳过），概览页提供显式入口卡片。
  - 新增 `POST /console/wizard/test-event` 接口，支持 JSON 与 Form 载荷，端到端调用 `MessageProcessor` 处理测试日程，生成 `source="wizard"` 记录并返回结构化反馈。
- **亮暗色多主题（跟随系统/浅色/深色）规范与交互优化**：
  - `styles.css` 完善全套亮色模式语义化 CSS 变量（背景、表面、卡片、文字、边框、交互控件），与 `:root` 深色变量成对规范定义；支持 `@media (prefers-color-scheme: light)` 媒体查询与 `html[data-theme="auto"]` 跟随系统；
  - `base.html` 在 `<head>` 中嵌入内联脚本以消除主题切换闪烁，顶部导航栏提供「跟随系统 / 浅色 / 深色」三态切换按钮；
  - 事件列表展示失败阶段徽章（提取失败 / 校验失败 / 日历写入失败），提供一键“复制原文”与“重试写入”交互，带前端防重禁用及即时操作反馈。
- **自动化测试与全量回归**：
  - 编写并执行全量测试套件（含 `tests/test_retry_backend.py`、`tests/test_wizard_and_theme.py` 及 `tests/test_p2_qa_regression.py`），共 364 项用例 100% 绿灯通过。
  - `git diff --check` 0 警告通过。
- **范围限制**：
  - 覆盖本地单元与集成测试、UI 变量规范与路由逻辑；不涉及生产环境实机部署与外部真实日历操作。

