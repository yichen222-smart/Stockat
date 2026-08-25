from app.reports.builder import build_report, split_wechat


def test_report_has_three_uncapped_sections_and_plain_text():
    report = build_report(
        "7月30日",
        [{"title": str(i), "source": "官方"} for i in range(8)],
        {"半导体": [{"title": str(i), "source": "工信部"} for i in range(8)]},
        [],
        "2026-07-29",
        grid_news=[{"title": str(i), "source": "东方财富"} for i in range(8)],
        watch_stocks=[
            {
                "stock_code": "600036",
                "stock_name": "招商银行",
                "grid_enabled": 1,
            }
        ],
        news_until="07-30 08:05",
    )
    assert report.count("来源：官方") == 3
    assert report.count("来源：工信部") == 3
    assert report.count("来源：东方财富") == 3
    parts = split_wechat(report)
    assert len(parts) == 3
    assert [part.startswith(f"【第{index}条/共3条") for index, part in enumerate(parts, 1)] == [True, True, True]
    assert "[内容过长，后续省略]" not in report


def test_non_report_text_is_not_cut_at_600_chars():
    text = "长" * 1200
    assert split_wechat(text) == [text]


def test_every_stock_has_own_news_and_duplicate_is_reference():
    report = build_report(
        "8月10日",
        [{"title": "芯片产业重大事件", "source": "工信部", "time": "08-10 07:00"}],
        {
            "半导体": [
                {
                    "title": "芯片产业重大事件",
                    "source": "工信部",
                    "reference": "今日要闻第1条",
                }
            ]
        },
        [],
        "2026-08-07",
        watch_stocks=[
            {"stock_code": "688981", "stock_name": "中芯国际", "grid_enabled": 0, "news_enabled": 1},
            {"stock_code": "601179", "stock_name": "中国西电", "grid_enabled": 1, "news_enabled": 1},
        ],
        stock_news={
            "688981": [{"title": "中芯国际公告", "source": "上交所"}],
            "601179": [{"title": "中国西电中标", "source": "公司公告"}],
        },
    )
    assert "关联今日要闻第1条：芯片产业重大事件" in report
    assert "688981 中芯国际" in report and "中芯国际公告" in report
    assert "601179 中国西电" in report and "中国西电中标" in report


def test_focus_scopes_have_visible_blank_line_between_items():
    report = build_report(
        "8月10日",
        [],
        {
            "银行": [{"title": "银行新闻", "source": "来源甲"}],
            "半导体": [{"title": "半导体新闻", "source": "来源乙"}],
        },
        [],
        "08:20",
        watch_stocks=[
            {"stock_code": "600770", "stock_name": "综艺股份", "news_enabled": 1},
            {"stock_code": "601179", "stock_name": "中国西电", "news_enabled": 1},
        ],
        stock_news={
            "600770": [{"title": "综艺股份新闻", "source": "来源丙"}],
            "601179": [{"title": "中国西电新闻", "source": "来源丁"}],
        },
    )

    assert "来源：来源甲\n　\n• 半导体" in report
    assert "来源：来源丙\n　\n• 601179 中国西电" in report


def test_empty_scope_still_has_one_auditable_result_row():
    report = build_report(
        "8月10日",
        [],
        {"白酒": []},
        [],
        "08:20",
        sector_status={"白酒": "no_news"},
    )
    assert "1. 已完成专属检索，窗口内无重要新增。" in report


def test_indirect_stock_news_has_visible_relation_label_and_reason():
    report = build_report(
        "8月14日",
        [],
        {},
        [],
        "08:20",
        watch_stocks=[
            {"stock_code": "601179", "stock_name": "中国西电", "news_enabled": 1}
        ],
        stock_news={
            "601179": [
                {
                    "title": "平高电气取得重大订单",
                    "source": "交易所",
                    "relation_type": "competitor",
                    "relation_label": "竞争对手相关",
                    "relation_reason": "核心竞争对手出现重大订单",
                    "impact_path": "竞争格局",
                }
            ]
        },
    )
    assert "【竞争对手相关】平高电气取得重大订单" in report
    assert "关联原因：核心竞争对手出现重大订单；影响链条：竞争格局" in report
