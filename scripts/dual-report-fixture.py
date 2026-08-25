#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.db import Database
from app.settings import Settings
from app.users.repository import UserScopedRepository
from app.users.service import UserService


ROOT = Path("/opt/grid-assistant")
PRIMARY_HOME = ROOT / "data" / "hermes"
SECOND_HOME = ROOT / "data" / "hermes-member2"


def approved_user(home: Path) -> str:
    data = json.loads(
        (home / "platforms" / "pairing" / "weixin-approved.json").read_text(
            encoding="utf-8"
        )
    )
    return next(iter(data))


def main() -> int:
    action = sys.argv[1]
    settings = Settings.load(ROOT)
    db = Database(settings.database_path)
    db.migrate()
    users = UserService(db, settings.invite_ttl_minutes)
    repo = UserScopedRepository(db)
    admin = users.by_wechat(approved_user(PRIMARY_HOME))
    member = users.by_wechat(approved_user(SECOND_HOME))
    assert admin and member

    fixtures = (
        (admin.id, "验收银行", "600036", "招商银行"),
        (member.id, "验收新能源", "300750", "宁德时代"),
    )
    if action == "setup":
        for user_id, sector, code, name in fixtures:
            repo.add_sector(user_id, sector)
            repo.add_stock(user_id, code, name, grid_enabled=True)
        print(json.dumps({"fixture": "ready", "users": 2}))
    elif action == "cleanup":
        with db.transaction() as conn:
            for user_id, sector, code, _ in fixtures:
                conn.execute(
                    "DELETE FROM grid_fill_log WHERE user_id=? AND stock_code=?",
                    (user_id, code),
                )
                conn.execute(
                    "DELETE FROM user_grid_state WHERE user_id=? AND stock_code=?",
                    (user_id, code),
                )
                conn.execute(
                    "DELETE FROM user_watch_stocks WHERE user_id=? AND stock_code=?",
                    (user_id, code),
                )
                conn.execute(
                    "DELETE FROM user_watch_sectors WHERE user_id=? AND sector_name=?",
                    (user_id, sector),
                )
        print(json.dumps({"fixture": "cleaned", "users": 2}))
    else:
        raise ValueError("expected setup or cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
