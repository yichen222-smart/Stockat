from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import time
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd


ALIASES = {
    "date": ("日期", "date", "trade_date"),
    "open": ("开盘", "open"),
    "high": ("最高", "high"),
    "low": ("最低", "low"),
    "close": ("收盘", "close"),
    "volume": ("成交量", "volume", "vol"),
    "amount": ("成交额", "amount"),
}


class MarketDataError(RuntimeError):
    pass


T = TypeVar("T")

def _with_retries(operation: Callable[[], T], attempts: int = 3) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def normalize_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise MarketDataError("行情接口返回空数据")
    rename: dict[Any, str] = {}
    for canonical, names in ALIASES.items():
        match = next((name for name in names if name in frame.columns), None)
        if match is None and canonical not in {"volume", "amount"}:
            raise MarketDataError(f"行情缺少字段：{canonical}")
        if match is not None:
            rename[match] = canonical
    result = frame.rename(columns=rename)[list(rename.values())].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in result:
            result[col] = pd.to_numeric(result[col], errors="coerce")
    result = result.dropna(subset=["date", "open", "high", "low", "close"])
    result = result.sort_values("date").drop_duplicates("date", keep="last")
    invalid = (
        (result[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (result["high"] < result["low"])
    )
    if invalid.any():
        raise MarketDataError("行情包含非正价格或最高价低于最低价")
    return result.reset_index(drop=True)


@dataclass
class AkshareAdapter:
    lookback_days: int = 1100

    def daily(self, stock_code: str, end: date | None = None) -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as exc:
            raise MarketDataError("AKShare is not installed") from exc
        end = end or date.today()
        start = end - timedelta(days=self.lookback_days)

        def eastmoney() -> pd.DataFrame:
            return ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )

        market = "sh" if stock_code[0] in "569" else "bj" if stock_code[0] in "48" else "sz"

        def sina() -> pd.DataFrame:
            return ak.stock_zh_a_daily(
                symbol=f"{market}{stock_code}",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )

        errors: list[str] = []
        for operation in (eastmoney, sina):
            try:
                return normalize_daily_frame(_with_retries(operation))
            except Exception as exc:
                errors.append(type(exc).__name__)
        raise MarketDataError("AKShare market data failed: " + ", ".join(errors))

    def trading_calendar(self) -> set[date]:
        try:
            import akshare as ak

            frame = ak.tool_trade_date_hist_sina()
            column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
            return set(pd.to_datetime(frame[column]).dt.date)
        except Exception as exc:
            raise MarketDataError(f"交易日历获取失败：{type(exc).__name__}") from exc

    def resolve_stock(self, query: str) -> tuple[str, str]:
        query = query.strip()
        is_code = len(query) == 6 and query.isdigit()
        if not is_code and not 1 <= len(query) <= 20:
            raise ValueError("股票名称格式无效")
        try:
            import akshare as ak

            frame = ak.stock_info_a_code_name()
            code_col = "code" if "code" in frame.columns else "证券代码"
            name_col = "name" if "name" in frame.columns else "证券简称"
            codes = frame[code_col].astype(str).str.zfill(6)
            if is_code:
                match = frame[codes == query]
            else:
                match = frame[frame[name_col].astype(str).str.strip() == query]
            if len(match) == 1:
                row = match.iloc[0]
                return str(row[code_col]).zfill(6), str(row[name_col]).strip()
            if len(match) > 1:
                raise ValueError("股票名称不唯一，请改用 6 位代码")
        except ValueError:
            raise
        except Exception:
            if is_code:
                return query, query
        if is_code:
            return query, query
        raise ValueError("未找到该 A 股名称，请改用 6 位代码")
    def resolve_stock_name(self, stock_code: str) -> str:
        if len(stock_code) != 6 or not stock_code.isdigit():
            raise ValueError("A 股代码应为 6 位数字")
        try:
            import akshare as ak

            frame = ak.stock_info_a_code_name()
            code_col = "code" if "code" in frame.columns else "证券代码"
            name_col = "name" if "name" in frame.columns else "证券简称"
            match = frame[frame[code_col].astype(str).str.zfill(6) == stock_code]
            if not match.empty:
                return str(match.iloc[0][name_col])
        except Exception:
            pass
        return stock_code

    def stock_news(self, stock_code: str) -> list[dict[str, Any]]:
        try:
            import akshare as ak

            frame = ak.stock_news_em(symbol=stock_code)
        except Exception as exc:
            raise MarketDataError(f"AKShare 个股新闻失败：{type(exc).__name__}") from exc
        if frame is None or frame.empty:
            return []

        def column(*names: str, position: int) -> Any:
            return next((name for name in names if name in frame.columns), frame.columns[position])

        title_col = column("新闻标题", "title", position=1)
        summary_col = column("新闻内容", "content", position=2)
        time_col = column("发布时间", "date", position=3)
        source_col = column("文章来源", "source", position=4)
        url_col = column("新闻链接", "url", position=5)
        output: list[dict[str, Any]] = []
        for _, row in frame.head(10).iterrows():
            published = pd.to_datetime(row[time_col], errors="coerce")
            if pd.isna(published):
                published_at = datetime.now(UTC)
            else:
                published_at = published.to_pydatetime()
                if published_at.tzinfo is None:
                    published_at = published_at.replace(
                        tzinfo=ZoneInfo("Asia/Shanghai")
                    ).astimezone(UTC)
                else:
                    published_at = published_at.astimezone(UTC)
            output.append(
                {
                    "title": str(row[title_col]).strip(),
                    "summary": str(row[summary_col])[:2000],
                    "published_at": published_at,
                    "source": str(row[source_col]).strip() or "东方财富",
                    "url": str(row[url_col]).strip(),
                    "stock_code": stock_code,
                }
            )
        return [item for item in output if item["title"]]
