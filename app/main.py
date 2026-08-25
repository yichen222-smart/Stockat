from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.commands.router import CommandRouter
from app.db import Database
from app.scheduler.daily import DailyRun
from app.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="stockat")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    daily = sub.add_parser("daily")
    daily.add_argument("--force", action="store_true")
    daily.add_argument("--no-send", action="store_true")
    daily.add_argument("--scheduled", action="store_true")
    daily.add_argument("--only-user-id")
    daily.add_argument(
        "--as-of",
        help="按指定北京时间模拟，例如 2026-08-10T08:20:00",
    )
    command = sub.add_parser("command")
    command.add_argument("--wechat-user-id")
    command.add_argument("--display-name")
    command.add_argument("text")
    sub.add_parser("health")
    args = parser.parse_args()
    settings = Settings.load(args.root)
    db = Database(settings.database_path)
    db.migrate()
    if args.command == "migrate":
        print("migration=ok")
    elif args.command == "daily":
        as_of = None
        if args.as_of:
            as_of = datetime.fromisoformat(args.as_of)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=ZoneInfo(settings.timezone))
        result = DailyRun(settings, db).execute(
            force=args.force,
            send=not args.no_send,
            scheduled=args.scheduled,
            only_user_id=args.only_user_id,
            as_of=as_of,
        )
        print(json.dumps(result, ensure_ascii=False))
        if result.get("status") in {"partial", "error"}:
            return 1
    elif args.command == "health":
        row = db.fetchone("PRAGMA journal_mode")
        print(json.dumps({"database": "ok", "journal_mode": row[0]}, ensure_ascii=False))
    else:
        wechat_user_id = args.wechat_user_id or os.environ.get("HERMES_SESSION_USER_ID", "")
        platform = os.environ.get("HERMES_SESSION_PLATFORM", "")
        if not wechat_user_id:
            parser.error("缺少稳定微信用户 ID")
        if not args.wechat_user_id and platform != "weixin":
            parser.error("自动身份模式仅允许来自微信网关会话")
        print(CommandRouter(db, settings.invite_ttl_minutes, settings).handle(
            wechat_user_id, args.text, args.display_name
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
