# Changelog

## [1.1.0] - 2026-05-17


### Bug Fixes

- restore copy_global_to for Discord guild command sync — 恢复copy_global_to确保guild级命令同步

- dead code in _send_telegram_replies and clear_commands wiping local command tree — 修复Telegram命令静默失败和Discord斜杠命令消失



### Features

- show vision model in /status and calendar source on dashboard — /status显示识图模型，概览页日历显示来源



### Maintenance

- auto-generate CHANGELOG.md and version in build — CI自动生成CHANGELOG和版本号


## [1.0.0] - 2026-05-17

### Features
- feat: add Discord slash commands — 添加Discord斜杠命令
- feat: share channel command handlers — 共享渠道命令处理

### Bug Fixes
- fix: avoid duplicate Discord slash commands — 避免Discord斜杠命令重复
- fix: copy changelog in Docker build — Docker构建复制更新日志

### Refactoring
- refactor: move Discord adapter to integrations — 移动Discord适配层

### Documentation
- docs: refine readme scope and privacy notes — 调整README范围和隐私说明

## [1.0.0-beta.1] - 2026-05-17

### Features
- feat: quick-modify support X点 format — 快捷修改支持X点格式
- feat: Discord channel support — Discord渠道支持
- feat: rename /list to /upcoming with future days — 重命名命令为upcoming显示未来日程
- feat: /list grouped by date with day limit — 列表按日期分组显示并限制天数
- feat: host network mode for production compose — 正式环境用host网络访问本地API
- feat: host network mode for local API access — 开发环境用host网络访问本地API
- feat: vision status on dashboard and unconfigured prompt — 概览显示识图状态及未配置提示
- feat: show vision model status on dashboard — 概览页显示识图模型状态
- feat: auto-start Telegram bot on startup — 启动时自动运行 Telegram Bot
- feat: quick-modify date support and iCal reminders — 快捷修改支持日期和日历提醒
- feat: per-event separate replies for multi-event — 多条日程各自回复独立消息
- feat: multi-event extraction, simplified reply — 支持多条日程提取，精简回复格式
- feat: add typing indicator in tg — Telegram 回复时显示正在输入
- feat: add favicon and calendar icon
- feat: use png calendar icon in topbar
- feat: add favicon link
- feat: tg command redesign and auto-register
- feat: inline event detail rows, refresh button
- feat: bind auth card with polling, user delete
- feat: custom dark dropdowns for all selects, ai flash fix
- feat: custom dark dropdown for ai provider
- feat: toggle switch for vision model
- feat: vision model support for tg photos
- feat: ai settings ux with auto baseurl
- feat: backup download, move clear to events page
- feat: session flash messages
- feat: manual release workflow, auto-dismiss alert
- feat: dashboard with stats, status, changelog
- feat: am/pm inference in quick time modify
- feat: redesig web console with Linear dark theme
- feat: in-place caldav event update instead of delete+create
- feat: regex quick time modify
- feat: regex quick modify for time changes
- feat: ai settings model dropdown + inline buttons
- feat: caldav calendar dropdown + inline buttons
- feat: add modify delete context matching and bot commands
- feat: wire core extraction pipeline
- feat: add event records view with filter
- feat: add telegram settings with bind links
- feat: add caldav settings with connection test
- feat: wire ai provider model checks
- feat: add ai settings scaffold
- feat: add authenticated admin shell
- feat: add application foundation scaffold

### Bug Fixes
- fix: unify event display formatting — 统一日程展示格式
- fix: dedupe current events by event id — 用事件ID去重当前日程
- fix: scope event replies by conversation — 按会话范围定位日程回复
- fix: harden reply target binding — 加固回复消息目标定位
- fix: support semantic modify transforms — 支持语义化修改转换
- fix: route multi-field modify to AI — 多字段修改交由AI完整处理
- fix: clear web and Discord handler typing errors — 清理Web和Discord处理器类型错误
- fix: clear service typing errors — 清理服务层类型错误
- fix: resolve database model import cycle — 修复数据库模型导入循环
- fix: define admin password generator — 修复管理员初始密码生成函数
- fix: handle empty reminders in modify result — 修改结果兼容空提醒列表
- fix: track event source for Discord vs Telegram — 事件来源区分Discord和Telegram
- fix: modify should only merge changed fields — 修改只合并AI实际返回的字段
- fix: relax validation for partial update events — 修改路径放宽校验支持部分字段更新
- fix: hard rule for reminder extraction in modify — 提醒修改提升为CRITICAL规则
- fix: clear reminder extraction rule in modify prompt — 明确提醒修改规则避免AI默认值
- fix: full event display on modify and multi-field prompt — 修改回复显示完整日程并优化AI提示
- fix: AI modify prompt supports reminders change — AI修改提示支持提醒时间修改
- fix: track Discord reply for modify/delete — Discord回复追踪以支持修改删除
- fix: strip @mentions from Discord messages — 清洗Discord消息中的@提及标记
- fix: allow DM and threads without @mention — 私聊和子线程无需@提及
- fix: Discord mention gate and manual auth — Discord需@提及且手动授权
- fix: add TYPE_CHECKING imports for Update/ContextTypes — 添加类型检查导入消LSP错误
- fix: AI error no longer in URL query string — AI错误不再出现在URL中
- fix: /list and /latest showed deleted/modified duplicates — 修复列表和最近显示已删除和重复日程
- fix: deleted events still shown in dashboard — 修复已删除日程仍显示在概览页
- fix: photo handler never reached and missing typing — 修复图片处理器无法触发且缺少输入提示
- fix: vision test/save redirect lost vision section — 修复识图测试保存重定向丢失识图区
- fix: vision save 422 and redirect hidden — 修复识图保存校验失败和重定向隐藏
- fix: vision model pull reverted to main model — 修复识图模型拉取返回主模型
- fix: month_str format broke month event count — 修复本月日程统计
- fix: dashboard stats accuracy and layout — 概览统计准确性及布局优化
- fix: await _write_one — 修复写入事件未等待异步完成
- fix: no_event commit and same-day date format — 修复无事件提交和同日时间显示
- fix: make _write_one async — 修复异步函数定义
- fix: save caldav form values on calendar list — 拉取日历列表时保存表单信息
- fix: version calc and changes interval
- fix: bigger icon 28px
- fix: enlarge topbar icon
- fix: save new href on update record
- fix: auto table columns width
- fix: replace pending with failed, 6-col table, chinese labels
- fix: sleep 1.5s after bot cancel before reload
- fix: cancel without await to prevent loop crash
- fix: async reload with await old task
- fix: remove orphaned js in ai.html
- fix: move dropdown js to global base template
- fix: ai models save on pull, 3s dismiss global, test route request
- fix: inject version globally into templates
- fix: make event_record_limit optional
- fix: optional username, vision layout, backup button
- fix: cleanup system settings layout
- fix: remove redundant topbar nav
- fix: dashboard only show create+success events
- fix: bot redirect, auto version from ci
- fix: 24h overflow and event_json refresh on modify
- fix: modify via delete+create instead of in-place
- fix: merge only changed fields, dont backfill
- fix: decode ical bytes to str for obj.data
- fix: modify prompt with am/pm context, better merge
- fix: use pat for ghcr auth
- fix: use write-all permissions
- fix: lowercase ghcr image name
- fix: cache bust css
- fix: quick modify end_time +1h
- fix: recalc end_time when start changes in merge
- fix: default 1h end_time on start change
- fix: shift end_time when start_time changes
- fix: prioritize href over uid for delete
- fix: only modify when replying to a message
- fix: simplified modify prompt, ai returns diff only
- fix: lenient ai parsing, preserve intent on validation fail
- fix: always treat target event reply as modify
- fix: _to_dict helper, remove all raw model_dump calls
- fix: unifying dict and pydantic access in modify path
- fix: handle dict in format modify result
- fix: ai modify returns diff, always merge with existing
- fix: enforce non-null title in ai modify output
- fix: merge ai modify result with existing event data
- fix: provide existing event context for ai modify
- fix: narrow modify keyword matching
- fix: delete by href instead of uid search
- fix: caldav delete error tracking and return check
- fix: add more delete keyword variants
- fix: delete record arg name mismatch
- fix: calendar url autofill on page load
- fix: use first calendar when none configured
- fix: wrap ai chat errors with better messages
- fix: rebuild caldav service with correct class structure
- fix: escape json braces in prompt template
- fix: strip markdown from ai json responses
- fix: use select by telegram_user_id not primary key
- fix: use broad non-command filter
- fix: restore _handle_message body and fix ptb v22 filter
- fix: proper old bot shutdown before new one
- fix: add error handling for message processing
- fix: add global declaration for _runtime
- fix: telegram token and username fallback to stored
- fix: ptb v22 async context manager for bot startup
- fix: list calendars always fallback to stored credentials
- fix: rewrite caldav to use get_calendars, remove principal refs
- fix: zero-propf find calendar fallback
- fix: caldav password fallback and api compat
- fix: robust caldav fallback with display name
- fix: cleaner caldav fallback when depth1 fails
- fix: use principal url for propfind
- fix: propfind manual xml fallback for synology
- fix: caldav list indent and error tracking
- fix: correct davresponse parser for propfind
- fix: caldav propfind depth1 for synology
- fix: caldav calendar discovery fallback
- fix: increase caldav timeout to 120s
- fix: remove requests dependency for caldav v3
- fix: add explicit basic auth for caldav
- fix: disable ssl verification for self-signed certs
- fix: preserve trailing slash in caldav urls
- fix: better caldav error msg and bot error display
- fix: caldav password, bot startup, force password change

### Refactoring
- refactor: introduce channel context — 引入渠道上下文抽象
- refactor: unified flash messages across all pages
- refactor: ai-driven intent routing, remove keyword matching
- refactor: rename web console routes

### Documentation
- docs: update readme version badge and caldav table
- docs: rewrite readme as mature product
- docs: clarify docker deployment flow
- docs: define self-hosted calendar assistant scope

### Maintenance
- ci: auto version based on conventional commits
- chore: ignore sisyphus dir
- chore: add sisyphus rules
- ci: trigger build on tag push
- ci: auto-build docker on push to main
- chore: use ghcr published image
- chore: add local docker deployment setup
