from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import yaml

from app.db import Database
from app.grid.engine import TradingCosts, search_parameters, should_replace
from app.indicators.talib_adapter import add_indicators
from app.market_data.akshare_adapter import AkshareAdapter, MarketDataError
from app.messaging.hermes_weixin import send_text, summarize_delivery_error
from app.news.analyzer import NewsAnalyzer
from app.news.pipeline import NewsItem, NewsPipeline, normalized_title
from app.news.profiles import (
    RELATION_LABELS,
    StockProfile,
    fallback_stock_profile,
    load_stock_profiles,
)
from app.reports.builder import build_report, split_wechat
from app.settings import Settings
from app.users.service import utcnow


WEIXIN_PART_INTERVAL_SECONDS = 32.0
WEIXIN_INTERACTION_REQUIRED_MARKER = "recipient interaction required"
WEIXIN_REPLY_REMINDER = (
    "报告老大，小的已播报完毕！请回复“收到”，"
    "不然小的明天就无法继续给老大发送盘前消息啦！(｡•́︿•̀｡)"
)
NON_TRADING_DAY_REMINDER = (
    "老大老大！今天股市不开门！但请你回我一句“收到”，"
    "不然之后我就要翘班啦！TwT"
)


class DailyRun:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.market = AkshareAdapter()

    def execute(
        self,
        *,
        force: bool = False,
        send: bool = True,
        scheduled: bool = False,
        only_user_id: str | None = None,
        as_of: datetime | None = None,
    ) -> dict:
        self.db.migrate()
        timezone = ZoneInfo(self.settings.timezone)
        now = as_of or datetime.now(timezone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone)
        else:
            now = now.astimezone(timezone)
        today = now.date()
        if scheduled and not force:
            cutoff = now.replace(
                hour=self.settings.late_send_cutoff_hour,
                minute=self.settings.late_send_cutoff_minute,
                second=0,
                microsecond=0,
            )
            if now >= cutoff:
                return {
                    "status": "skipped",
                    "reason": "missed_morning_window",
                    "cutoff": cutoff.strftime("%H:%M"),
                }

        is_trading_day, calendar_source = self._is_trading_day(today)
        if calendar_source == "unavailable" and not force:
            return {
                "status": "error",
                "reason": "trading_calendar_unavailable",
                "calendar": calendar_source,
            }
        is_non_trading_reminder = not is_trading_day and not force

        if only_user_id:
            user_rows = self.db.fetchall(
                "SELECT u.*,r.gateway_home FROM users u LEFT JOIN user_delivery_routes r ON r.user_id=u.id WHERE u.id=? AND u.status='active'",
                (only_user_id,),
            )
        elif scheduled:
            user_rows = self.db.fetchall(
                """
                SELECT u.*,r.gateway_home
                FROM users u
                LEFT JOIN user_delivery_routes r ON r.user_id=u.id
                WHERE u.status='active' AND u.morning_report_enabled=1
                  AND NOT EXISTS (
                      SELECT 1 FROM user_reports ur
                      WHERE ur.user_id=u.id AND ur.report_date=? AND ur.status='sent'
                  )
                ORDER BY u.created_at
                """,
                (today.isoformat(),),
            )
        else:
            user_rows = self.db.fetchall(
                "SELECT u.*,r.gateway_home FROM users u LEFT JOIN user_delivery_routes r ON r.user_id=u.id WHERE u.status='active' AND u.morning_report_enabled=1 ORDER BY u.created_at"
            )
        users = [dict(row) for row in user_rows]
        if not users:
            result = {
                "status": "ok",
                "users": 0,
                "sent": 0,
                "calendar": calendar_source,
            }
            if is_non_trading_reminder:
                result["report_type"] = "non_trading_day_reminder"
            return result

        if is_non_trading_reminder:
            if scheduled and send:
                self._wait_until_report_time()
            return self._send_non_trading_day_reminders(
                users,
                today.isoformat(),
                send=send,
                scheduled=scheduled,
                calendar_source=calendar_source,
            )

        common = self._collect_common_material(today, now)
        grid_params = self._calculate_unique_grids()
        candidates = self._candidate_news(today, now)
        if scheduled and send:
            self._wait_until_report_time()
        sent_count = 0
        failures = 0
        for user in users:  # Serial by design for 2C2G.
            try:
                existing = self.db.fetchone(
                    "SELECT id,status,content FROM user_reports WHERE user_id=? AND report_date=?",
                    (user["id"], today.isoformat()),
                )
                if (
                    scheduled
                    and existing
                    and existing["status"] in {"generated", "failed"}
                    and existing["content"]
                ):
                    content = existing["content"]
                    self._ensure_report_parts(user["id"], today.isoformat(), content)
                else:
                    content = self._build_user_report(
                        user, candidates, grid_params, common, report_now=now
                    )
                    self._store_generated_report(
                        user["id"],
                        user["wechat_user_id"],
                        today.isoformat(),
                        content,
                    )
                if send:
                    gateway_home = self._require_gateway_home(user)
                    self._send_report_parts(
                        user["id"],
                        today.isoformat(),
                        user["wechat_user_id"],
                        content,
                        gateway_home,
                    )
                    with self.db.transaction() as conn:
                        conn.execute(
                            """
                            UPDATE user_reports SET status='sent',sent_at=?,error_message=NULL
                            WHERE user_id=? AND report_date=?
                            """,
                            (utcnow(), user["id"], today.isoformat()),
                        )
                    sent_count += 1
            except Exception as exc:
                failures += 1
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE user_reports SET status='failed',error_message=?,retry_count=retry_count+1
                        WHERE user_id=? AND report_date=?
                        """,
                        (summarize_delivery_error(exc), user["id"], today.isoformat()),
                    )
        return {
            "status": "ok" if not failures else "partial",
            "users": len(users),
            "sent": sent_count,
            "failed": failures,
            "news_candidates": len(candidates),
            "grid_stocks": len(grid_params),
            "calendar": calendar_source,
        }

    def _send_non_trading_day_reminders(
        self,
        users: list[dict],
        report_date: str,
        *,
        send: bool,
        scheduled: bool,
        calendar_source: str,
    ) -> dict:
        sent_count = 0
        failures = 0
        for user in users:
            try:
                existing = self.db.fetchone(
                    "SELECT id,status,content FROM user_reports WHERE user_id=? AND report_date=?",
                    (user["id"], report_date),
                )
                if (
                    scheduled
                    and existing
                    and existing["status"] in {"generated", "failed"}
                    and existing["content"] == NON_TRADING_DAY_REMINDER
                ):
                    content = existing["content"]
                    self._ensure_report_parts(user["id"], report_date, content)
                else:
                    content = NON_TRADING_DAY_REMINDER
                    self._store_generated_report(
                        user["id"], user["wechat_user_id"], report_date, content
                    )
                if send:
                    gateway_home = self._require_gateway_home(user)
                    self._send_report_parts(
                        user["id"],
                        report_date,
                        user["wechat_user_id"],
                        content,
                        gateway_home,
                    )
                    with self.db.transaction() as conn:
                        conn.execute(
                            """
                            UPDATE user_reports SET status='sent',sent_at=?,error_message=NULL
                            WHERE user_id=? AND report_date=?
                            """,
                            (utcnow(), user["id"], report_date),
                        )
                    sent_count += 1
            except Exception as exc:
                failures += 1
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE user_reports SET status='failed',error_message=?,retry_count=retry_count+1
                        WHERE user_id=? AND report_date=?
                        """,
                        (summarize_delivery_error(exc), user["id"], report_date),
                    )
        return {
            "status": "ok" if not failures else "partial",
            "report_type": "non_trading_day_reminder",
            "users": len(users),
            "sent": sent_count,
            "failed": failures,
            "calendar": calendar_source,
        }

    def _store_generated_report(
        self, user_id: str, send_target: str, report_date: str, content: str
    ) -> None:
        report_id = str(uuid.uuid4())
        parts = self._delivery_parts(content)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_reports(
                    id,user_id,report_date,status,content,generated_at,send_target
                ) VALUES(?,?,?,'generated',?,?,?)
                ON CONFLICT(user_id,report_date) DO UPDATE SET
                    status='generated',content=excluded.content,
                    generated_at=excluded.generated_at,send_target=excluded.send_target,
                    sent_at=NULL,error_message=NULL,retry_count=0
                """,
                (report_id, user_id, report_date, content, utcnow(), send_target),
            )
            conn.execute(
                "DELETE FROM user_report_parts WHERE user_id=? AND report_date=?",
                (user_id, report_date),
            )
            for index, part in enumerate(parts, 1):
                conn.execute(
                    """
                    INSERT INTO user_report_parts(
                        user_id,report_date,part_index,part_count,content_hash,status
                    ) VALUES(?,?,?,?,?,'pending')
                    """,
                    (
                        user_id,
                        report_date,
                        index,
                        len(parts),
                        hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    ),
                )

    def _ensure_report_parts(self, user_id: str, report_date: str, content: str) -> None:
        parts = self._delivery_parts(content)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE user_report_parts SET part_count=? WHERE user_id=? AND report_date=?",
                (len(parts), user_id, report_date),
            )
            for index, part in enumerate(parts, 1):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_report_parts(
                        user_id,report_date,part_index,part_count,content_hash,status
                    ) VALUES(?,?,?,?,?,'pending')
                    """,
                    (
                        user_id,
                        report_date,
                        index,
                        len(parts),
                        hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    ),
                )

    @staticmethod
    def _delivery_parts(content: str) -> list[str]:
        """Return one closure reminder or the trading report plus its reminder."""
        if content == NON_TRADING_DAY_REMINDER:
            return [content]
        return [*split_wechat(content), WEIXIN_REPLY_REMINDER]

    def _require_gateway_home(self, user: dict) -> str:
        raw_home = str(user.get("gateway_home") or "").strip()
        if not raw_home:
            raise RuntimeError("用户缺少独立微信发送路由")
        gateway_home = os.path.realpath(raw_home)
        delivery_root = os.path.realpath(str(self.settings.root / "data"))
        if os.path.commonpath([gateway_home, delivery_root]) != delivery_root:
            raise RuntimeError("用户微信发送路由不在允许目录")
        if not os.path.basename(gateway_home).startswith("hermes"):
            raise RuntimeError("用户微信发送路由名称无效")
        return gateway_home

    def _send_report_parts(
        self,
        user_id: str,
        report_date: str,
        chat_id: str,
        content: str,
        gateway_home: str,
    ) -> None:
        parts = self._delivery_parts(content)
        self._ensure_report_parts(user_id, report_date, content)
        for index, part in enumerate(parts, 1):
            state = self.db.fetchone(
                """
                SELECT status,content_hash FROM user_report_parts
                WHERE user_id=? AND report_date=? AND part_index=?
                """,
                (user_id, report_date, index),
            )
            part_hash = hashlib.sha256(part.encode("utf-8")).hexdigest()
            if state and state["status"] == "sent" and state["content_hash"] == part_hash:
                continue
            last_error = None
            for attempt in range(3):
                try:
                    send_text(chat_id, part, gateway_home=gateway_home)
                    with self.db.transaction() as conn:
                        conn.execute(
                            """
                            UPDATE user_report_parts
                            SET status='sent',sent_at=?,error_message=NULL,content_hash=?
                            WHERE user_id=? AND report_date=? AND part_index=?
                            """,
                            (utcnow(), part_hash, user_id, report_date, index),
                        )
                    if index < len(parts):
                        time.sleep(WEIXIN_PART_INTERVAL_SECONDS)
                    break
                except Exception as exc:
                    last_error = exc
                    with self.db.transaction() as conn:
                        conn.execute(
                            """
                            UPDATE user_report_parts
                            SET status='failed',error_message=?,retry_count=retry_count+1
                            WHERE user_id=? AND report_date=? AND part_index=?
                            """,
                            (summarize_delivery_error(exc), user_id, report_date, index),
                        )
                    if WEIXIN_INTERACTION_REQUIRED_MARKER in str(exc).lower():
                        raise RuntimeError(
                            "微信主动发送上下文已失效，需要收件人先回复任意消息"
                        ) from exc
                    if attempt < 2:
                        time.sleep(self._delivery_retry_delay(exc, attempt))
            else:
                raise RuntimeError(f"微信第{index}段发送重试耗尽") from last_error

    @staticmethod
    def _delivery_retry_delay(exc: Exception, attempt: int) -> float:
        match = re.search(
            r"cooldown active for\s+([0-9]+(?:\.[0-9]+)?)s",
            str(exc),
            flags=re.IGNORECASE,
        )
        if match:
            return max(float(match.group(1)) + 2.0, WEIXIN_PART_INTERVAL_SECONDS)
        return float(2**attempt)

    def _wait_until_report_time(self) -> None:
        timezone = ZoneInfo(self.settings.timezone)
        now = datetime.now(timezone)
        target = now.replace(
            hour=self.settings.report_hour,
            minute=self.settings.report_minute,
            second=0,
            microsecond=0,
        )
        delay = (target - now).total_seconds()
        if delay > 0:
            time.sleep(delay)

    def _is_trading_day(self, target: date) -> tuple[bool, str]:
        cache = self.settings.root / "data" / "market_cache" / "trading_calendar.json"
        try:
            days = self.market.trading_calendar()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(sorted(day.isoformat() for day in days)), encoding="utf-8"
            )
            return target in days, "akshare"
        except MarketDataError:
            if cache.exists():
                try:
                    days = set(json.loads(cache.read_text(encoding="utf-8")))
                    return target.isoformat() in days, "cache"
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            return False, "unavailable"

    def _collect_common_material(self, today: date, as_of: datetime) -> dict:
        config_path = self.settings.root / "config" / "news_sources.yaml"
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        pipeline = NewsPipeline(self.db)
        window_start, window_end = self._news_window(today, as_of)
        self._reset_transient_stock_search_scopes(window_start, window_end)
        sectors = self._active_sector_definitions()
        stocks = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT stock_code,MAX(stock_name) AS stock_name
                FROM user_watch_stocks
                WHERE enabled=1 AND news_enabled=1
                GROUP BY stock_code ORDER BY stock_code
                """
            )
        ]
        profile_path = self.settings.root / "config" / "stock_influence_profiles.yaml"
        profiles, profile_metadata = (
            load_stock_profiles(profile_path) if profile_path.exists() else ({}, {})
        )
        active_profiles = {
            str(stock["stock_code"]): profiles.get(str(stock["stock_code"]))
            or fallback_stock_profile(str(stock["stock_code"]), str(stock["stock_name"]))
            for stock in stocks
        }

        fetched: list[NewsItem] = []
        for source in raw.get("sources", []):
            if source.get("enabled", True):
                fetched.extend(pipeline.fetch(source))
        self._tag_sector_items(fetched, sectors)

        stock_status: dict[str, str] = {}
        stock_failures: set[str] = set()
        for stock in stocks:
            code = str(stock["stock_code"])
            profile = active_profiles[code]
            started = time.monotonic()
            try:
                rows = self.market.stock_news(code)
                pipeline.record_health(
                    f"akshare_stock_news:{code}",
                    True,
                    int((time.monotonic() - started) * 1000),
                )
                for row in rows:
                    if not profile.qualified_direct_title(
                        str(row.get("title") or ""), str(row.get("summary") or "")
                    ):
                        continue
                    fetched.append(
                        NewsItem(
                            title=row["title"],
                            published_at=row["published_at"],
                            source=row["source"],
                            source_type="akshare",
                            url=row["url"],
                            summary=row["summary"],
                            category="stock",
                            related_stocks=[code],
                            stock_relations={
                                code: {
                                    "relation_type": "direct",
                                    "related_entity": profile.name,
                                    "relation_reason": "AKShare个股新闻接口返回的实质事件",
                                    "impact_path": "公司经营",
                                    "relevance": 1.0,
                                    "match_method": "stock_api",
                                }
                            },
                            source_weight=0.65,
                        )
                    )
            except MarketDataError as exc:
                stock_failures.add(code)
                pipeline.record_health(
                    f"akshare_stock_news:{code}",
                    False,
                    int((time.monotonic() - started) * 1000),
                    type(exc).__name__,
                )

        self._tag_stock_influence_items(fetched, active_profiles)

        def in_window(item: NewsItem) -> bool:
            published = item.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            return window_start <= published.astimezone(UTC) <= window_end

        global_count = sum(
            in_window(item) and item.category != "stock" for item in fetched
        )
        if global_count < 5:
            rows, _ = self._web_search_scopes(
                "global",
                [{"key": "global", "name": "A股盘前总体重要新闻", "keywords": []}],
                window_start,
                window_end,
                as_of,
                pipeline,
            )
            fetched.extend(rows)

        sector_status: dict[str, str] = {}
        missing_sectors: list[dict] = []
        for name, definition in sectors.items():
            matches = [
                item
                for item in fetched
                if name in item.related_sectors and in_window(item)
            ]
            if matches:
                sector_status[name] = "ok"
            else:
                missing_sectors.append(
                    {
                        "key": name,
                        "name": name,
                        "keywords": definition["keywords"],
                    }
                )
        if missing_sectors:
            rows, statuses = self._web_search_scopes(
                "sector",
                missing_sectors,
                window_start,
                window_end,
                as_of,
                pipeline,
            )
            fetched.extend(rows)
            sector_status.update(statuses)

        # Global/sector directed-search results are also free candidates for stocks.
        self._tag_stock_influence_items(fetched, active_profiles)

        missing_stocks: list[dict] = []
        for stock in stocks:
            code = str(stock["stock_code"])
            matches = [
                item
                for item in fetched
                if code in item.related_stocks and in_window(item)
            ]
            if matches:
                stock_status[code] = "ok"
            else:
                missing_stocks.append(
                    {
                        "key": code,
                        "name": f"{code} {stock['stock_name']}",
                        "keywords": [code, str(stock["stock_name"])],
                        "profile": active_profiles[code].search_payload(),
                    }
                )
        if missing_stocks:
            rows, statuses = self._web_search_scopes(
                "stock",
                missing_stocks,
                window_start,
                window_end,
                as_of,
                pipeline,
            )
            fetched.extend(rows)
            for code, status in statuses.items():
                if status == "source_error" and code not in stock_failures:
                    status = "no_news"
                stock_status[code] = status

        inserted = pipeline.persist(fetched)
        return {
            "inserted": inserted,
            "fetched": len(fetched),
            "sector_status": sector_status,
            "stock_status": stock_status,
            "stock_profile_generated_at": str(profile_metadata.get("generated_at") or ""),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        }

    def _reset_transient_stock_search_scopes(
        self, window_start: datetime, window_end: datetime
    ) -> None:
        """A rerun must not inherit stock API/search labels from an older rule set."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM news_item_scopes
                WHERE scope_type='stock'
                  AND match_method IN ('directed_search','stock_api')
                  AND news_item_id IN (
                      SELECT id FROM news_items
                      WHERE published_at>=? AND published_at<=?
                  )
                """,
                (window_start.isoformat(), window_end.isoformat()),
            )

    @staticmethod
    def _load_json_terms(value: object) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []

    def _active_sector_definitions(self) -> dict[str, dict[str, list[str]]]:
        raw = yaml.safe_load(
            (self.settings.root / "config" / "sector_sources.yaml").read_text(
                encoding="utf-8"
            )
        ) or {}
        configured = raw.get("sectors") or {}
        definitions: dict[str, dict[str, list[str]]] = {}
        rows = self.db.fetchall(
            """
            SELECT sector_name,keywords_json,exclude_keywords_json
            FROM user_watch_sectors WHERE enabled=1 ORDER BY sector_name
            """
        )
        for row in rows:
            name = str(row["sector_name"]).strip()
            cfg = configured.get(name) or configured.get("default") or {}
            entry = definitions.setdefault(name, {"keywords": [name], "exclude_keywords": []})
            entry["keywords"] = sorted(
                set(entry["keywords"])
                | set(str(item) for item in cfg.get("keywords", []))
                | set(self._load_json_terms(row["keywords_json"]))
            )
            entry["exclude_keywords"] = sorted(
                set(entry["exclude_keywords"])
                | set(str(item) for item in cfg.get("exclude_keywords", []))
                | set(self._load_json_terms(row["exclude_keywords_json"]))
            )
        return definitions

    @staticmethod
    def _tag_sector_items(
        items: list[NewsItem], definitions: dict[str, dict[str, list[str]]]
    ) -> None:
        for item in items:
            text = f"{item.title} {item.summary}".lower()
            for sector, definition in definitions.items():
                keywords = [term.lower() for term in definition["keywords"] if term]
                excludes = [term.lower() for term in definition["exclude_keywords"] if term]
                if keywords and any(term in text for term in keywords) and not any(
                    term in text for term in excludes
                ):
                    item.related_sectors.append(sector)
            item.related_sectors = sorted(set(item.related_sectors))

    @staticmethod
    def _tag_stock_influence_items(
        items: list[NewsItem], profiles: dict[str, StockProfile]
    ) -> None:
        """Reuse the collected pool before spending a per-stock Web Search call."""
        for item in items:
            text = f"{item.title} {item.summary}"
            for stock_code, profile in profiles.items():
                if stock_code in item.stock_relations:
                    continue
                relation = profile.classify(text)
                if not relation:
                    continue
                if (
                    relation.get("relation_type") in {"direct", "competitor"}
                    and str(relation.get("related_entity") or "").casefold()
                    not in item.title.casefold()
                ):
                    continue
                item.related_stocks.append(stock_code)
                item.stock_relations[stock_code] = {
                    **relation,
                    "match_method": "profile_keyword",
                }
            item.related_stocks = sorted(set(item.related_stocks))

    def _web_search_scopes(
        self,
        scope_type: str,
        scopes: list[dict],
        window_start: datetime,
        window_end: datetime,
        as_of: datetime,
        pipeline: NewsPipeline,
    ) -> tuple[list[NewsItem], dict[str, str]]:
        output: list[NewsItem] = []
        statuses: dict[str, str] = {}
        timezone = ZoneInfo(self.settings.timezone)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.settings.hermes_home)
        env.setdefault("HOME", str(self.settings.hermes_home / "home"))
        for offset in range(0, len(scopes), 4):
            chunk = scopes[offset : offset + 4]
            if scope_type == "stock":
                prompt = (
                    "使用 Web Search 对下面每只股票分别进行一次中等规模新闻检索。"
                    "一次检索同时覆盖四维：direct公司公告/业绩/订单/管理层/产品；"
                    "competitor核心竞对的价格/产能/重大订单/技术突破/事故/处罚；"
                    "industry细分行业的政策/供需/行业价格/重大技术变化；"
                    "supply_chain仅保留足以明显改变成本或需求的上下游事件。"
                    f"检索窗口为北京时间{window_start.astimezone(timezone):%Y-%m-%d %H:%M}至"
                    f"{window_end.astimezone(timezone):%Y-%m-%d %H:%M}。"
                    "每只股票四维合计最多返回6条，不为凑数返回日常活动、普通行情稿或泛泛评论；"
                    "间接新闻必须能用画像中的实体和重大事件词解释影响链条。"
                    "优先交易所、政府、公司公告和可靠财经媒体，不得编造，不得给买卖建议。"
                    "仅输出<JSON>与</JSON>之间的JSON数组。每项字段：scope_key、relation_type、"
                    "related_entity、relation_reason、title、summary、source、url、published_at"
                    "（北京时间ISO格式）。对象及四维画像列表："
                    + json.dumps(chunk, ensure_ascii=False)
                )
            else:
                prompt = (
                    "使用 Web Search 对下面每个对象分别进行定向新闻检索；不能拿总体新闻代替。"
                    f"检索窗口为北京时间{window_start.astimezone(timezone):%Y-%m-%d %H:%M}至"
                    f"{window_end.astimezone(timezone):%Y-%m-%d %H:%M}。"
                    "每个对象返回1至3条重要事实新闻；确实没有则该对象返回空数组。"
                    "优先交易所、政府、公司公告和可靠财经媒体，不得编造，不得给买卖建议。"
                    "仅输出<JSON>与</JSON>之间的JSON数组。每项字段：scope_key、title、summary、"
                    "source、url、published_at（北京时间ISO格式）。对象列表："
                    + json.dumps(chunk, ensure_ascii=False)
                )
            started = time.monotonic()
            try:
                result = subprocess.run(
                    [str(self.settings.hermes_bin), "-z", prompt, "-t", "web"],
                    cwd=self.settings.root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=240,
                    check=True,
                )
                match = re.search(r"<JSON>\s*(\[.*?\])\s*</JSON>", result.stdout, re.S)
                rows = json.loads(match.group(1)) if match else []
                if not isinstance(rows, list):
                    rows = []
                elapsed = int((time.monotonic() - started) * 1000)
                allowed = {str(scope["key"]): scope for scope in chunk}
                counts = {key: 0 for key in allowed}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("scope_key") or "")
                    limit = 6 if scope_type == "stock" else 3
                    if key not in allowed or not row.get("title") or counts[key] >= limit:
                        continue
                    published = self._parse_search_time(row.get("published_at"), as_of)
                    if not window_start <= published <= window_end:
                        continue
                    stock_relations: dict[str, dict[str, object]] = {}
                    if scope_type == "stock":
                        payload = allowed[key].get("profile") or {}
                        profile = StockProfile(
                            code=key,
                            name=str(payload.get("stock_name") or allowed[key]["name"]),
                            generated_at=str(payload.get("profile_generated_at") or ""),
                            raw=payload,
                        )
                        relation_text = " ".join(
                            str(row.get(field) or "")
                            for field in ("title", "summary")
                        )
                        relation = profile.classify(relation_text)
                        if not relation:
                            continue
                        if (
                            relation.get("relation_type") in {"direct", "competitor"}
                            and str(relation.get("related_entity") or "").casefold()
                            not in str(row.get("title") or "").casefold()
                        ):
                            continue
                        stock_relations[key] = {
                            **relation,
                            "match_method": "directed_search",
                        }
                    item = NewsItem(
                        title=str(row["title"]).strip(),
                        published_at=published,
                        source=str(row.get("source") or "Web Search"),
                        source_type="web_search",
                        url=str(row.get("url") or ""),
                        summary=str(row.get("summary") or "")[:2000],
                        category="market" if scope_type == "global" else scope_type,
                        related_sectors=[key] if scope_type == "sector" else [],
                        related_stocks=[key] if scope_type == "stock" else [],
                        stock_relations=stock_relations,
                        source_weight=0.55,
                        fetched_at=as_of.astimezone(UTC),
                    )
                    output.append(item)
                    counts[key] += 1
                for key, count in counts.items():
                    statuses[key] = "ok" if count else "no_news"
                    pipeline.record_health(
                        f"web_search:{scope_type}:{key}", True, elapsed
                    )
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                for scope in chunk:
                    key = str(scope["key"])
                    statuses[key] = "source_error"
                    pipeline.record_health(
                        f"web_search:{scope_type}:{key}",
                        False,
                        elapsed,
                        type(exc).__name__,
                    )
        return output, statuses

    def _parse_search_time(self, value: object, as_of: datetime) -> datetime:
        timezone = ZoneInfo(self.settings.timezone)
        if not value:
            return as_of.astimezone(UTC)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
            return parsed.astimezone(UTC)
        except ValueError:
            return as_of.astimezone(UTC)
    def _calculate_unique_grids(self) -> dict[str, dict]:
        rows = self.db.fetchall(
            """
            SELECT DISTINCT stock_code FROM user_watch_stocks
            WHERE enabled=1 AND grid_enabled=1 ORDER BY stock_code
            """
        )
        search_cfg = yaml.safe_load(
            (self.settings.root / "config" / "grid_search.yaml").read_text(encoding="utf-8")
        )
        cost_cfg = yaml.safe_load(
            (self.settings.root / "config" / "trading_costs.yaml").read_text(encoding="utf-8")
        )
        costs = TradingCosts(**cost_cfg)
        output: dict[str, dict] = {}
        for row in rows:
            code = row["stock_code"]
            try:
                frame = self.market.daily(code)
                calculated = add_indicators(frame)
                params = search_parameters(
                    calculated,
                    costs,
                    search_cfg["buy_grid_pct"],
                    search_cfg["sell_grid_pct"],
                ).to_dict()
                existing = self.db.fetchone(
                    """
                    SELECT score,valid_from,details_json
                    FROM grid_parameter_cache WHERE stock_code=?
                    """,
                    (code,),
                )
                replace_cache = True
                if existing:
                    try:
                        active_from = date.fromisoformat(existing["valid_from"])
                        active_days = int(
                            (calculated["date"].dt.date >= active_from).sum()
                        )
                        replace_cache = should_replace(
                            float(existing["score"]),
                            float(params["score"]),
                            active_days,
                            min_hold_days=int(search_cfg["minimum_active_trading_days"]),
                            improvement_threshold=float(search_cfg["improvement_threshold"]),
                        )
                        if not replace_cache:
                            params = json.loads(existing["details_json"])
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        replace_cache = True
                latest_row = calculated.iloc[-1]
                params = {
                    **params,
                    "latest_close": round(float(latest_row["close"]), 3),
                    "latest_close_date": str(latest_row["date"].date()),
                    "adjustment": "qfq",
                }
                output[code] = params
                recent = calculated["close"].tail(2)
                stop_reason = None
                latest = float(recent.iloc[-1])
                if latest < params["extreme_lower"] or latest > params["extreme_upper"]:
                    stop_reason = "单日价格越过极限停止价"
                elif len(recent) == 2 and (
                    (recent < params["normal_lower"]).all()
                    or (recent > params["normal_upper"]).all()
                ):
                    stop_reason = "连续两日价格越过正常范围"
                self._apply_market_risk(code, stop_reason)
                if not replace_cache:
                    continue
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO grid_parameter_cache(
                            stock_code,status,reference_price,buy_grid_pct,sell_grid_pct,
                            first_buy_price,first_sell_price,normal_lower,normal_upper,
                            extreme_lower,extreme_upper,model_version,data_until,
                            calculated_at,valid_from,score,details_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(stock_code) DO UPDATE SET
                            status=excluded.status,reference_price=excluded.reference_price,
                            buy_grid_pct=excluded.buy_grid_pct,sell_grid_pct=excluded.sell_grid_pct,
                            first_buy_price=excluded.first_buy_price,
                            first_sell_price=excluded.first_sell_price,
                            normal_lower=excluded.normal_lower,normal_upper=excluded.normal_upper,
                            extreme_lower=excluded.extreme_lower,extreme_upper=excluded.extreme_upper,
                            model_version=excluded.model_version,data_until=excluded.data_until,
                            calculated_at=excluded.calculated_at,score=excluded.score,
                            details_json=excluded.details_json
                        """,
                        (
                            code, "ACTIVE", params["reference_price"], params["buy_grid_pct"],
                            params["sell_grid_pct"], params["first_buy_price"],
                            params["first_sell_price"], params["normal_lower"],
                            params["normal_upper"], params["extreme_lower"],
                            params["extreme_upper"], params["model_version"],
                            params["data_until"], utcnow(), date.today().isoformat(),
                            params["score"], json.dumps(params, ensure_ascii=False),
                        ),
                    )
            except Exception:
                self._apply_market_risk(code, "行情或指标数据异常")
                continue
        return output

    def _apply_market_risk(self, stock_code: str, stop_reason: str | None) -> None:
        watchers = self.db.fetchall(
            """
            SELECT user_id FROM user_watch_stocks
            WHERE stock_code=? AND enabled=1 AND grid_enabled=1
            """,
            (stock_code,),
        )
        with self.db.transaction() as conn:
            for watcher in watchers:
                if stop_reason:
                    conn.execute(
                        """
                        INSERT INTO user_grid_state(
                            user_id,stock_code,status,stop_reason,updated_at
                        ) VALUES(?,?,'STOPPED',?,?)
                        ON CONFLICT(user_id,stock_code) DO UPDATE SET
                            status='STOPPED',stop_reason=excluded.stop_reason,
                            updated_at=excluded.updated_at
                        """,
                        (watcher["user_id"], stock_code, stop_reason, utcnow()),
                    )
                else:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO user_grid_state(
                            user_id,stock_code,status,updated_at
                        ) VALUES(?,?,'ACTIVE',?)
                        """,
                        (watcher["user_id"], stock_code, utcnow()),
                    )
    def _news_window(self, today: date, as_of: datetime) -> tuple[datetime, datetime]:
        timezone = ZoneInfo(self.settings.timezone)
        local_now = as_of.astimezone(timezone)
        planned_end = datetime.combine(today, datetime.min.time(), timezone).replace(
            hour=8, minute=20
        )
        end = min(local_now, planned_end)
        cache = self.settings.root / "data" / "market_cache" / "trading_calendar.json"
        trading_days: list[date] = []
        if cache.exists():
            try:
                trading_days = [
                    date.fromisoformat(value)
                    for value in json.loads(cache.read_text(encoding="utf-8"))
                    if date.fromisoformat(value) < today
                ]
            except (OSError, ValueError, json.JSONDecodeError):
                trading_days = []
        if not trading_days:
            try:
                trading_days = [day for day in self.market.trading_calendar() if day < today]
            except MarketDataError:
                trading_days = []
        previous = max(trading_days) if trading_days else today - timedelta(days=3 if today.weekday() == 0 else 1)
        start = datetime.combine(previous, datetime.min.time(), timezone).replace(
            hour=15
        )
        return start.astimezone(UTC), end.astimezone(UTC)

    def _candidate_news(self, today: date, as_of: datetime) -> list[dict]:
        start, end = self._news_window(today, as_of)
        rows = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM news_items WHERE published_at>=? AND published_at<=?
                ORDER BY is_official DESC,source_weight DESC,published_at DESC LIMIT 500
                """,
                (start.isoformat(), end.isoformat()),
            )
        ]
        scope_meta: dict[str, dict[str, dict[str, object]]] = {}
        for scope in self.db.fetchall(
            """
            SELECT s.news_item_id,s.scope_key,s.match_method,s.relevance,
                   s.relation_type,s.related_entity,s.relation_reason,s.impact_path
            FROM news_item_scopes s
            JOIN news_items n ON n.id=s.news_item_id
            WHERE s.scope_type='stock' AND n.published_at>=? AND n.published_at<=?
            """,
            (start.isoformat(), end.isoformat()),
        ):
            if scope["match_method"] == "profile_keyword":
                continue
            scope_meta.setdefault(str(scope["news_item_id"]), {})[
                str(scope["scope_key"])
            ] = {
                "match_method": scope["match_method"],
                "relevance": float(scope["relevance"] or 0.0),
                "relation_type": scope["relation_type"] or "direct",
                "related_entity": scope["related_entity"],
                "relation_reason": scope["relation_reason"],
                "impact_path": scope["impact_path"],
            }
        profile_path = self.settings.root / "config" / "stock_influence_profiles.yaml"
        configured_profiles, _ = (
            load_stock_profiles(profile_path) if profile_path.exists() else ({}, {})
        )
        active_profiles: dict[str, StockProfile] = {}
        for stock in self.db.fetchall(
            """
            SELECT stock_code,MAX(stock_name) AS stock_name
            FROM user_watch_stocks
            WHERE enabled=1 AND news_enabled=1
            GROUP BY stock_code
            """
        ):
            code = str(stock["stock_code"])
            active_profiles[code] = configured_profiles.get(code) or fallback_stock_profile(
                code, str(stock["stock_name"])
            )
        for row in rows:
            current_meta = scope_meta.setdefault(str(row["id"]), {})
            text = f"{row['title']} {row.get('summary') or ''}"
            for code, profile in active_profiles.items():
                if code in current_meta:
                    continue
                relation = profile.classify(text)
                if relation and not (
                    relation.get("relation_type") in {"direct", "competitor"}
                    and str(relation.get("related_entity") or "").casefold()
                    not in str(row["title"]).casefold()
                ):
                    current_meta[code] = {
                        **relation,
                        "match_method": "profile_keyword",
                    }
            row["stock_scope_meta_json"] = json.dumps(
                current_meta, ensure_ascii=False
            )
        return rows

    def _build_user_report(
        self,
        user: dict,
        candidates: list[dict],
        grid_params: dict[str, dict],
        common: dict,
        *,
        report_now: datetime,
    ) -> str:
        sectors = [
            row["sector_name"]
            for row in self.db.fetchall(
                "SELECT sector_name FROM user_watch_sectors WHERE user_id=? AND enabled=1",
                (user["id"],),
            )
        ]
        stocks = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT stock_code,stock_name,news_enabled,grid_enabled FROM user_watch_stocks
                WHERE user_id=? AND enabled=1 ORDER BY id
                """,
                (user["id"],),
            )
        ]
        selected = NewsAnalyzer().analyze(
            candidates, sectors, [stock for stock in stocks if stock["news_enabled"]]
        )
        by_id = {row["id"]: row for row in candidates}

        timezone = ZoneInfo(self.settings.timezone)
        used_events: list[dict] = []

        def same_event(row: dict, prior: dict) -> bool:
            if row["id"] == prior["id"] or (
                row.get("event_key") and row.get("event_key") == prior.get("event_key")
            ):
                return True
            left = normalized_title(str(row["title"]))
            right = normalized_title(str(prior["title"]))
            return bool(left and right) and SequenceMatcher(None, left, right).ratio() >= 0.82

        def material(
            ids: list[str], label_prefix: str, stock_code: str | None = None
        ) -> list[dict]:
            result = []
            for item_id in ids:
                if item_id not in by_id:
                    continue
                row = by_id[item_id]
                published = datetime.fromisoformat(row["published_at"])
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                entry = {
                    "id": item_id,
                    "title": selected["summaries"].get(item_id) or row["title"],
                    "source": row["source"],
                    "time": published.astimezone(timezone).strftime("%m-%d %H:%M"),
                }
                if stock_code:
                    try:
                        scope_meta = json.loads(
                            str(row.get("stock_scope_meta_json") or "{}")
                        ).get(stock_code, {})
                    except (AttributeError, TypeError, json.JSONDecodeError):
                        scope_meta = {}
                    relation_type = str(scope_meta.get("relation_type") or "direct")
                    entry.update(
                        {
                            "relation_type": relation_type,
                            "relation_label": RELATION_LABELS.get(
                                relation_type, RELATION_LABELS["direct"]
                            ),
                            "relation_reason": scope_meta.get("relation_reason"),
                            "impact_path": scope_meta.get("impact_path"),
                        }
                    )
                duplicate = next(
                    (prior for prior in used_events if same_event(row, prior)), None
                )
                if duplicate:
                    entry["reference"] = duplicate["label"]
                else:
                    used_events.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "event_key": row.get("event_key"),
                            "label": f"{label_prefix}第{len(result) + 1}条",
                        }
                    )
                result.append(entry)
            return result

        top_news = material(selected["top_ids"], "今日要闻")
        sector_news = {
            sector: material(
                selected["sector_ids"].get(sector, []), f"板块“{sector}”"
            )
            for sector in sectors
        }
        stock_news = {
            str(stock["stock_code"]): material(
                selected["stock_ids"].get(str(stock["stock_code"]), []),
                f"股票{stock['stock_code']}",
                str(stock["stock_code"]),
            )
            for stock in stocks
            if stock["news_enabled"]
        }

        grid_rows = []
        for stock in stocks:
            if not stock["grid_enabled"] or stock["stock_code"] not in grid_params:
                continue
            state = self.db.fetchone(
                """
                SELECT status,stop_reason,consecutive_buy_count,consecutive_sell_count
                FROM user_grid_state WHERE user_id=? AND stock_code=?
                """,
                (user["id"], stock["stock_code"]),
            )
            grid_rows.append(
                {
                    **grid_params[stock["stock_code"]],
                    "stock_code": stock["stock_code"],
                    "stock_name": stock["stock_name"],
                    "status": state["status"] if state else "ACTIVE",
                    "stop_reason": state["stop_reason"] if state else None,
                    "buy_count": state["consecutive_buy_count"] if state else 0,
                    "sell_count": state["consecutive_sell_count"] if state else 0,
                }
            )
        data_until = max(
            [row["data_until"] for row in grid_rows if row.get("data_until")]
            or [datetime.now().strftime("%H:%M")]
        )
        return build_report(
            report_now.strftime("%m月%d日"),
            top_news,
            sector_news,
            grid_rows,
            data_until,
            watch_stocks=stocks,
            news_until=report_now.astimezone(timezone).strftime("%m-%d %H:%M"),
            stock_news=stock_news,
            sector_status={
                sector: common.get("sector_status", {}).get(sector, "source_error")
                for sector in sectors
            },
            stock_status={
                str(stock["stock_code"]): common.get("stock_status", {}).get(
                    str(stock["stock_code"]), "source_error"
                )
                for stock in stocks
                if stock["news_enabled"]
            },
        )
