import pandas as pd

from app.grid.engine import TradingCosts, conservative_backtest, should_replace


def test_conservative_intraday_order_and_debounce():
    rows = []
    for i in range(60):
        rows.append({"close": 100.0, "high": 104.0, "low": 96.0})
    result = conservative_backtest(pd.DataFrame(rows), 0.02, 0.02, TradingCosts())
    assert result.worst_order_used
    assert result.fills > 0
    assert not should_replace(10, 11, 3)
    assert should_replace(10, 10.5, 5)


def test_replacement_when_active_invalid():
    assert should_replace(10, 1, 0, active_invalid=True)
