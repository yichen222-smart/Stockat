import os

import pytest
import requests

from app.indicators.talib_adapter import add_indicators
from app.market_data.akshare_adapter import AkshareAdapter


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 for network integration tests",
)


def test_live_akshare_qfq_and_talib():
    frame = AkshareAdapter(lookback_days=500).daily("600036")
    assert len(frame) > 100
    assert {"date", "open", "high", "low", "close"}.issubset(frame.columns)
    calculated = add_indicators(frame)
    assert calculated["atr14"].notna().sum() > 50
    assert calculated["natr14"].notna().sum() > 50


@pytest.mark.parametrize(
    "route",
    [
        "/gov/csrc/news",
        "/gov/nfra/915",
        "/sse/sselawsrules/latest",
        "/szse/rule/allrules/bussiness",
        "/cls/telegraph",
        "/wallstreetcn/news",
        "/jin10",
        "/eastmoney/report/strategyreport",
    ],
)
def test_live_rsshub_routes(route):
    response = requests.get(f"http://127.0.0.1:1200{route}", timeout=45)
    response.raise_for_status()
    assert b"<rss" in response.content[:500]
