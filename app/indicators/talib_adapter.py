from __future__ import annotations

import numpy as np
import pandas as pd


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    try:
        import talib
    except ImportError as exc:
        raise RuntimeError("TA-Lib 未安装，禁止用手写指标替代") from exc
    out = frame.copy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    out["atr14"] = talib.ATR(high, low, close, timeperiod=14)
    out["natr14"] = talib.NATR(high, low, close, timeperiod=14)
    out["sma20"] = talib.SMA(close, timeperiod=20)
    out["sma60"] = talib.SMA(close, timeperiod=60)
    out["rsi14"] = talib.RSI(close, timeperiod=14)
    if np.isnan(out["natr14"].iloc[-1]):
        raise RuntimeError("有效历史数据不足以计算 NATR(14)")
    return out
