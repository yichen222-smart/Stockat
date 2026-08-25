from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence


MIGRATION_001 = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    wechat_user_id TEXT UNIQUE NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled', 'deleted')),
    morning_report_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS user_delivery_routes (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    platform TEXT NOT NULL DEFAULT 'weixin',
    gateway_home TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code_hash TEXT PRIMARY KEY,
    created_by TEXT NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by TEXT REFERENCES users(id),
    status TEXT NOT NULL CHECK(status IN ('active', 'used', 'expired', 'revoked'))
);

CREATE TABLE IF NOT EXISTS user_watch_sectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    sector_name TEXT NOT NULL,
    keywords_json TEXT,
    exclude_keywords_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, sector_name)
);

CREATE TABLE IF NOT EXISTS user_watch_stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    news_enabled INTEGER NOT NULL DEFAULT 1,
    grid_enabled INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, stock_code)
);

CREATE TABLE IF NOT EXISTS user_grid_state (
    user_id TEXT NOT NULL REFERENCES users(id),
    stock_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STOPPED')),
    consecutive_buy_count INTEGER NOT NULL DEFAULT 0,
    consecutive_sell_count INTEGER NOT NULL DEFAULT 0,
    last_fill_direction TEXT CHECK(last_fill_direction IN ('BUY', 'SELL') OR last_fill_direction IS NULL),
    last_fill_at TEXT,
    stop_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, stock_code)
);

CREATE TABLE IF NOT EXISTS grid_fill_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    stock_code TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY', 'SELL')),
    levels INTEGER NOT NULL DEFAULT 1 CHECK(levels > 0),
    reported_at TEXT NOT NULL,
    reverted_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS stock_price_cache (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    amount REAL,
    adjustment TEXT NOT NULL DEFAULT 'qfq',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(stock_code, trade_date, adjustment)
);

CREATE TABLE IF NOT EXISTS grid_parameter_cache (
    stock_code TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    reference_price REAL,
    buy_grid_pct REAL,
    sell_grid_pct REAL,
    first_buy_price REAL,
    first_sell_price REAL,
    normal_lower REAL,
    normal_upper REAL,
    extreme_lower REAL,
    extreme_upper REAL,
    model_version TEXT,
    data_until TEXT,
    calculated_at TEXT,
    valid_from TEXT,
    score REAL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    url TEXT,
    canonical_url TEXT,
    summary TEXT,
    raw_text_path TEXT,
    category TEXT,
    related_sectors_json TEXT,
    related_stocks_json TEXT,
    is_official INTEGER NOT NULL DEFAULT 0,
    source_weight REAL NOT NULL DEFAULT 0.5,
    fetched_at TEXT NOT NULL,
    event_key TEXT,
    UNIQUE(canonical_url)
);

CREATE TABLE IF NOT EXISTS news_item_scopes (
    news_item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('sector', 'stock')),
    scope_key TEXT NOT NULL,
    match_method TEXT NOT NULL,
    relevance REAL NOT NULL DEFAULT 1.0,
    relation_type TEXT NOT NULL DEFAULT 'direct',
    related_entity TEXT,
    relation_reason TEXT,
    impact_path TEXT,
    query_run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(news_item_id, scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS user_reports (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    report_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'generated', 'sent', 'failed')),
    content TEXT,
    generated_at TEXT,
    sent_at TEXT,
    send_target TEXT,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, report_date)
);

CREATE TABLE IF NOT EXISTS user_report_parts (
    user_id TEXT NOT NULL REFERENCES users(id),
    report_date TEXT NOT NULL,
    part_index INTEGER NOT NULL CHECK(part_index > 0),
    part_count INTEGER NOT NULL CHECK(part_count > 0),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'failed')),
    sent_at TEXT,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, report_date, part_index)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    response_ms INTEGER,
    error_summary TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS command_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    wechat_user_id TEXT NOT NULL,
    command_name TEXT NOT NULL,
    success INTEGER NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_sector_user ON user_watch_sectors(user_id, enabled);
CREATE INDEX IF NOT EXISTS idx_stock_user ON user_watch_stocks(user_id, enabled);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(published_at);
CREATE INDEX IF NOT EXISTS idx_news_event ON news_items(event_key);
CREATE INDEX IF NOT EXISTS idx_news_scope
ON news_item_scopes(scope_type, scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_report_status ON user_reports(report_date, status);
CREATE INDEX IF NOT EXISTS idx_report_part_status
ON user_report_parts(report_date, status);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.transaction() as conn:
            conn.executescript(MIGRATION_001)
            scope_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(news_item_scopes)")
            }
            additions = {
                "relation_type": "TEXT NOT NULL DEFAULT 'direct'",
                "related_entity": "TEXT",
                "relation_reason": "TEXT",
                "impact_path": "TEXT",
            }
            for column, definition in additions.items():
                if column not in scope_columns:
                    conn.execute(
                        f"ALTER TABLE news_item_scopes ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with contextlib.closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with contextlib.closing(self.connect()) as conn:
            return list(conn.execute(sql, params).fetchall())
