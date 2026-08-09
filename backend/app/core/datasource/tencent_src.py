"""腾讯财经数据源.

- 实时行情: https://qt.gtimg.cn/q=sz300750,sh600519 (GBK 编码文本)
- K线: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz300750,day,,,120,qfq (JSON)
- 不提供可靠股票列表 -> get_stock_list 抛 NotImplementedError, 由 manager 切到东财
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx
import pandas as pd

from app.core.datasource.base import DataSourceInterface, Quote, StockInfo, guess_market, normalize_kline

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

PERIOD_PARAM = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60", "daily": "day", "weekly": "week"}


def _fnum(v: object, default: float = 0.0) -> float:
    """安全转 float: 腾讯偶发在 K 线第 7 位塞除权信息 dict(如 {'FHcontent': '10派1.3元'}), 必须跳过."""
    if isinstance(v, (dict, list)):
        return default
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class TencentSource(DataSourceInterface):
    name = "tencent"
    label = "腾讯财经"

    def __init__(self, interval_sec: float = 0.8) -> None:
        self._interval = interval_sec
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=10.0, follow_redirects=True
            )
        return self._client

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _text(self, url: str) -> str:
        await self._throttle()
        client = await self._get_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _code(symbol: str) -> str:
        return f"{guess_market(symbol) or 'sz'}{symbol}"

    # ------------------------------------------------------------ 接口实现
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None) -> pd.DataFrame:
        p = PERIOD_PARAM.get(period, "day")
        suffix = ",qfq" if p in ("day", "week") else ""
        code = secid or self._code(symbol)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{p},,,{count}{suffix}"
        text = await self._text(url)
        import json

        data = json.loads(text)
        node = (data.get("data") or {}).get(code) or {}
        rows_raw = node.get(f"qfq{p}") or node.get(p) or []
        rows = []
        for r in rows_raw:
            if not r or len(r) < 6:
                continue
            rows.append({
                "date": r[0],
                "open": _fnum(r[1]),
                "close": _fnum(r[2]),
                "high": _fnum(r[3]),
                "low": _fnum(r[4]),
                "volume": _fnum(r[5]),
                # 第 7 位可能是成交额, 也可能是除权信息 dict, 用 _fnum 兜底
                "amount": _fnum(r[6]) if len(r) > 6 else 0.0,
            })
        return normalize_kline(pd.DataFrame(rows))

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        # 分批, 每批最多 60 只
        out: list[Quote] = []
        for i in range(0, len(symbols), 60):
            batch = symbols[i : i + 60]
            url = "https://qt.gtimg.cn/q=" + ",".join(self._code(s) for s in batch)
            text = await self._text(url)
            for line in text.strip().split(";"):
                line = line.strip()
                if not line.startswith("v_"):
                    continue
                try:
                    fields = line.split("~")
                    if len(fields) < 40:
                        continue
                    out.append(Quote(
                        symbol=fields[2].strip(),
                        name=fields[1].strip(),
                        price=float(fields[3]),
                        prev_close=float(fields[4]),
                        open=float(fields[5]),
                        volume=float(fields[6]),
                        change=float(fields[31]),
                        change_pct=float(fields[32]),
                        high=float(fields[33]),
                        low=float(fields[34]),
                        amount=float(fields[37]) * 10000,  # 万元 -> 元
                        timestamp=fields[30],
                    ))
                except (IndexError, ValueError) as exc:
                    logger.debug("腾讯行情解析失败: %s", exc)
        return out

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        # 腾讯无稳定免费全量列表接口, 交给东财/AKShare
        raise NotImplementedError("tencent 不提供股票列表, 由 manager 切换到其他源")

    async def health_check(self) -> bool:
        """直连探测(不经过 _throttle 限速锁, 避免业务并发时排队超时被误熔断)."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"https://qt.gtimg.cn/q={self._code('000001')}",
                timeout=5.0,
            )
            return resp.status_code == 200 and len(resp.text) > 10
        except Exception:  # noqa: BLE001
            return False
