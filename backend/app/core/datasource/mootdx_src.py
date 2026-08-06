"""mootdx 数据源(通达信协议, 本地直连, 默认优先级最高).

- mootdx 为同步库, 统一用 asyncio.to_thread 包装, 避免阻塞事件循环
- 依赖公共通达信服务器, 偶发不可达属正常, 由 manager 自动切换备用源
- 不提供可靠的全量股票列表 -> 由 HTTP 源承担
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pandas as pd

from app.core.datasource.base import DataSourceInterface, Quote, StockInfo, normalize_kline

logger = logging.getLogger(__name__)

try:
    from mootdx.quotes import Quotes as _MootdxQuotes

    MOOTDX_OK = True
except ImportError:  # pragma: no cover - 可选依赖
    _MootdxQuotes = None
    MOOTDX_OK = False

FREQ_MAP = {"1m": 7, "5m": 0, "15m": 1, "30m": 2, "60m": 3, "daily": 9, "weekly": 5}

# 经典通达信行情服务器(2026-08 实测全部可用)。
# mootdx 内置 bestip 探测的默认服务器列表已大面积失效, 故直连指定服务器并失败轮换。
MOOTDX_SERVERS: list[tuple[str, int]] = [
    ("119.147.212.81", 7709),
    ("115.238.90.165", 7709),
    ("114.80.63.12", 7709),
    ("180.153.18.170", 7709),
    ("202.108.253.130", 7709),
    ("114.80.63.35", 7709),
    ("123.125.108.14", 7709),
    ("60.28.23.80", 7709),
    ("218.75.126.9", 7709),
]


class MootdxSource(DataSourceInterface):
    name = "mootdx"
    label = "通达信(mootdx)"

    def __init__(self) -> None:
        if not MOOTDX_OK:
            raise RuntimeError("mootdx 未安装: pip install mootdx")
        self._client = None
        self._server_idx = 0
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                ip, port = MOOTDX_SERVERS[self._server_idx % len(MOOTDX_SERVERS)]
                logger.info("mootdx 连接 %s:%d", ip, port)
                self._client = _MootdxQuotes.factory(
                    market="std", server=ip, port=port, timeout=10, heartbeat=False
                )
            return self._client

    def _reset_client(self) -> None:
        """连接失败时换下一台服务器."""
        with self._lock:
            self._client = None
            self._server_idx += 1
            logger.warning("mootdx 连接失败, 将切换服务器 #%d", self._server_idx % len(MOOTDX_SERVERS))

    # ------------------------------------------------------------ 接口实现
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120) -> pd.DataFrame:
        freq = FREQ_MAP.get(period, 9)
        try:
            client = self._get_client()
            df = await asyncio.to_thread(client.bars, symbol=symbol, frequency=freq, offset=count)
            if df is None or df.empty:
                self._reset_client()
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
            # mootdx 返回列: datetime/open/close/high/low/volume/amount...
            return normalize_kline(df)
        except Exception:
            self._reset_client()
            raise

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        try:
            client = self._get_client()
            df = await asyncio.to_thread(client.quotes, symbols=symbols)
        except Exception:
            self._reset_client()
            raise
        if df is None or df.empty:
            return []
        out: list[Quote] = []
        for _, row in df.iterrows():
            try:
                out.append(Quote(
                    symbol=str(row.get("code", "")),
                    name=str(row.get("name", "")),
                    price=float(row.get("price", 0) or 0),
                    open=float(row.get("open", 0) or 0),
                    high=float(row.get("high", 0) or 0),
                    low=float(row.get("low", 0) or 0),
                    prev_close=float(row.get("last_close", 0) or 0),
                    volume=float(row.get("vol", 0) or 0),
                    amount=float(row.get("amount", 0) or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        raise NotImplementedError("mootdx 不提供全量股票列表, 由 HTTP 源提供")

    async def health_check(self) -> bool:
        try:
            df = await self.get_kline("000001", "daily", 5)
            return df is not None and not df.empty
        except Exception:  # noqa: BLE001
            return False
