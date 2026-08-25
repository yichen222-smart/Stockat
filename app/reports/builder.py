from __future__ import annotations

import re


DISCLAIMER = "内容仅作信息整理和参数参考，不构成投资建议。"
SECTION_PATTERN = re.compile(r"(?=【第[123]条/共3条)")
VISIBLE_BLANK_LINE = "　"


def split_wechat(
    text: str,
    limit: int | None = None,
    max_parts: int | None = None,
) -> list[str]:
    """Split only on report sections; no application-level character cap."""
    del limit, max_parts
    parts = [part.strip() for part in SECTION_PATTERN.split(text.strip()) if part.strip()]
    return parts if len(parts) == 3 else [text.strip()]


def _news_lines(
    items: list[dict],
    *,
    empty: str,
    status: str | None = None,
) -> list[str]:
    if not items:
        if status == "source_error":
            return ["1. 专属新闻源暂时异常，本次结果可能不完整。"]
        if status == "no_news":
            return ["1. 已完成专属检索，窗口内无重要新增。"]
        return [f"1. {empty}"]
    lines: list[str] = []
    for index, item in enumerate(items[:3], 1):
        relation_type = str(item.get("relation_type") or "direct")
        relation_prefix = (
            f"【{item.get('relation_label')}】" if relation_type != "direct" else ""
        )
        if item.get("reference"):
            lines.append(
                f"{index}. {relation_prefix}关联{item['reference']}：{item['title']}"
            )
        else:
            lines.append(f"{index}. {relation_prefix}{item['title']}")
            lines.append(
                f"来源：{item['source']}｜{item.get('time', '')}".rstrip("｜")
            )
        if relation_type != "direct" and item.get("relation_reason"):
            reason = str(item["relation_reason"])
            if item.get("impact_path"):
                reason += f"；影响链条：{item['impact_path']}"
            lines.append(f"关联原因：{reason}")
    return lines


def build_report(
    report_date: str,
    top_news: list[dict],
    sector_news: dict[str, list[dict]],
    grid_rows: list[dict],
    data_until: str,
    grid_news: list[dict] | None = None,
    watch_stocks: list[dict] | None = None,
    news_until: str | None = None,
    stock_news: dict[str, list[dict]] | None = None,
    sector_status: dict[str, str] | None = None,
    stock_status: dict[str, str] | None = None,
) -> str:
    watch_stocks = watch_stocks or []
    sector_status = sector_status or {}
    stock_status = stock_status or {}
    if stock_news is None:
        legacy = grid_news or []
        stock_news = {
            str(stock["stock_code"]): legacy for stock in watch_stocks
        }
    stopped = sum(row.get("status") == "STOPPED" for row in grid_rows)

    first = [
        f"【第1条/共3条｜{report_date}盘前提示】",
        "",
        "🌅 一眼结论",
        "• 先看今日要闻，再核对你的关注和网格停止状态。",
        f"• 网格状态：{stopped} 只停止，{len(grid_rows) - stopped} 只正常跟踪。",
        "",
        "📰 今日必看",
        *_news_lines(top_news, empty="今日未筛选到需要特别提示的市场要闻。"),
        "",
        f"资讯截至：{news_until or data_until}",
    ]

    second = [
        "【第2条/共3条｜你的关注】",
        "",
        "━━ 📌 板块关注 ━━",
    ]
    if sector_news:
        for scope_index, (sector, items) in enumerate(sector_news.items()):
            second.extend(
                ["" if scope_index == 0 else VISIBLE_BLANK_LINE, f"• {sector}"]
            )
            second.extend(
                _news_lines(
                    items,
                    empty="今日未筛选到该板块的重要新闻。",
                    status=sector_status.get(sector),
                )
            )
    else:
        second.extend(["", "当前没有启用中的自选板块。"])
    second.extend(["", "━━ 📈 股票关注 ━━"])
    if watch_stocks:
        for scope_index, stock in enumerate(watch_stocks):
            label = "网格股票" if stock.get("grid_enabled") else "普通关注"
            code = str(stock["stock_code"])
            second.extend(
                [
                    "" if scope_index == 0 else VISIBLE_BLANK_LINE,
                    f"• {code} {stock['stock_name']}｜{label}",
                ]
            )
            if stock.get("news_enabled", 1):
                second.extend(
                    _news_lines(
                        stock_news.get(code, []),
                        empty="今日未筛选到该股票的重要新闻。",
                        status=stock_status.get(code),
                    )
                )
            else:
                second.append("股票新闻已暂停。")
    else:
        second.extend(["", "当前没有启用中的自选股票。"])

    third = ["【第3条/共3条｜网格参数】"]
    if not grid_rows:
        third.extend(["", "当前没有启用中的网格股票。"])
    for row in grid_rows:
        third.extend(
            [
                "",
                f"{row['stock_code']} {row['stock_name']}",
                f"状态：{'已停止' if row['status'] == 'STOPPED' else '正常跟踪'}",
            ]
        )
        if row.get("latest_close") is not None:
            third.append(
                f"最近收盘价：{row['latest_close']:.3f} 元"
                f"（{row.get('latest_close_date', data_until)}）"
            )
        if row.get("adjustment"):
            third.append("行情口径：前复权（qfq）")
        third.extend(
            [
                f"网格中枢参考价：{row['reference_price']:.3f} 元",
                "说明：中枢参考价是近20个交易日收盘价中位数，不是最新股价。",
                f"买入档间距：每下跌 {row['buy_grid_pct']:.2%} 一档",
                f"卖出档间距：每上涨 {row['sell_grid_pct']:.2%} 一档",
                f"正常参考范围：{row['normal_lower']:.3f}—{row['normal_upper']:.3f} 元",
                f"极限停止价：{row['extreme_lower']:.3f} / {row['extreme_upper']:.3f} 元",
                "📝 你的手动记录",
                f"连续买入 {row.get('buy_count', 0)} 档｜连续卖出 {row.get('sell_count', 0)} 档",
                "记录来自你在微信中的主动上报，不会读取证券账户。",
            ]
        )
        if row.get("stop_reason"):
            third.append(f"停止原因：{row['stop_reason']}")
        third.extend(
            [
                "⛔ 停止条件",
                "连续同方向成交5档；连续两日越过正常范围；"
                "单日越过极限停止价；出现重大风险或数据异常。",
                f"当前网格参数版本数据截至：{row.get('data_until', data_until)}",
            ]
        )
    third.extend(["", DISCLAIMER])
    return "\n".join(first) + "\n\n" + "\n".join(second) + "\n\n" + "\n".join(third)
