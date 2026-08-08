"""AKShare 数据源(聚合源, 最后兜底).

- 同步库, asyncio.to_thread 包装
- 数据全、接口稳, 但较重, 只在主源都失败时使用
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from contextlib import contextmanager

import pandas as pd

from app.core.datasource.base import DataSourceInterface, Quote, StockInfo, normalize_kline

logger = logging.getLogger(__name__)

try:
    import akshare as _ak

    AKSHARE_OK = True
except ImportError:  # pragma: no cover - 可选依赖
    _ak = None
    AKSHARE_OK = False

PERIOD_MAP = {"daily": "daily", "weekly": "weekly"}
MINUTE_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


@contextmanager
def _no_proxy_env():
    """临时屏蔽系统代理环境变量(akshare 内部 requests 会读, 数据源应直连国内接口)."""
    saved = {}
    for k in _PROXY_ENV_KEYS:
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    try:
        yield
    finally:
        os.environ.update(saved)


class AkshareSource(DataSourceInterface):
    name = "akshare"
    label = "AKShare"

    def __init__(self) -> None:
        if not AKSHARE_OK:
            raise RuntimeError("akshare 未安装: pip install akshare")

    # ------------------------------------------------------------ 接口实现
    async def _call(self, func, *args, **kwargs):
        """在线程池中执行 akshare 同步调用, 同时屏蔽系统代理."""
        with _no_proxy_env():
            return await asyncio.to_thread(func, *args, **kwargs)

    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None) -> pd.DataFrame:
        sym = secid or symbol  # akshare 用 6 位代码; 指数需调用方保证格式正确
        if period in MINUTE_MAP:
            df = await self._call(
                _ak.stock_zh_a_hist_min_em, symbol=sym, period=MINUTE_MAP[period], adjust=""
            )
            df = await self._call(
                _ak.stock_zh_a_hist_min_em, symbol=symbol, period=MINUTE_MAP[period], adjust=""
            )
            if df is not None and not df.empty:
                df = df.rename(columns={
                    "时间": "date", "开盘": "open", "收盘": "close",
                    "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
                })
                df = df.tail(count)
                return normalize_kline(df)
        start = (dt.date.today() - dt.timedelta(days=int(count * 2.5) + 30)).strftime("%Y%m%d")
        df = await self._call(
            _ak.stock_zh_a_hist,
            symbol=sym,
            period=PERIOD_MAP.get(period, "daily"),
            start_date=start,
            end_date="20500101",
            adjust="qfq",
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
        })
        df = df.tail(count)
        return normalize_kline(df)

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        spot = await self._call(_ak.stock_zh_a_spot_em)
        if spot is None or spot.empty:
            return []
        spot = spot[spot["代码"].isin(symbols)]
        out: list[Quote] = []
        for _, r in spot.iterrows():
            out.append(Quote(
                symbol=str(r.get("代码", "")),
                name=str(r.get("名称", "")),
                price=float(r.get("最新价", 0) or 0),
                open=float(r.get("今开", 0) or 0),
                high=float(r.get("最高", 0) or 0),
                low=float(r.get("最低", 0) or 0),
                prev_close=float(r.get("昨收", 0) or 0),
                volume=float(r.get("成交量", 0) or 0),
                amount=float(r.get("成交额", 0) or 0),
                change=float(r.get("涨跌额", 0) or 0),
                change_pct=float(r.get("涨跌幅", 0) or 0),
            ))
        return out

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        df = await self._call(_ak.stock_info_a_code_name)
        if df is None or df.empty:
            return []
        out: list[StockInfo] = []
        for _, r in df.iterrows():
            symbol = str(r.get("code", ""))
            if not symbol:
                continue
            if market != "all":
                mkt = "sh" if symbol.startswith("6") else ("bj" if symbol.startswith(("4", "8")) else "sz")
                if mkt != market:
                    continue
            out.append(StockInfo(symbol=symbol, name=str(r.get("name", "")), market=""))
        return out

    async def health_check(self) -> bool:
        try:
            df = await self.get_kline("000001", "daily", 5)
            return df is not None and not df.empty
        except Exception:  # noqa: BLE001
            return False
