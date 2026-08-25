from __future__ import annotations

from app.db import Database
from app.users.service import utcnow


class UserScopedRepository:
    """Every public method requires a user_id; no unscoped content reads."""

    def __init__(self, db: Database):
        self.db = db

    def add_sector(self, user_id: str, sector_name: str) -> None:
        sector_name = sector_name.strip()
        if not sector_name:
            raise ValueError("板块名称不能为空")
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_watch_sectors(user_id,sector_name,created_at,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id,sector_name)
                DO UPDATE SET enabled=1,updated_at=excluded.updated_at
                """,
                (user_id, sector_name, now, now),
            )

    def remove_sector(self, user_id: str, sector_name: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE user_watch_sectors SET enabled=0,updated_at=?
                WHERE user_id=? AND sector_name=? AND enabled=1
                """,
                (utcnow(), user_id, sector_name.strip()),
            )
            return cur.rowcount > 0

    def sectors(self, user_id: str) -> list[str]:
        return [
            row["sector_name"]
            for row in self.db.fetchall(
                """
                SELECT sector_name FROM user_watch_sectors
                WHERE user_id=? AND enabled=1 ORDER BY id
                """,
                (user_id,),
            )
        ]

    def add_stock(
        self,
        user_id: str,
        stock_code: str,
        stock_name: str,
        *,
        grid_enabled: bool,
    ) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_watch_stocks(
                    user_id,stock_code,stock_name,grid_enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id,stock_code) DO UPDATE SET
                    stock_name=excluded.stock_name,
                    grid_enabled=MAX(user_watch_stocks.grid_enabled,excluded.grid_enabled),
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (user_id, stock_code, stock_name, int(grid_enabled), now, now),
            )
            if grid_enabled:
                conn.execute(
                    """
                    INSERT INTO user_grid_state(user_id,stock_code,status,updated_at)
                    VALUES(?,?,'ACTIVE',?)
                    ON CONFLICT(user_id,stock_code) DO UPDATE SET
                        status='ACTIVE',stop_reason=NULL,updated_at=excluded.updated_at
                    """,
                    (user_id, stock_code, now),
                )

    def remove_stock(self, user_id: str, stock_code: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE user_watch_stocks SET enabled=0,updated_at=?
                WHERE user_id=? AND stock_code=? AND enabled=1
                """,
                (utcnow(), user_id, stock_code),
            )
            return cur.rowcount > 0

    def set_sector_enabled(self, user_id: str, sector_name: str, enabled: bool) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE user_watch_sectors SET enabled=?,updated_at=?
                WHERE user_id=? AND sector_name=?
                """,
                (int(enabled), utcnow(), user_id, sector_name.strip()),
            )
            return cur.rowcount > 0

    def set_stock_news(self, user_id: str, stock_code: str, enabled: bool) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE user_watch_stocks SET news_enabled=?,updated_at=?
                WHERE user_id=? AND stock_code=? AND enabled=1
                """,
                (int(enabled), utcnow(), user_id, stock_code),
            )
            return cur.rowcount > 0

    def stop_grid_tracking(self, user_id: str, stock_code: str) -> bool:
        stamp = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE user_watch_stocks SET grid_enabled=0,updated_at=?
                WHERE user_id=? AND stock_code=? AND enabled=1 AND grid_enabled=1
                """,
                (stamp, user_id, stock_code),
            )
            if cur.rowcount:
                conn.execute(
                    """
                    UPDATE user_grid_state SET status='STOPPED',stop_reason=?,updated_at=?
                    WHERE user_id=? AND stock_code=?
                    """,
                    ("用户停止跟踪", stamp, user_id, stock_code),
                )
            return cur.rowcount > 0

    def grids(self, user_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT w.stock_code,w.stock_name,
                       COALESCE(s.status,'ACTIVE') AS status,s.stop_reason,
                       p.reference_price,p.buy_grid_pct,p.sell_grid_pct,
                       p.normal_lower,p.normal_upper,p.extreme_lower,p.extreme_upper,
                       p.data_until
                FROM user_watch_stocks w
                LEFT JOIN user_grid_state s
                  ON s.user_id=w.user_id AND s.stock_code=w.stock_code
                LEFT JOIN grid_parameter_cache p ON p.stock_code=w.stock_code
                WHERE w.user_id=? AND w.enabled=1 AND w.grid_enabled=1
                ORDER BY w.id
                """,
                (user_id,),
            )
        ]
    def stocks(self, user_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT stock_code,stock_name,news_enabled,grid_enabled
                FROM user_watch_stocks
                WHERE user_id=? AND enabled=1 ORDER BY id
                """,
                (user_id,),
            )
        ]

    def set_morning_report(self, user_id: str, enabled: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET morning_report_enabled=?,updated_at=? WHERE id=?",
                (int(enabled), utcnow(), user_id),
            )
