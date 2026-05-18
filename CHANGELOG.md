# Changelog

## [v1.1.4] - 2026-05-18


### Bug Fixes

- preserve CalDAV event timezones as UTC — 将CalDAV日程时间按UTC写入



### Maintenance

- bump version to 1.1.3 [skip ci]


## [v1.1.3] - 2026-05-17


### Bug Fixes

- move web alerts from query params to flash — Web提示从URL参数改为Flash



### Maintenance

- bump version to 1.1.2 [skip ci]


## [v1.1.2] - 2026-05-17


### Bug Fixes

- persist admin password changes separately — 单独保存管理员密码修改



### Maintenance

- preserve v prefix in generated version and changelog — CI生成版本和更新日志保留v前缀

- bump version to 1.1.1 [skip ci]


## [v1.1.1] - 2026-05-17


### Bug Fixes

- render latest changelog sections and refresh sidebar version — 渲染最新更新日志分组并刷新侧边栏版本



### Maintenance

- bump version to 1.1.0, fix CI semver sort and changelog commit-back — 升级版本到1.1.0，修复CI semver排序和更新日志回写


## [v1.1.0] - 2026-05-17


### Bug Fixes

- restore copy_global_to for Discord guild command sync — 恢复copy_global_to确保guild级命令同步

- dead code in _send_telegram_replies and clear_commands wiping local command tree — 修复Telegram命令静默失败和Discord斜杠命令消失



### Features

- show vision model in /status and calendar source on dashboard — /status显示识图模型，概览页日历显示来源



### Maintenance

- auto-generate CHANGELOG.md and version in build — CI自动生成CHANGELOG和版本号


## [v1.0.0] - 2026-05-17


### Bug Fixes

- avoid duplicate Discord slash commands — 避免Discord斜杠命令重复

- copy changelog in Docker build — Docker构建复制更新日志



### Documentation

- refine readme scope and privacy notes — 调整README范围和隐私说明

- update readme for beta release — 更新Beta版本说明



### Features

- add Discord slash commands — 添加Discord斜杠命令

- share channel command handlers — 共享渠道命令处理



### Maintenance

- bump version to 1.0.0 — 升级版本到1.0.0



### Refactoring

- move Discord adapter to integrations — 移动Discord适配层


## [v1.0.0-beta.1] - 2026-05-17


### Bug Fixes

- unify event display formatting — 统一日程展示格式



### Maintenance

- bump version to 1.0.0-beta.1 — 升级版本到1.0.0-beta.1

- automate changelog generation — 自动化更新日志生成


## [v0.17.17] - 2026-05-17


### Bug Fixes

- dedupe current events by event id — 用事件ID去重当前日程


## [v0.17.16] - 2026-05-17


### Bug Fixes

- scope event replies by conversation — 按会话范围定位日程回复

- harden reply target binding — 加固回复消息目标定位

- support semantic modify transforms — 支持语义化修改转换

- route multi-field modify to AI — 多字段修改交由AI完整处理



### Refactoring

- introduce channel context — 引入渠道上下文抽象


## [v0.17.12] - 2026-05-17


### Bug Fixes

- clear web and Discord handler typing errors — 清理Web和Discord处理器类型错误

- clear service typing errors — 清理服务层类型错误

- resolve database model import cycle — 修复数据库模型导入循环

- define admin password generator — 修复管理员初始密码生成函数

- handle empty reminders in modify result — 修改结果兼容空提醒列表


## [v0.17.7] - 2026-05-16


### Bug Fixes

- track event source for Discord vs Telegram — 事件来源区分Discord和Telegram


## [v0.17.6] - 2026-05-16


### Bug Fixes

- modify should only merge changed fields — 修改只合并AI实际返回的字段


## [v0.17.5] - 2026-05-16


### Bug Fixes

- relax validation for partial update events — 修改路径放宽校验支持部分字段更新


## [v0.17.4] - 2026-05-16


### Bug Fixes

- hard rule for reminder extraction in modify — 提醒修改提升为CRITICAL规则


## [v0.17.3] - 2026-05-16


### Bug Fixes

- clear reminder extraction rule in modify prompt — 明确提醒修改规则避免AI默认值


## [v0.17.2] - 2026-05-16


### Bug Fixes

- full event display on modify and multi-field prompt — 修改回复显示完整日程并优化AI提示


## [v0.17.1] - 2026-05-16


### Bug Fixes

- AI modify prompt supports reminders change — AI修改提示支持提醒时间修改


## [v0.17.0] - 2026-05-16


### Features

- quick-modify support X点 format — 快捷修改支持X点格式


## [v0.16.4] - 2026-05-16


### Bug Fixes

- track Discord reply for modify/delete — Discord回复追踪以支持修改删除


## [v0.16.3] - 2026-05-16


### Bug Fixes

- strip @mentions from Discord messages — 清洗Discord消息中的@提及标记


## [v0.16.2] - 2026-05-16


### Bug Fixes

- allow DM and threads without @mention — 私聊和子线程无需@提及


## [v0.16.1] - 2026-05-16


### Bug Fixes

- Discord mention gate and manual auth — Discord需@提及且手动授权


## [v0.16.0] - 2026-05-16


### Features

- Discord channel support — Discord渠道支持


## [v0.15.1] - 2026-05-16


### Bug Fixes

- add TYPE_CHECKING imports for Update/ContextTypes — 添加类型检查导入消LSP错误


## [v0.15.0] - 2026-05-16


### Features

- rename /list to /upcoming with future days — 重命名命令为upcoming显示未来日程


## [v0.14.0] - 2026-05-16


### Features

- /list grouped by date with day limit — 列表按日期分组显示并限制天数


## [v0.13.1] - 2026-05-16


### Bug Fixes

- AI error no longer in URL query string — AI错误不再出现在URL中


## [v0.13.0] - 2026-05-16


### Features

- host network mode for production compose — 正式环境用host网络访问本地API


## [v0.12.0] - 2026-05-16


### Features

- host network mode for local API access — 开发环境用host网络访问本地API


## [v0.11.0] - 2026-05-16


### Features

- vision status on dashboard and unconfigured prompt — 概览显示识图状态及未配置提示


## [v0.10.0] - 2026-05-16


### Features

- show vision model status on dashboard — 概览页显示识图模型状态


## [v0.9.0] - 2026-05-16


### Features

- auto-start Telegram bot on startup — 启动时自动运行 Telegram Bot


## [v0.8.8] - 2026-05-16


### Bug Fixes

- /list and /latest showed deleted/modified duplicates — 修复列表和最近显示已删除和重复日程


## [v0.8.7] - 2026-05-16


### Bug Fixes

- deleted events still shown in dashboard — 修复已删除日程仍显示在概览页


## [v0.8.6] - 2026-05-16


### Bug Fixes

- photo handler never reached and missing typing — 修复图片处理器无法触发且缺少输入提示


## [v0.8.5] - 2026-05-16


### Bug Fixes

- vision test/save redirect lost vision section — 修复识图测试保存重定向丢失识图区


## [v0.8.4] - 2026-05-16


### Bug Fixes

- vision save 422 and redirect hidden — 修复识图保存校验失败和重定向隐藏


## [v0.8.3] - 2026-05-16


### Bug Fixes

- vision model pull reverted to main model — 修复识图模型拉取返回主模型


## [v0.8.2] - 2026-05-16


### Bug Fixes

- month_str format broke month event count — 修复本月日程统计


## [v0.8.1] - 2026-05-16


### Bug Fixes

- dashboard stats accuracy and layout — 概览统计准确性及布局优化


## [v0.8.0] - 2026-05-15


### Features

- quick-modify date support and iCal reminders — 快捷修改支持日期和日历提醒



### Other

- Revert "debug: log modify and delete operations — 调试修改删除操作"


## [v0.7.0] - 2026-05-15


### Features

- per-event separate replies for multi-event — 多条日程各自回复独立消息


## [v0.6.3] - 2026-05-15


### Bug Fixes

- await _write_one — 修复写入事件未等待异步完成


## [v0.6.2] - 2026-05-15


### Bug Fixes

- no_event commit and same-day date format — 修复无事件提交和同日时间显示


## [v0.6.1] - 2026-05-15


### Bug Fixes

- make _write_one async — 修复异步函数定义


## [v0.6.0] - 2026-05-15


### Features

- multi-event extraction, simplified reply — 支持多条日程提取，精简回复格式


## [v0.5.1] - 2026-05-15


### Bug Fixes

- save caldav form values on calendar list — 拉取日历列表时保存表单信息


## [v0.5.0] - 2026-05-15


### Features

- add typing indicator in tg — Telegram 回复时显示正在输入


## [v0.4.1] - 2026-05-15


### Bug Fixes

- version calc and changes interval


## [v0.4.0] - 2026-05-15


### Bug Fixes

- bigger icon 28px

- enlarge topbar icon

- save new href on update record



### Documentation

- update readme version badge and caldav table



### Features

- add favicon and calendar icon

- use png calendar icon in topbar

- add favicon link

- tg command redesign and auto-register



### Maintenance

- auto version based on conventional commits


## [v0.3.0] - 2026-05-15


### Bug Fixes

- auto table columns width

- replace pending with failed, 6-col table, chinese labels

- sleep 1.5s after bot cancel before reload

- cancel without await to prevent loop crash

- async reload with await old task

- remove orphaned js in ai.html

- move dropdown js to global base template

- ai models save on pull, 3s dismiss global, test route request

- inject version globally into templates

- make event_record_limit optional

- optional username, vision layout, backup button



### Documentation

- rewrite readme as mature product



### Features

- inline event detail rows, refresh button

- bind auth card with polling, user delete

- custom dark dropdowns for all selects, ai flash fix

- custom dark dropdown for ai provider

- toggle switch for vision model



### Maintenance

- ignore sisyphus dir

- add sisyphus rules



### Other

- Revert "chore: add sisyphus rules"



### Refactoring

- unified flash messages across all pages


## [v0.2.0] - 2026-05-15


### Bug Fixes

- cleanup system settings layout

- remove redundant topbar nav

- dashboard only show create+success events



### Features

- vision model support for tg photos

- ai settings ux with auto baseurl

- backup download, move clear to events page

- session flash messages



### Maintenance

- trigger build on tag push


## [v0.1.0] - 2026-05-14


### Bug Fixes

- bot redirect, auto version from ci

- 24h overflow and event_json refresh on modify

- modify via delete+create instead of in-place

- merge only changed fields, dont backfill

- decode ical bytes to str for obj.data

- modify prompt with am/pm context, better merge

- use pat for ghcr auth

- use write-all permissions

- lowercase ghcr image name

- cache bust css

- quick modify end_time +1h

- recalc end_time when start changes in merge

- default 1h end_time on start change

- shift end_time when start_time changes

- prioritize href over uid for delete

- only modify when replying to a message

- simplified modify prompt, ai returns diff only

- lenient ai parsing, preserve intent on validation fail

- always treat target event reply as modify

- _to_dict helper, remove all raw model_dump calls

- unifying dict and pydantic access in modify path

- handle dict in format modify result

- ai modify returns diff, always merge with existing

- enforce non-null title in ai modify output

- merge ai modify result with existing event data

- provide existing event context for ai modify

- narrow modify keyword matching

- delete by href instead of uid search

- caldav delete error tracking and return check

- add more delete keyword variants

- delete record arg name mismatch

- calendar url autofill on page load

- use first calendar when none configured

- wrap ai chat errors with better messages

- rebuild caldav service with correct class structure

- escape json braces in prompt template

- strip markdown from ai json responses

- use select by telegram_user_id not primary key

- use broad non-command filter

- restore _handle_message body and fix ptb v22 filter

- proper old bot shutdown before new one

- add error handling for message processing

- add global declaration for _runtime

- telegram token and username fallback to stored

- ptb v22 async context manager for bot startup

- list calendars always fallback to stored credentials

- rewrite caldav to use get_calendars, remove principal refs

- zero-propf find calendar fallback

- caldav password fallback and api compat

- robust caldav fallback with display name

- cleaner caldav fallback when depth1 fails

- use principal url for propfind

- propfind manual xml fallback for synology

- caldav list indent and error tracking

- correct davresponse parser for propfind

- caldav propfind depth1 for synology

- caldav calendar discovery fallback

- increase caldav timeout to 120s

- remove requests dependency for caldav v3

- add explicit basic auth for caldav

- disable ssl verification for self-signed certs

- preserve trailing slash in caldav urls

- better caldav error msg and bot error display

- caldav password, bot startup, force password change



### Documentation

- clarify docker deployment flow

- define self-hosted calendar assistant scope



### Features

- manual release workflow, auto-dismiss alert

- dashboard with stats, status, changelog

- am/pm inference in quick time modify

- redesig web console with Linear dark theme

- in-place caldav event update instead of delete+create

- regex quick time modify

- regex quick modify for time changes

- ai settings model dropdown + inline buttons

- caldav calendar dropdown + inline buttons

- add modify delete context matching and bot commands

- wire core extraction pipeline

- add event records view with filter

- add telegram settings with bind links

- add caldav settings with connection test

- wire ai provider model checks

- add ai settings scaffold

- add authenticated admin shell

- add application foundation scaffold



### Maintenance

- auto-build docker on push to main

- use ghcr published image

- add local docker deployment setup



### Other

- english to chinese in ui



### Refactoring

- ai-driven intent routing, remove keyword matching

- rename web console routes


