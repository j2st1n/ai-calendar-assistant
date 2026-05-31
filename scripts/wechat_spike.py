#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.ilink import ILinkClient, ILinkError

HELP = """
Commands:
  qrcode                 Fetch and print a login QR payload
  status <qrcode>        Check QR scan/login status
  poll                   Long-poll messages once and print JSON
  send <uid> <ctx> <txt> Send a text reply using context_token
  help                   Show this help
  quit                   Exit
""".strip()


def _token_from_args() -> str:
    if len(sys.argv) > 1 and sys.argv[1] not in {"help", "--help", "-h"}:
        return sys.argv[1]
    token = os.environ.get("WECHAT_BOT_TOKEN", "")
    if token:
        return token
    if len(sys.argv) > 1:
        print("Usage: WECHAT_BOT_TOKEN=<token> python scripts/wechat_spike.py", file=sys.stderr)
        print("   or: python scripts/wechat_spike.py <token>", file=sys.stderr)
        raise SystemExit(0)
    return getpass("WECHAT_BOT_TOKEN: ").strip()


async def main() -> None:
    token = _token_from_args()
    if not token:
        print("Missing bot token.", file=sys.stderr)
        raise SystemExit(1)
    client = ILinkClient(token)
    updates_buf = ""
    print(HELP)
    while True:
        try:
            line = input("wechat> ").strip()
        except EOFError:
            print()
            return
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"Parse error: {exc}")
            continue
        command = parts[0].lower()
        try:
            if command in {"quit", "exit"}:
                return
            if command == "help":
                print(HELP)
            elif command == "qrcode":
                print(await client.get_qrcode())
            elif command == "status":
                if len(parts) != 2:
                    print("Usage: status <qrcode>")
                    continue
                print(await client.get_qrcode_status(parts[1]))
            elif command == "poll":
                msgs, updates_buf = await client.get_updates(updates_buf)
                print(json.dumps({"count": len(msgs), "get_updates_buf": updates_buf, "msgs": msgs}, ensure_ascii=False, indent=2))
            elif command == "send":
                if len(parts) < 4:
                    print("Usage: send <to_user_id> <context_token> <text>")
                    continue
                response = await client.send_message(parts[1], parts[2], " ".join(parts[3:]))
                print(json.dumps(response, ensure_ascii=False, indent=2))
            else:
                print(f"Unknown command: {command}")
        except ILinkError as exc:
            print(f"iLink error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
