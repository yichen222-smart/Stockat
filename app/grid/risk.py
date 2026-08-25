from __future__ import annotations

from app.db import Database
from app.users.service import utcnow


class GridRiskService:
    def __init__(self, db: Database):
        self.db = db

    def report_fill(
        self, user_id: str, stock_code: str, direction: str, levels: int = 1
    ) -> dict:
        direction = direction.upper()
        if direction not in {"BUY", "SELL"} or not 1 <= levels <= 5:
            raise ValueError("成交方向或档数无效")
        with self.db.transaction() as conn:
            watch = conn.execute(
                """
                SELECT 1 FROM user_watch_stocks
                WHERE user_id=? AND stock_code=? AND grid_enabled=1 AND enabled=1
                """,
                (user_id, stock_code),
            ).fetchone()
            if not watch:
                raise ValueError("这不是你的启用中网格股票")
            state = conn.execute(
                "SELECT * FROM user_grid_state WHERE user_id=? AND stock_code=?",
                (user_id, stock_code),
            ).fetchone()
            buy_count = state["consecutive_buy_count"] if state else 0
            sell_count = state["consecutive_sell_count"] if state else 0
            if direction == "BUY":
                buy_count = buy_count + levels
                sell_count = 0
            else:
                sell_count = sell_count + levels
                buy_count = 0
            stopped = buy_count >= 5 or sell_count >= 5
            reason = "连续同方向成交达到 5 档" if stopped else None
            status = "STOPPED" if stopped else "ACTIVE"
            stamp = utcnow()
            conn.execute(
                """
                INSERT INTO grid_fill_log(user_id,stock_code,direction,levels,reported_at)
                VALUES(?,?,?,?,?)
                """,
                (user_id, stock_code, direction, levels, stamp),
            )
            conn.execute(
                """
                INSERT INTO user_grid_state(
                    user_id,stock_code,status,consecutive_buy_count,
                    consecutive_sell_count,last_fill_direction,last_fill_at,stop_reason,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,stock_code) DO UPDATE SET
                    status=excluded.status,
                    consecutive_buy_count=excluded.consecutive_buy_count,
                    consecutive_sell_count=excluded.consecutive_sell_count,
                    last_fill_direction=excluded.last_fill_direction,
                    last_fill_at=excluded.last_fill_at,
                    stop_reason=excluded.stop_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    stock_code,
                    status,
                    buy_count,
                    sell_count,
                    direction,
                    stamp,
                    reason,
                    stamp,
                ),
            )
        return {
            "status": status,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "stop_reason": reason,
        }
    def undo_last_fill(self, user_id: str) -> dict:
        with self.db.transaction() as conn:
            fill = conn.execute(
                """
                SELECT * FROM grid_fill_log
                WHERE user_id=? AND reverted_at IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if not fill:
                raise ValueError("没有可撤销的网格成交记录")
            state = conn.execute(
                "SELECT * FROM user_grid_state WHERE user_id=? AND stock_code=?",
                (user_id, fill["stock_code"]),
            ).fetchone()
            buy_count = state["consecutive_buy_count"] if state else 0
            sell_count = state["consecutive_sell_count"] if state else 0
            if fill["direction"] == "BUY":
                buy_count = max(0, buy_count - fill["levels"])
            else:
                sell_count = max(0, sell_count - fill["levels"])
            reason = state["stop_reason"] if state else None
            status = state["status"] if state else "ACTIVE"
            if reason == "连续同方向成交达到 5 档" and buy_count < 5 and sell_count < 5:
                reason = None
                status = "ACTIVE"
            stamp = utcnow()
            conn.execute(
                "UPDATE grid_fill_log SET reverted_at=? WHERE id=?",
                (stamp, fill["id"]),
            )
            conn.execute(
                """
                UPDATE user_grid_state SET consecutive_buy_count=?,
                    consecutive_sell_count=?,status=?,stop_reason=?,updated_at=?
                WHERE user_id=? AND stock_code=?
                """,
                (
                    buy_count, sell_count, status, reason, stamp,
                    user_id, fill["stock_code"],
                ),
            )
        return {
            "stock_code": fill["stock_code"],
            "direction": fill["direction"],
            "levels": fill["levels"],
        }
