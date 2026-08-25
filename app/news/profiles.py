from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


RELATION_LABELS = {
    "direct": "直接相关",
    "competitor": "竞争对手相关",
    "industry": "行业相关",
    "supply_chain": "上下游相关",
}
INDIRECT_RELATIONS = {"competitor", "industry", "supply_chain"}
GENERIC_MARKET_TERMS = {
    "主力资金",
    "融资融券",
    "龙虎榜",
    "异动快报",
    "涨停揭秘",
    "大宗交易",
    "股价上涨",
    "股价下跌",
    "盘中",
    "收盘",
    "开盘",
}


def _terms(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _first_match(text: str, terms: list[str]) -> str | None:
    folded = text.casefold()
    return next((term for term in terms if term.casefold() in folded), None)


def _first_pair_match(
    text: str, entities: list[str], events: list[str], max_gap: int = 48
) -> tuple[str, str] | None:
    """Require a relationship entity and a material event to occur locally."""
    folded = text.casefold()
    for entity in entities:
        entity_folded = entity.casefold()
        start = folded.find(entity_folded)
        while start >= 0:
            entity_end = start + len(entity_folded)
            for event in events:
                event_folded = event.casefold()
                event_start = folded.find(event_folded)
                while event_start >= 0:
                    event_end = event_start + len(event_folded)
                    gap = max(start, event_start) - min(entity_end, event_end)
                    if gap <= max_gap:
                        return entity, event
                    event_start = folded.find(event_folded, event_start + 1)
            start = folded.find(entity_folded, start + 1)
    return None


@dataclass(frozen=True)
class StockProfile:
    code: str
    name: str
    generated_at: str
    raw: dict[str, Any]

    def section(self, relation_type: str) -> dict[str, Any]:
        value = self.raw.get(relation_type) or {}
        return value if isinstance(value, dict) else {}

    def search_payload(self) -> dict[str, Any]:
        return {
            "stock_code": self.code,
            "stock_name": self.name,
            "profile_generated_at": self.generated_at,
            "direct": self.section("direct"),
            "competitor": self.section("competitor"),
            "industry": self.section("industry"),
            "supply_chain": self.section("supply_chain"),
        }

    def classify(self, text: str) -> dict[str, object] | None:
        direct = self.section("direct")
        pair = _first_pair_match(
            text,
            _terms(direct.get("entities")),
            _terms(direct.get("event_terms")),
            max_gap=80,
        )
        if pair:
            entity, event = pair
            return {
                "relation_type": "direct",
                "related_entity": entity,
                "relation_reason": f"公司主体“{entity}”出现“{event}”事件",
                "impact_path": "公司经营",
                "relevance": 1.0,
            }

        competitor = self.section("competitor")
        pair = _first_pair_match(
            text,
            _terms(competitor.get("entities")),
            _terms(competitor.get("event_terms")),
        )
        if pair:
            entity, event = pair
            return {
                "relation_type": "competitor",
                "related_entity": entity,
                "relation_reason": f"核心竞争对手“{entity}”出现“{event}”事件",
                "impact_path": "竞争格局",
                "relevance": 0.72,
            }

        industry = self.section("industry")
        pair = _first_pair_match(
            text,
            _terms(industry.get("entities")),
            _terms(industry.get("event_terms")),
        )
        if pair:
            entity, event = pair
            return {
                "relation_type": "industry",
                "related_entity": entity,
                "relation_reason": f"细分行业“{entity}”出现“{event}”变化",
                "impact_path": "行业供需或定价",
                "relevance": 0.70,
            }

        supply = self.section("supply_chain")
        fallback_events = _terms(supply.get("event_terms"))
        upstream_pair = _first_pair_match(
            text,
            _terms(supply.get("upstream")),
            _terms(supply.get("upstream_event_terms")) or fallback_events,
        )
        downstream_pair = _first_pair_match(
            text,
            _terms(supply.get("downstream")),
            _terms(supply.get("downstream_event_terms")) or fallback_events,
        )
        pair = upstream_pair or downstream_pair
        if pair:
            entity, event = pair
            side = "上游" if upstream_pair else "下游"
            return {
                "relation_type": "supply_chain",
                "related_entity": entity,
                "relation_reason": f"关键{side}“{entity}”出现“{event}”变化",
                "impact_path": "成本端" if upstream_pair else "需求端",
                "relevance": 0.68,
            }
        return None

    def qualified_direct_title(self, title: str, summary: str = "") -> bool:
        text = f"{title} {summary}".strip()
        if not title.strip():
            return False
        has_company_entity = _first_match(
            title, _terms(self.section("direct").get("entities"))
        ) is not None
        if not has_company_entity:
            return False
        direct_events = _terms(self.section("direct").get("event_terms"))
        has_substantive_event = _first_match(text, direct_events) is not None
        has_generic_market_term = _first_match(text, sorted(GENERIC_MARKET_TERMS)) is not None
        return has_substantive_event or not has_generic_market_term


def load_stock_profiles(path: Path) -> tuple[dict[str, StockProfile], dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    metadata = raw.get("metadata") or {}
    generated_at = str(metadata.get("generated_at") or "")
    profiles: dict[str, StockProfile] = {}
    for code, value in (raw.get("stocks") or {}).items():
        if not isinstance(value, dict):
            continue
        normalized = str(code).zfill(6)
        profiles[normalized] = StockProfile(
            code=normalized,
            name=str(value.get("name") or normalized),
            generated_at=generated_at,
            raw=value,
        )
    return profiles, metadata


def fallback_stock_profile(code: str, name: str) -> StockProfile:
    """Keep newly added stocks useful until the next reviewed profile release."""
    return StockProfile(
        code=str(code).zfill(6),
        name=name,
        generated_at="unreviewed",
        raw={
            "direct": {
                "entities": [name, str(code).zfill(6)],
                "products": [],
                "event_terms": [
                    "公告",
                    "业绩",
                    "订单",
                    "签约",
                    "中标",
                    "投产",
                    "产能",
                    "管理层",
                    "重组",
                    "收购",
                    "处罚",
                    "事故",
                    "技术突破",
                ],
            },
            "competitor": {"entities": [], "event_terms": []},
            "industry": {"entities": [], "event_terms": []},
            "supply_chain": {"upstream": [], "downstream": [], "event_terms": []},
        },
    )
