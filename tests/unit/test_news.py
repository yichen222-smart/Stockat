import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.market_data.akshare_adapter import AkshareAdapter
from app.db import Database
from app.news.analyzer import NewsAnalyzer
from app.news.pipeline import NewsItem, NewsPipeline, canonical_url, deduplicate, normalized_title
from app.news.profiles import load_stock_profiles
from app.scheduler.daily import DailyRun


def test_url_title_and_event_dedup():
    assert canonical_url("https://EXAMPLE.com/a/?utm_source=x&k=1") == "https://example.com/a?k=1"
    assert normalized_title("政策发布｜新华社") == "政策发布"
    now = datetime.now(UTC)
    items = [
        NewsItem("政策发布｜新华社", now, "新华社", "rss", "https://x/a", is_official=True),
        NewsItem("政策发布 - 财联社", now, "财联社", "rss", "https://x/b"),
    ]
    assert len(deduplicate(items)) == 1


def test_analyzer_keeps_independent_one_to_three_slots_per_entity():
    now = datetime.now(UTC).isoformat()
    items = [
        {
            "id": "g1",
            "title": "全球市场重要事件",
            "source": "官方",
            "source_type": "rss",
            "summary": "",
            "published_at": now,
            "category": "market",
            "related_sectors_json": "[]",
            "related_stocks_json": "[]",
            "is_official": 1,
            "source_weight": 1.0,
        },
        {
            "id": "s1",
            "title": "半导体产业新闻",
            "source": "工信部",
            "source_type": "rss",
            "summary": "",
            "published_at": now,
            "category": "sector",
            "related_sectors_json": '["半导体"]',
            "related_stocks_json": "[]",
            "is_official": 1,
            "source_weight": 1.0,
        },
        {
            "id": "x1",
            "title": "公司公告",
            "source": "上交所",
            "source_type": "akshare",
            "summary": "",
            "published_at": now,
            "category": "stock",
            "related_sectors_json": "[]",
            "related_stocks_json": '["688981"]',
            "is_official": 1,
            "source_weight": 1.0,
        },
    ]
    analyzer = NewsAnalyzer()
    analyzer.api_key = ""
    result = analyzer.analyze(
        items,
        ["半导体"],
        [{"stock_code": "688981", "stock_name": "中芯国际"}],
    )
    assert 1 <= len(result["top_ids"]) <= 3
    assert result["sector_ids"]["半导体"] == ["s1"]
    assert result["stock_ids"]["688981"] == ["x1"]


def test_stock_news_naive_time_is_interpreted_as_shanghai(monkeypatch):
    frame = pd.DataFrame(
        [
            {
                "关键词": "688981",
                "新闻标题": "公告",
                "新闻内容": "内容",
                "发布时间": "2026-08-10 08:00:00",
                "文章来源": "交易所",
                "新闻链接": "https://example.com/a",
            }
        ]
    )
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_news_em=lambda symbol: frame))
    row = AkshareAdapter().stock_news("688981")[0]
    assert row["published_at"].isoformat() == "2026-08-10T00:00:00+00:00"


def test_example_profiles_cover_supported_symbols_and_require_two_factors():
    root = Path(__file__).resolve().parents[2]
    profiles, metadata = load_stock_profiles(root / "config" / "stock_influence_profiles.yaml")
    assert metadata["generated_at"] == "2026-08-14"
    assert set(profiles) == {
        "601179",
        "300308",
        "688981",
        "600986",
        "603618",
        "600126",
        "600770",
        "600732",
        "601899",
    }
    profile = profiles["601179"]
    assert profile.classify("平高电气参加普通行业交流活动") is None
    relation = profile.classify("平高电气中标一项特高压设备重大订单")
    assert relation and relation["relation_type"] == "competitor"
    generic_market_report = (
        "粤开证券市场日报：今日A股主要指数多数上涨，"
        "报告后文分别讨论铜和其他投资主题。"
    )
    assert profile.classify(generic_market_report) is None
    assert profiles["603618"].classify(generic_market_report) is None
    assert profiles["600986"].classify(
        "北交所跟踪报告：指数收跌，另附算力服务公司投资名单"
    ) is None
    assert not profiles["600986"].qualified_direct_title(
        "北证50指数收跌1.62%", "市场情绪偏谨慎"
    )
    assert profiles["600986"].qualified_direct_title(
        "浙文互联发布业绩公告", "公司披露年度经营情况"
    )


def test_competitor_must_be_the_title_subject():
    root = Path(__file__).resolve().parents[2]
    profiles, _ = load_stock_profiles(root / "config" / "stock_influence_profiles.yaml")
    background_only = NewsItem(
        "三星与博通扩大存储合作",
        datetime.now(UTC),
        "媒体",
        "rss",
        summary="文章背景提到台积电产能情况。",
    )
    DailyRun._tag_stock_influence_items(
        [background_only], {"688981": profiles["688981"]}
    )
    assert "688981" not in background_only.related_stocks

    competitor_subject = NewsItem(
        "台积电宣布扩大产能",
        datetime.now(UTC),
        "媒体",
        "rss",
    )
    DailyRun._tag_stock_influence_items(
        [competitor_subject], {"688981": profiles["688981"]}
    )
    assert competitor_subject.stock_relations["688981"]["relation_type"] == "competitor"

    direct_background_only = NewsItem(
        "A股主要指数多数上涨",
        datetime.now(UTC),
        "媒体",
        "rss",
        summary="长摘要后文提到中国西电公告。",
    )
    DailyRun._tag_stock_influence_items(
        [direct_background_only], {"601179": profiles["601179"]}
    )
    assert "601179" not in direct_background_only.related_stocks


def test_pipeline_persists_stock_relation_metadata(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    item = NewsItem(
        "竞争对手重大中标",
        datetime.now(UTC),
        "交易所",
        "rss",
        url="https://example.com/relation",
        related_stocks=["601179"],
        stock_relations={
            "601179": {
                "relation_type": "competitor",
                "related_entity": "平高电气",
                "relation_reason": "核心竞争对手重大中标",
                "impact_path": "竞争格局",
                "relevance": 0.72,
                "match_method": "profile_keyword",
            }
        },
    )
    NewsPipeline(db).persist([item])
    row = db.fetchone(
        "SELECT * FROM news_item_scopes WHERE news_item_id=? AND scope_key='601179'",
        (item.id,),
    )
    assert row and row["relation_type"] == "competitor"
    assert row["related_entity"] == "平高电气"
    assert row["match_method"] == "profile_keyword"


def test_stock_selection_caps_indirect_types():
    now = datetime.now(UTC).isoformat()

    def row(item_id: str, relation_type: str) -> dict:
        return {
            "id": item_id,
            "title": item_id,
            "source": "官方",
            "source_type": "rss",
            "summary": "",
            "published_at": now,
            "category": "stock",
            "related_sectors_json": "[]",
            "related_stocks_json": '["601179"]',
            "stock_scope_meta_json": (
                '{"601179":{"relation_type":"' + relation_type + '","relevance":0.8}}'
            ),
            "is_official": 1,
            "source_weight": 1.0,
        }

    items = [
        row("direct", "direct"),
        row("competitor-1", "competitor"),
        row("competitor-2", "competitor"),
        row("industry", "industry"),
        row("supply", "supply_chain"),
    ]
    analyzer = NewsAnalyzer()
    analyzer.api_key = ""
    selected = analyzer.analyze(
        items, [], [{"stock_code": "601179", "stock_name": "中国西电"}]
    )["stock_ids"]["601179"]
    types = [
        next(item for item in items if item["id"] == item_id)["stock_scope_meta_json"]
        for item_id in selected
    ]
    assert len(selected) <= 3
    assert sum('"competitor"' in value for value in types) <= 1
    assert sum('"direct"' not in value for value in types) <= 2


def test_scope_metadata_is_authoritative_after_profile_rule_refresh():
    now = datetime.now(UTC).isoformat()
    stale = {
        "id": "stale",
        "title": "旧规则曾经误标的普通市场日报",
        "source": "媒体",
        "source_type": "rss",
        "summary": "",
        "published_at": now,
        "category": "market",
        "related_sectors_json": "[]",
        "related_stocks_json": '["601179"]',
        "stock_scope_meta_json": "{}",
        "is_official": 0,
        "source_weight": 0.5,
    }
    analyzer = NewsAnalyzer()
    analyzer.api_key = ""
    selected = analyzer.analyze(
        [stale], [], [{"stock_code": "601179", "stock_name": "中国西电"}]
    )
    assert selected["stock_ids"]["601179"] == []


def test_rerun_removes_old_stock_api_and_directed_search_scopes(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    now = datetime.now(UTC)
    items = []
    for method in ("directed_search", "stock_api"):
        item = NewsItem(
            f"旧{method}结果",
            now,
            "来源",
            "web_search" if method == "directed_search" else "akshare",
            url=f"https://example.com/{method}",
            related_stocks=["601179"],
            stock_relations={
                "601179": {
                    "relation_type": "direct",
                    "relevance": 1.0,
                    "match_method": method,
                }
            },
        )
        items.append(item)
    NewsPipeline(db).persist(items)
    run = DailyRun(SimpleNamespace(), db)
    run._reset_transient_stock_search_scopes(
        now - timedelta(minutes=1), now + timedelta(minutes=1)
    )
    assert not db.fetchall("SELECT 1 FROM news_item_scopes")


def test_repersisting_common_item_does_not_resurrect_old_stock_json(tmp_path):
    db = Database(tmp_path / "app.db")
    db.migrate()
    now = datetime.now(UTC)
    old = NewsItem(
        "普通市场日报",
        now,
        "来源",
        "akshare",
        url="https://example.com/reused",
        related_stocks=["600986"],
        stock_relations={
            "600986": {
                "relation_type": "direct",
                "relevance": 1.0,
                "match_method": "stock_api",
            }
        },
    )
    pipeline = NewsPipeline(db)
    pipeline.persist([old])
    DailyRun(SimpleNamespace(), db)._reset_transient_stock_search_scopes(
        now - timedelta(minutes=1), now + timedelta(minutes=1)
    )
    pipeline.persist(
        [NewsItem("普通市场日报", now, "公共RSS", "rss", url=old.url)]
    )
    assert not db.fetchall("SELECT 1 FROM news_item_scopes")
    row = db.fetchone("SELECT related_stocks_json FROM news_items WHERE id=?", (old.id,))
    assert row and row["related_stocks_json"] == "[]"
