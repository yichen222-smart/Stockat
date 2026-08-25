from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TradingCosts:
    buy_commission: float = 0.0003
    sell_commission: float = 0.0003
    stamp_tax: float = 0.0005
    slippage: float = 0.0005
    minimum_commission: float = 0.0


@dataclass(frozen=True)
class GridParameters:
    reference_price: float
    buy_grid_pct: float
    sell_grid_pct: float
    first_buy_price: float
    first_sell_price: float
    normal_lower: float
    normal_upper: float
    extreme_lower: float
    extreme_upper: float
    score: float
    data_until: str
    model_version: str = "grid-wf-v1"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BacktestResult:
    score: float
    net_value: float
    fills: int
    max_drawdown: float
    worst_order_used: bool


def _simulate_order(
    low: float,
    high: float,
    reference: float,
    buy_pct: float,
    sell_pct: float,
    order: str,
    inventory: int,
    costs: TradingCosts,
) -> tuple[float, int, int]:
    cash = 0.0
    fills = 0
    buy_price = reference * (1 - buy_pct)
    sell_price = reference * (1 + sell_pct)
    directions = ("buy", "sell") if order == "buy_first" else ("sell", "buy")
    for direction in directions:
        if direction == "buy" and low <= buy_price and inventory < 5:
            cost = buy_price * (1 + costs.buy_commission + costs.slippage)
            cash -= cost
            inventory += 1
            fills += 1
        elif direction == "sell" and high >= sell_price and inventory > -5:
            proceeds = sell_price * (
                1 - costs.sell_commission - costs.stamp_tax - costs.slippage
            )
            cash += proceeds
            inventory -= 1
            fills += 1
    return cash, inventory, fills


def conservative_backtest(
    frame: pd.DataFrame,
    buy_pct: float,
    sell_pct: float,
    costs: TradingCosts,
) -> BacktestResult:
    if len(frame) < 30:
        raise ValueError("回测至少需要 30 个交易日")
    reference = float(frame["close"].iloc[:20].median())
    cash = 0.0
    inventory = 0
    fills = 0
    curve: list[float] = []
    for row in frame.iloc[20:].itertuples():
        outcomes = []
        for order in ("buy_first", "sell_first"):
            day_cash, day_inventory, day_fills = _simulate_order(
                float(row.low),
                float(row.high),
                reference,
                buy_pct,
                sell_pct,
                order,
                inventory,
                costs,
            )
            marked = cash + day_cash + day_inventory * float(row.close)
            outcomes.append((marked, day_cash, day_inventory, day_fills))
        _, day_cash, inventory, day_fills = min(outcomes, key=lambda item: item[0])
        cash += day_cash
        fills += day_fills
        curve.append(cash + inventory * float(row.close))
        reference = 0.95 * reference + 0.05 * float(row.close)
    values = np.asarray(curve, dtype=float)
    peaks = np.maximum.accumulate(values)
    drawdowns = peaks - values
    max_drawdown = float(drawdowns.max(initial=0.0))
    net_value = float(values[-1])
    turnover_penalty = max(0, fills - len(frame) // 2) * 0.02
    score = net_value - 0.5 * max_drawdown - turnover_penalty
    return BacktestResult(score, net_value, fills, max_drawdown, True)


def search_parameters(
    frame: pd.DataFrame,
    costs: TradingCosts,
    buy_candidates: Iterable[float],
    sell_candidates: Iterable[float],
) -> GridParameters:
    clean = frame.dropna(subset=["close", "high", "low", "natr14"]).copy()
    if len(clean) < 120:
        raise ValueError("网格参数需要至少 120 个有效交易日")
    split = max(80, int(len(clean) * 0.7))
    train, validate = clean.iloc[:split], clean.iloc[split:]
    candidates = []
    for buy, sell in product(buy_candidates, sell_candidates):
        train_result = conservative_backtest(train, buy, sell, costs)
        validation_result = conservative_backtest(validate, buy, sell, costs)
        stability = abs(train_result.score - validation_result.score)
        combined = validation_result.score - 0.25 * stability
        candidates.append((combined, buy, sell, validation_result))
    score, buy_pct, sell_pct, _ = max(candidates, key=lambda item: item[0])
    reference = float(clean["close"].iloc[-20:].median())
    atr_pct = float(clean["natr14"].iloc[-20:].median()) / 100
    normal_span = max(atr_pct * 4, max(buy_pct, sell_pct) * 5)
    extreme_span = max(normal_span * 1.75, atr_pct * 7)
    return GridParameters(
        reference_price=round(reference, 3),
        buy_grid_pct=buy_pct,
        sell_grid_pct=sell_pct,
        first_buy_price=round(reference * (1 - buy_pct), 3),
        first_sell_price=round(reference * (1 + sell_pct), 3),
        normal_lower=round(reference * (1 - normal_span), 3),
        normal_upper=round(reference * (1 + normal_span), 3),
        extreme_lower=round(reference * (1 - extreme_span), 3),
        extreme_upper=round(reference * (1 + extreme_span), 3),
        score=round(float(score), 6),
        data_until=str(clean["date"].iloc[-1].date()),
    )


def should_replace(
    active_score: float,
    candidate_score: float,
    active_trading_days: int,
    *,
    min_hold_days: int = 5,
    improvement_threshold: float = 0.05,
    active_invalid: bool = False,
) -> bool:
    if active_invalid:
        return True
    if active_trading_days < min_hold_days:
        return False
    denominator = max(abs(active_score), 1e-9)
    return (candidate_score - active_score) / denominator >= improvement_threshold
