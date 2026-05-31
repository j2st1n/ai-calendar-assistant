# WeChat iLink P0 Spike

This is a manual protocol probe for WeChat ClawBot/iLink. It does not connect to the app runtime, database, MessageProcessor, Web UI, or CalDAV.

## Requirements

Set a ClawBot/iLink bot token from the Tencent/OpenClaw login flow:

```bash
export WECHAT_BOT_TOKEN="..."
```

Do not commit or paste the token into logs or issues.

## Run

```bash
python scripts/wechat_spike.py
```

You can also pass the token as the first argument for local one-off testing:

```bash
python scripts/wechat_spike.py "$WECHAT_BOT_TOKEN"
```

## Commands

```text
qrcode                 Fetch and print a login QR payload
status <qrcode>        Check QR scan/login status
poll                   Long-poll messages once and print JSON
send <uid> <ctx> <txt> Send a text reply using context_token
help                   Show usage
quit                   Exit
```

## What To Record During P0

- Whether `get_qrcode` returns a URL, base64 image, or raw QR payload.
- Whether inbound messages include a stable message id.
- Whether inbound messages include a stable chat/session id.
- Whether `context_token` changes on each message or is stable per conversation.
- Whether `get_updates_buf` prevents duplicate messages after restart.
- What token-expired and auth-failed responses look like.

## Out Of Scope

- MessageProcessor integration.
- Web settings.
- DB persistence.
- Media/image download and AES handling.
- Long-running runtime service.
