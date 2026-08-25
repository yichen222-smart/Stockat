from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import requests

from app.news.pipeline import NewsItem, rank


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _unique_valid(values: object, valid: set[str], limit: int = 3) -> list[str]:
    output: list[str] = []
    if not isinstance(values, list):
        return output
    for value in values:
        if value in valid and value not in output:
            output.append(value)
        if len(output) >= limit:
            break
    return output


def _stock_meta(row: dict, stock_code: str) -> dict:
    try:
        parsed = json.loads(str(row.get("stock_scope_meta_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    value = parsed.get(stock_code) or {}
    return value if isinstance(value, dict) else {}


def _stock_scope_keys(row: dict) -> set[str]:
    try:
        parsed = json.loads(str(row.get("stock_scope_meta_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return set()
    return {str(key) for key in parsed} if isinstance(parsed, dict) else set()


def _unique_stock_valid(values: object, rows: list[dict], stock_code: str) -> list[str]:
    if not isinstance(values, list):
        return []
    by_id = {row["id"]: row for row in rows}
    chosen: list[str] = []
    indirect_count = 0
    indirect_types: set[str] = set()
    for value in values:
        if value not in by_id or value in chosen:
            continue
        relation_type = str(_stock_meta(by_id[value], stock_code).get("relation_type") or "direct")
        if relation_type != "direct":
            if indirect_count >= 2 or relation_type in indirect_types:
                continue
            indirect_count += 1
            indirect_types.add(relation_type)
        chosen.append(value)
        if len(chosen) >= 3:
            break
    return chosen


class NewsAnalyzer:
    """Select news independently for the global, sector and stock scopes."""

    def __init__(self, timeout: int = 90):
        self.timeout = timeout
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.model = os.environ.get("HERMES_MODEL", "deepseek-v4-flash")

    def analyze(self, items: list[dict], sectors: list[str], stocks: list[dict]) -> dict:
        pools = self._scope_pools(items, sectors, stocks)
        fallback = self._fallback(pools)
        if not self.api_key or not items:
            return fallback

        def compact(
            rows: list[dict], limit: int, stock_code: str | None = None
        ) -> list[dict]:
            compacted = []
            for row in rows[:limit]:
                item = {
                    "id": row["id"],
                    "title": row["title"],
                    "source": row["source"],
                    "summary": (row.get("summary") or "")[:300],
                    "published_at": row["published_at"],
                    "category": row.get("category"),
                    "official": bool(row.get("is_official")),
                }
                if stock_code:
                    meta = _stock_meta(row, stock_code)
                    item.update(
                        {
                            "relation_type": meta.get("relation_type") or "direct",
                            "related_entity": meta.get("related_entity"),
                            "relation_reason": meta.get("relation_reason"),
                            "impact_path": meta.get("impact_path"),
                            "relevance": meta.get("relevance"),
                        }
                    )
                compacted.append(item)
            return compacted

        prompt = {
            "task": "分别从互相独立的候选池中做事实型筛选和短摘要，不得给买卖建议。",
            "rules": [
                "今日要闻选择1至3条",
                "每个有候选的板块必须选择1至3条",
                "每只有候选的股票必须选择1至3条",
                "每只股票间接新闻合计最多2条，同一种间接类型最多1条",
                "间接新闻必须有明确relation_reason，优先直接新闻和高重要性间接事件，不得凑数",
                "只能选择对应候选池里的id，禁止用总体候选代替板块或股票候选",
                "优先公告、官方原始来源、重大事件和多源确认",
                "仅返回JSON",
            ],
            "global_candidates": compact(pools["global"], 25),
            "sector_candidates": {
                key: compact(rows, 12) for key, rows in pools["sectors"].items()
            },
            "stock_candidates": {
                key: compact(rows, 12, key) for key, rows in pools["stocks"].items()
            },
            "output": {
                "top_ids": ["id"],
                "sector_ids": {"板块名": ["id"]},
                "stock_ids": {"股票代码": ["id"]},
                "summaries": {"id": "不超过80字的事实摘要"},
            },
        }
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是审慎的财经新闻编辑，只做事实筛选。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(prompt, ensure_ascii=False),
                        },
                    ],
                },
            )
            response.raise_for_status()
            parsed = json.loads(response.json()["choices"][0]["message"]["content"])
            return self._validate(parsed, fallback, pools)
        except Exception:
            return fallback

    @staticmethod
    def _scope_pools(items: list[dict], sectors: list[str], stocks: list[dict]) -> dict:
        global_rows = [
            row
            for row in items
            if row.get("category") != "stock"
            and not _json_list(row.get("related_stocks_json"))
        ]
        if not global_rows:
            global_rows = list(items)

        sector_rows: dict[str, list[dict]] = {}
        for sector in sectors:
            sector_rows[sector] = [
                row
                for row in items
                if sector in _json_list(row.get("related_sectors_json"))
            ]

        stock_rows: dict[str, list[dict]] = {}
        for stock in stocks:
            code = str(stock["stock_code"])
            stock_rows[code] = [
                row
                for row in items
                if code in _stock_scope_keys(row)
                or (
                    not row.get("stock_scope_meta_json")
                    and code in _json_list(row.get("related_stocks_json"))
                )
            ]
        return {"global": global_rows, "sectors": sector_rows, "stocks": stock_rows}

    @classmethod
    def _validate(cls, parsed: dict, fallback: dict, pools: dict) -> dict:
        valid_top = {row["id"] for row in pools["global"]}
        result = {
            "top_ids": _unique_valid(parsed.get("top_ids"), valid_top),
            "sector_ids": {},
            "stock_ids": {},
            "summaries": {},
        }
        for key, rows in pools["sectors"].items():
            valid = {row["id"] for row in rows}
            chosen = _unique_valid((parsed.get("sector_ids") or {}).get(key), valid)
            result["sector_ids"][key] = chosen or fallback["sector_ids"][key]
        for key, rows in pools["stocks"].items():
            chosen = _unique_stock_valid(
                (parsed.get("stock_ids") or {}).get(key), rows, key
            )
            result["stock_ids"][key] = chosen or fallback["stock_ids"][key]
        if not result["top_ids"]:
            result["top_ids"] = fallback["top_ids"]
        valid_all = {
            row["id"]
            for rows in [pools["global"], *pools["sectors"].values(), *pools["stocks"].values()]
            for row in rows
        }
        summaries = parsed.get("summaries") or {}
        if isinstance(summaries, dict):
            result["summaries"] = {
                key: str(value)[:120]
                for key, value in summaries.items()
                if key in valid_all
            }
        return result

    @staticmethod
    def _fallback(pools: dict) -> dict:
        now = datetime.now(UTC)

        def score(row: dict) -> float:
            published = datetime.fromisoformat(row["published_at"])
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            item = NewsItem(
                row["title"],
                published,
                row["source"],
                row.get("source_type", "rss"),
                summary=row.get("summary") or "",
                is_official=bool(row.get("is_official")),
                source_weight=float(row.get("source_weight") or 0.5),
            )
            return rank(item, now)

        def choose(rows: list[dict]) -> list[str]:
            return [row["id"] for row in sorted(rows, key=score, reverse=True)[:3]]

        def choose_stock(rows: list[dict], stock_code: str) -> list[str]:
            def stock_score(row: dict) -> float:
                relevance = float(_stock_meta(row, stock_code).get("relevance") or 0.5)
                return score(row) + 0.20 * relevance

            ordered = [
                row["id"] for row in sorted(rows, key=stock_score, reverse=True)
            ]
            return _unique_stock_valid(ordered, rows, stock_code)

        return {
            "top_ids": choose(pools["global"]),
            "sector_ids": {key: choose(rows) for key, rows in pools["sectors"].items()},
            "stock_ids": {
                key: choose_stock(rows, key) for key, rows in pools["stocks"].items()
            },
            "summaries": {},
        }
