#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import Database
from app.messaging.hermes_weixin import send_text
from app.reports.builder import split_wechat
from app.scheduler.daily import DailyRun, WEIXIN_PART_INTERVAL_SECONDS
from app.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a morning snapshot and send exactly three test parts to one confirmed admin."
    )
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--confirm-admin-user-id", required=True)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    settings = Settings.load()
    db = Database(settings.database_path)
    db.migrate()
    admins = [
        dict(row)
        for row in db.fetchall(
            """
            SELECT u.*,r.gateway_home
            FROM users u JOIN user_delivery_routes r ON r.user_id=u.id
            WHERE u.role='admin' AND u.status='active'
            """
        )
    ]
    if len(admins) != 1 or admins[0]["id"] != args.confirm_admin_user_id:
        raise SystemExit("admin-confirmation-mismatch")
    admin = admins[0]
    timezone = ZoneInfo(settings.timezone)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone)
    else:
        as_of = as_of.astimezone(timezone)

    run = DailyRun(settings, db)
    common = run._collect_common_material(as_of.date(), as_of)
    grid_params = run._calculate_unique_grids()
    candidates = run._candidate_news(as_of.date(), as_of)
    content = run._build_user_report(
        admin, candidates, grid_params, common, report_now=as_of
    )
    parts = split_wechat(content)
    if len(parts) != 3:
        raise SystemExit(f"unexpected-report-parts={len(parts)}")

    summary = {
        "mode": "send" if args.send else "preview",
        "admin_only": True,
        "parts": len(parts),
        "profile_generated_at": common.get("stock_profile_generated_at"),
        "news_candidates": len(candidates),
        "stock_status": common.get("stock_status"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.send:
        print("\n\n".join(parts))
        return 0

    gateway_home = run._require_gateway_home(admin)
    for index, part in enumerate(parts, 1):
        test_part = f"【四维新闻功能测试｜模拟{as_of:%Y-%m-%d %H:%M}】\n{part}"
        send_text(admin["wechat_user_id"], test_part, gateway_home=gateway_home)
        print(json.dumps({"part": index, "sent": True}, ensure_ascii=False))
        if index < len(parts):
            time.sleep(WEIXIN_PART_INTERVAL_SECONDS)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO command_audit(
                user_id,wechat_user_id,command_name,success,detail,created_at
            ) VALUES(?,?,?,1,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (
                admin["id"],
                admin["wechat_user_id"],
                "admin_four_dimension_test",
                json.dumps(summary, ensure_ascii=False),
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
