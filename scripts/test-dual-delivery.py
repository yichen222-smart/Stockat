#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from app.db import Database
from app.messaging.hermes_weixin import send_text
from app.settings import Settings


def main() -> int:
    root = Path("/opt/grid-assistant")
    settings = Settings.load(root)
    db = Database(settings.database_path)
    rows = db.fetchall(
        """
        SELECT u.role,u.wechat_user_id,r.gateway_home
        FROM users u
        JOIN user_delivery_routes r ON r.user_id=u.id
        WHERE u.status='active'
        ORDER BY u.created_at
        """
    )
    if len(rows) != 2:
        raise AssertionError("expected two active users with delivery routes")
    sent_roles: list[str] = []
    for row in rows:
        label = "管理员" if row["role"] == "admin" else "第二用户"
        send_text(
            row["wechat_user_id"],
            f"服务器重启后验收：{label}通道已自动恢复。",
            gateway_home=row["gateway_home"],
        )
        sent_roles.append(row["role"])
    print(json.dumps({"sent": len(rows), "roles": sorted(sent_roles)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
