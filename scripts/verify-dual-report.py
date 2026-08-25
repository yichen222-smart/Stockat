#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from app.db import Database
from app.settings import Settings


ROOT = Path("/opt/grid-assistant")


def main() -> int:
    settings = Settings.load(ROOT)
    db = Database(settings.database_path)
    rows = db.fetchall(
        """
        SELECT r.status, r.content, d.gateway_home
        FROM user_reports r
        JOIN user_delivery_routes d ON d.user_id=r.user_id
        WHERE r.report_date=?
        ORDER BY r.user_id
        """,
        (date.today().isoformat(),),
    )
    hashes = {
        hashlib.sha256(row["content"].encode("utf-8")).hexdigest() for row in rows
    }
    contents = [row["content"] for row in rows]
    primary = next((row["content"] for row in rows if row["gateway_home"] == str(ROOT / "data" / "hermes")), "")
    member = next((row["content"] for row in rows if row["gateway_home"] == str(ROOT / "data" / "hermes-member2")), "")
    fixture_rows = db.fetchone(
        """
        SELECT
          (SELECT COUNT(*) FROM user_watch_sectors
           WHERE sector_name IN ('验收银行', '验收新能源')) +
          (SELECT COUNT(*) FROM user_watch_stocks
           WHERE stock_code IN ('600036', '300750'))
        """
    )[0]
    routes = db.fetchone(
        "SELECT COUNT(DISTINCT user_id) FROM user_delivery_routes"
    )[0]
    result = {
        "reports": len(rows),
        "all_sent": all(row["status"] == "sent" for row in rows),
        "contents_distinct": len(hashes) == len(rows) == 2,
        "primary_stock_isolated": "600036" in primary and "300750" not in primary,
        "member_stock_isolated": "300750" in member and "600036" not in member,
        "delivery_routes": routes,
        "fixture_rows_remaining": fixture_rows,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(
        (
            result["reports"] == 2,
            result["all_sent"],
            result["contents_distinct"],
            result["primary_stock_isolated"],
            result["member_stock_isolated"],
            result["delivery_routes"] == 2,
            result["fixture_rows_remaining"] == 0,
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
