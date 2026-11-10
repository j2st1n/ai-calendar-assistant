# 微信扫码登录链路修复（v1.22.5 之后）

本文记录「扫码后控制台无反应」的根因、修复后的完整链路与回归测试，依据腾讯
openclaw-weixin 官方协议文档的「二维码登录」一节。

## 缺陷清单与修复对照

| # | 缺陷 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 扫码后控制台状态永不更新 | `channels.html` 轮询 `/console/wechat/qr/poll`，后端只注册了 `/console/wechat/qr/status`（404 自 a24a233 引入） | 模板改为轮询 `/console/wechat/qr/status`；新增测试锁死模板 URL 必须在路由表中 |
| 2 | 状态永远不推进 | `ilink.py` 的 `_request` 默认 15 秒超时，而官方 `get_qrcode_status` 是约 35 秒的长轮询；`ReadTimeout` 被当成 unknown | 状态轮询单独使用 40 秒客户端超时（≥35 秒）；超时/网络错误统一归类为 `status=wait` 后继续 |
| 3 | 状态映射缺失 | 前端只判断 `scanned`（官方拼写是 `scaned`），没有 confirmed 保存、need_verifycode、verify_code_blocked、scaned_but_redirect、binded_redirect 分支 | 按官方状态机补齐后端归一化与前端分支 |
| 4 | 登录成功不保存凭证 | `confirmed` 只展示成功并跳转，从未调用 `POST /console/wechat/save` | `confirmed` 立即调用 save，成功后展示「已保存」并刷新 |
| 5 | 凭证不完整 | save 只存 `bot_token` 并清 cursor，不存 baseurl / ilink_bot_id / ilink_user_id | save 一并持久化 `wechat_bot_baseurl`、`wechat_ilink_bot_id`、`wechat_ilink_user_id`（仅在返回时保存），并清空 `wechat_updates_buf` |
| 6 | 运行时不使用保存的 baseurl | `WechatService` 总是 `ILinkClient(token)` 使用默认地址 | runtime/probe 读取保存的 baseurl；缺省仍为 `https://ilinkai.weixin.qq.com` |
| 7 | 固定间隔轮询与长轮询叠加 | `setInterval` 每 1.5 秒发一次，35 秒长轮询会并发堆积 | 改为串行长轮询：上一轮 fetch 完成后才调度下一轮（`setTimeout` + `.finally`） |

## 官方二维码登录状态机

`POST /ilink/bot/get_bot_qrcode?bot_type=3` 取码后，用返回的 `qrcode` 轮询
`GET /ilink/bot/get_qrcode_status`。单次请求约 35 秒，超时/取消/网络错误都按
`wait` 继续。

| status | 后端行为 | 控制台行为 |
| --- | --- | --- |
| `wait` | 原样返回 | 继续等待；超过 30 轮（约 20 分钟）提示重新取码 |
| `scaned` | 原样返回 | 「已扫码，请在手机上确认登录」，清空并停止携带 verify_code |
| `confirmed` | 把 `bot_token` / `ilink_bot_id` / `baseurl` / `ilink_user_id` 写入 5 分钟内存缓存，响应只给 `has_token` 布尔 | 立即 `POST /console/wechat/save`，成功后展示已保存并刷新页面 |
| `expired` | 原样返回 | 提示二维码已过期，回到「获取二维码」 |
| `need_verifycode` / `confirmed_but_need_verifycode` | 原样返回并附带 `need_verifycode: true` | 显示数字输入框；提交后下一轮轮询携带 `verify_code` |
| `verify_code_blocked` | 原样返回 | 提示错误次数过多，要求重新取码 |
| `scaned_but_redirect` | 返回 `redirect_host` | 后续轮询携带 `redirect_host`，改到 `https://<redirect_host>` |
| `binded_redirect` | 原样返回 | 提示该微信号已绑定、无需重复扫码，停止轮询 |

凭证缓存（`app/integrations/ilink.py`，TTL 300 秒、带线程锁、最多 32 条）让
`/console/wechat/save` 复用 `confirmed` 的原始载荷，避免二次长轮询在
`binded_redirect` 上丢掉 token；缓存缺失时才回退到一次新的状态查询。

## 代码位置

- `app/integrations/ilink.py`：长轮询状态查询、`verify_code`、`redirect_host`、
  传输错误归 `wait`、base_url 归一化、confirmed 凭证缓存。
- `app/services/wechat_service.py`：runtime 支持保存的 baseurl（启动自愈时也读取）。
- `app/web/routes.py`：`/console/wechat/qr/status` 接收 `verify_code` /
  `redirect_host` 且不回显 token；`/console/wechat/save` 持久化完整凭证并清 cursor；
  启动、停止、探测均使用保存的 baseurl。
- `app/web/templates/channels.html`：控制台串行长轮询与完整状态映射（保留原 DOM 与布局，
  新增验证码输入框 `#qr-verifycode` 与「重试保存登录凭证」按钮）。
- `app/web/templates/wechat.html`：旧页面同步修复轮询地址、串行化与状态映射。

## 回归测试

```bash
.venv/bin/pytest tests/test_ilink.py tests/test_wechat_routes.py tests/test_channels_routes.py -q
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app
```

关键断言：状态轮询客户端超时 ≥35 秒；`ReadTimeout`/连接错误 → `wait`；`verify_code`
与 `redirect_host` 透传；`/qr/status` 响应不含 token 明文；save 保存
baseurl/ids 并清 cursor、runtime 以保存的 baseurl 启动；模板 URL 必须与路由注册
一致，且二维码轮询不再使用 `setInterval`。

## 部署与回滚

- 运行时需要同步的文件：`app/integrations/ilink.py`、
  `app/services/wechat_service.py`、`app/web/routes.py`、
  `app/web/templates/channels.html`、`app/web/templates/wechat.html`。
- 无数据库迁移、无新增依赖；新设置键按需写入 `settings` 表。
- 回滚：还原上述文件并重启容器即可；已保存的 `wechat_bot_baseurl` 等键在旧代码下
  被忽略，不影响旧逻辑。
