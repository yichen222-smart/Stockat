#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path

from app.commands.router import CommandRouter
from app.db import Database
from app.settings import Settings
from app.users.repository import UserScopedRepository
from app.users.service import UserService


ROOT = Path("/opt/grid-assistant")
PRIMARY_HOME = ROOT / "data" / "hermes"
SECOND_HOME = ROOT / "data" / "hermes-member2"


def approved_user(home: Path) -> str:
    path = home / "platforms" / "pairing" / "weixin-approved.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if len(data) != 1:
        raise AssertionError("expected exactly one approved Weixin user per gateway")
    return next(iter(data))


def main() -> int:
    settings = Settings.load(ROOT)
    db = Database(settings.database_path)
    db.migrate()
    router = CommandRouter(db, settings.invite_ttl_minutes, settings)
    users = UserService(db, settings.invite_ttl_minutes)
    repo = UserScopedRepository(db)

    admin_weixin = approved_user(PRIMARY_HOME)
    member_weixin = approved_user(SECOND_HOME)
    assert admin_weixin != member_weixin
    admin = users.by_wechat(admin_weixin)
    member = users.by_wechat(member_weixin)
    assert admin and admin.role == "admin" and admin.status == "active"
    assert member and member.role == "member" and member.status == "active"

    admin_sector = "验收板块甲"
    member_sector = "验收板块乙"
    admin_stock = "990001"
    member_stock = "990002"
    original_admin_report = admin.morning_report_enabled
    original_member_report = member.morning_report_enabled

    try:
        os.environ["HERMES_HOME"] = str(PRIMARY_HOME)
        assert "已添加板块" in router.handle(admin_weixin, f"添加板块：{admin_sector}")
        repo.add_stock(admin.id, admin_stock, "验收股票甲", grid_enabled=True)

        os.environ["HERMES_HOME"] = str(SECOND_HOME)
        assert "已添加板块" in router.handle(member_weixin, f"添加板块：{member_sector}")
        repo.add_stock(member.id, member_stock, "验收股票乙", grid_enabled=True)

        os.environ["HERMES_HOME"] = str(PRIMARY_HOME)
        admin_view = router.handle(admin_weixin, "查看我的关注")
        os.environ["HERMES_HOME"] = str(SECOND_HOME)
        member_view = router.handle(member_weixin, "查看我的关注")
        assert admin_sector in admin_view and member_sector not in admin_view
        assert admin_stock in admin_view and member_stock not in admin_view
        assert member_sector in member_view and admin_sector not in member_view
        assert member_stock in member_view and admin_stock not in member_view

        denied = router.handle(member_weixin, "添加用户")
        assert "管理员" in denied

        os.environ["HERMES_HOME"] = str(PRIMARY_HOME)
        assert "成交已记录" in router.handle(admin_weixin, f"记录买入：{admin_stock}")
        os.environ["HERMES_HOME"] = str(SECOND_HOME)
        assert "成交已记录" in router.handle(member_weixin, f"记录卖出：{member_stock}")
        os.environ["HERMES_HOME"] = str(PRIMARY_HOME)
        assert "已撤销" in router.handle(admin_weixin, "撤销上一条网格成交记录")
        os.environ["HERMES_HOME"] = str(SECOND_HOME)
        assert "已撤销" in router.handle(member_weixin, "撤销上一条网格成交记录")

        assert "早报已暂停" in router.handle(member_weixin, "暂停我的早报")
        admin_state = db.fetchone(
            "SELECT morning_report_enabled FROM users WHERE id=?", (admin.id,)
        )[0]
        member_state = db.fetchone(
            "SELECT morning_report_enabled FROM users WHERE id=?", (member.id,)
        )[0]
        assert admin_state == int(original_admin_report)
        assert member_state == 0

        routes = db.fetchall(
            "SELECT user_id,gateway_home FROM user_delivery_routes WHERE user_id IN (?,?)",
            (admin.id, member.id),
        )
        assert len(routes) == 2
        route_map = {row["user_id"]: row["gateway_home"] for row in routes}
        assert route_map[admin.id] == str(PRIMARY_HOME)
        assert route_map[member.id] == str(SECOND_HOME)
    finally:
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM grid_fill_log WHERE stock_code IN (?,?)",
                (admin_stock, member_stock),
            )
            conn.execute(
                "DELETE FROM user_grid_state WHERE stock_code IN (?,?)",
                (admin_stock, member_stock),
            )
            conn.execute(
                "DELETE FROM user_watch_stocks WHERE stock_code IN (?,?)",
                (admin_stock, member_stock),
            )
            conn.execute(
                "DELETE FROM user_watch_sectors WHERE sector_name IN (?,?)",
                (admin_sector, member_sector),
            )
            conn.execute(
                "UPDATE users SET morning_report_enabled=? WHERE id=?",
                (int(original_admin_report), admin.id),
            )
            conn.execute(
                "UPDATE users SET morning_report_enabled=? WHERE id=?",
                (int(original_member_report), member.id),
            )

    print(
        json.dumps(
            {
                "users": 2,
                "stable_distinct_ids": True,
                "database_isolation": True,
                "admin_boundary": True,
                "fill_isolation": True,
                "delivery_routes": 2,
                "temporary_data_cleaned": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
