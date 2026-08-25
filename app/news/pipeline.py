from __future__ import annotations

import hashlib
import calendar
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from app.db import Database


MEDIA_SUFFIX = re.compile(r"[-—_|｜]\s*(财联社|新华社|央视新闻|东方财富|证券时报).*$")
PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(("utm_", "spm", "from"))
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), "")
    )


def normalized_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).strip()
    text = MEDIA_SUFFIX.sub("", text)
    return PUNCT.sub("", text).lower()


@dataclass
class NewsItem:
    title: str
    published_at: datetime
    source: str
    source_type: str
    url: str = ""
    summary: str = ""
    category: str = "general"
    related_sectors: list[str] = field(default_factory=list)
    related_stocks: list[str] = field(default_factory=list)
    stock_relations: dict[str, dict[str, object]] = field(default_factory=dict)
    is_official: bool = False
    source_weight: float = 0.5
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def canonical_url(self) -> str:
        return canonical_url(self.url)

    @property
    def normalized_title(self) -> str:
        return normalized_title(self.title)

    @property
    def id(self) -> str:
        payload = self.canonical_url or f"{self.source}:{self.normalized_title}:{self.published_at.date()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def event_key(self) -> str:
        return hashlib.sha1(
            f"{self.normalized_title[:48]}:{self.published_at.date()}".encode("utf-8")
        ).hexdigest()


def parse_feed(content: bytes, source: dict) -> list[NewsItem]:
    import feedparser

    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败：{type(parsed.bozo_exception).__name__}")
    items: list[NewsItem] = []
    for entry in parsed.entries:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        published = (
            datetime.fromtimestamp(calendar.timegm(struct), UTC)
            if struct
            else datetime.now(UTC)
        )
        items.append(
            NewsItem(
                title=str(entry.get("title", "")).strip(),
                published_at=published,
                source=str(source["name"]),
                source_type=str(source.get("type", "rss")),
                url=str(entry.get("link", "")),
                summary=str(entry.get("summary", ""))[:2000],
                category=str(source.get("category", "general")),
                is_official=bool(source.get("official", False)),
                source_weight=float(source.get("weight", 0.5)),
            )
        )
    return [item for item in items if item.title]


def deduplicate(items: list[NewsItem], threshold: float = 0.86) -> list[NewsItem]:
    kept: list[NewsItem] = []
    for item in sorted(
        items, key=lambda n: (n.is_official, n.source_weight, n.published_at), reverse=True
    ):
        duplicate = False
        for existing in kept:
            same_url = bool(item.canonical_url) and item.canonical_url == existing.canonical_url
            similar = (
                SequenceMatcher(None, item.normalized_title, existing.normalized_title).ratio()
                >= threshold
                and abs((item.published_at - existing.published_at).total_seconds()) < 172800
            )
            if same_url or similar:
                existing.related_sectors = sorted(
                    set(existing.related_sectors) | set(item.related_sectors)
                )
                existing.related_stocks = sorted(
                    set(existing.related_stocks) | set(item.related_stocks)
                )
                for stock_code, relation in item.stock_relations.items():
                    prior = existing.stock_relations.get(stock_code) or {}
                    if float(relation.get("relevance") or 0.0) >= float(
                        prior.get("relevance") or 0.0
                    ):
                        existing.stock_relations[stock_code] = dict(relation)
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept


def rank(
    item: NewsItem,
    now: datetime,
    *,
    market_impact: float = 0.5,
    relevance: float = 0.5,
    confirmations: int = 1,
) -> float:
    hours = max(0.0, (now - item.published_at).total_seconds() / 3600)
    freshness = max(0.0, 1.0 - hours / 48)
    authority = min(1.0, item.source_weight + (0.2 if item.is_official else 0.0))
    confirmation = min(1.0, confirmations / 3)
    return (
        0.30 * authority
        + 0.25 * market_impact
        + 0.20 * relevance
        + 0.15 * freshness
        + 0.10 * confirmation
    )


class NewsPipeline:
    def __init__(self, db: Database, timeout: int = 15):
        self.db = db
        self.timeout = timeout

    def fetch(self, source: dict) -> list[NewsItem]:
        started = time.monotonic()
        try:
            response = requests.get(
                source["url"],
                timeout=self.timeout,
                headers={"User-Agent": "grid-assistant/0.1 (+private RSS reader)"},
            )
            response.raise_for_status()
            items = parse_feed(response.content, source)
            self._health(source["id"], True, int((time.monotonic() - started) * 1000), "")
            return items
        except Exception as exc:
            self._health(
                source["id"], False, int((time.monotonic() - started) * 1000), type(exc).__name__
            )
            return []

    def persist(self, items: list[NewsItem]) -> int:
        inserted = 0
        with self.db.transaction() as conn:
            for item in deduplicate(items):
                existing = conn.execute(
                    "SELECT related_sectors_json,related_stocks_json FROM news_items WHERE id=?",
                    (item.id,),
                ).fetchone()
                if existing:
                    try:
                        old_sectors = json.loads(existing["related_sectors_json"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        old_sectors = []
                    item.related_sectors = sorted(set(old_sectors) | set(item.related_sectors))
                    # Stock relationships are rule-versioned and must be rebuilt from
                    # the current run. Reusing old JSON can resurrect a rejected scope.
                    item.related_stocks = sorted(set(item.related_stocks))
                cur = conn.execute(
                    """
                    INSERT INTO news_items(
                        id,title,normalized_title,published_at,source,source_type,url,
                        canonical_url,summary,category,related_sectors_json,
                        related_stocks_json,is_official,source_weight,fetched_at,event_key
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        published_at=excluded.published_at,
                        source=excluded.source,
                        source_type=excluded.source_type,
                        url=excluded.url,
                        summary=excluded.summary,
                        category=excluded.category,
                        related_sectors_json=excluded.related_sectors_json,
                        related_stocks_json=excluded.related_stocks_json,
                        is_official=excluded.is_official,
                        source_weight=excluded.source_weight,
                        fetched_at=excluded.fetched_at,
                        event_key=excluded.event_key
                    """,
                    (
                        item.id,
                        item.title,
                        item.normalized_title,
                        item.published_at.isoformat(),
                        item.source,
                        item.source_type,
                        item.url,
                        item.canonical_url or None,
                        item.summary,
                        item.category,
                        json.dumps(item.related_sectors, ensure_ascii=False),
                        json.dumps(item.related_stocks, ensure_ascii=False),
                        int(item.is_official),
                        item.source_weight,
                        item.fetched_at.isoformat(),
                        item.event_key,
                    ),
                )
                inserted += 1 if cur.rowcount else 0
                for sector in item.related_sectors:
                    conn.execute(
                        """
                        INSERT INTO news_item_scopes(
                            news_item_id,scope_type,scope_key,match_method,relevance,created_at
                        ) VALUES(?, 'sector', ?, ?, 1.0, ?)
                        ON CONFLICT(news_item_id,scope_type,scope_key) DO UPDATE SET
                            match_method=excluded.match_method,
                            relevance=excluded.relevance,
                            created_at=excluded.created_at
                        """,
                        (
                            item.id,
                            sector,
                            "directed_search" if item.source_type == "web_search" else "keyword",
                            item.fetched_at.isoformat(),
                        ),
                    )
                for stock_code in item.related_stocks:
                    relation = item.stock_relations.get(stock_code) or {}
                    conn.execute(
                        """
                        INSERT INTO news_item_scopes(
                            news_item_id,scope_type,scope_key,match_method,relevance,
                            relation_type,related_entity,relation_reason,impact_path,created_at
                        ) VALUES(?, 'stock', ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(news_item_id,scope_type,scope_key) DO UPDATE SET
                            match_method=excluded.match_method,
                            relevance=excluded.relevance,
                            relation_type=excluded.relation_type,
                            related_entity=excluded.related_entity,
                            relation_reason=excluded.relation_reason,
                            impact_path=excluded.impact_path,
                            created_at=excluded.created_at
                        """,
                        (
                            item.id,
                            stock_code,
                            str(
                                relation.get("match_method")
                                or (
                                    "directed_search"
                                    if item.source_type == "web_search"
                                    else "stock_api"
                                )
                            ),
                            float(relation.get("relevance") or 1.0),
                            str(relation.get("relation_type") or "direct"),
                            str(relation.get("related_entity") or "") or None,
                            str(relation.get("relation_reason") or "") or None,
                            str(relation.get("impact_path") or "") or None,
                            item.fetched_at.isoformat(),
                        ),
                    )
        return inserted

    def record_health(
        self, source_id: str, success: bool, response_ms: int = 0, error: str = ""
    ) -> None:
        self._health(source_id, success, response_ms, error)

    def _health(self, source_id: str, success: bool, response_ms: int, error: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO source_health(
                    source_id,last_success_at,last_failure_at,consecutive_failures,
                    response_ms,error_summary,enabled
                ) VALUES(?,?,?,?,?,?,1)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE source_health.last_success_at END,
                    last_failure_at=CASE WHEN ? THEN source_health.last_failure_at ELSE excluded.last_failure_at END,
                    consecutive_failures=CASE WHEN ? THEN 0 ELSE source_health.consecutive_failures+1 END,
                    response_ms=excluded.response_ms,
                    error_summary=excluded.error_summary
                """,
                (
                    source_id,
                    now if success else None,
                    None if success else now,
                    0 if success else 1,
                    response_ms,
                    error[:200],
                    int(success),
                    int(success),
                    int(success),
                ),
            )
