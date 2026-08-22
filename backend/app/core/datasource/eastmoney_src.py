"""东方财富数据源(push2 / push2his).

东财风控专项(方案 §3.3):
- 降频: 令牌桶, 请求间隔 >= interval_sec(默认 2s, 勿低于 1.5)
- 串行: Semaphore(1), 单队列顺序执行
- 重试退避: 指数 1s/2s/4s, 最多 retry 次; 网络错重试, 风控错直接抛 EastmoneyRiskError 由 manager 切源
- UA 池 + NID 补丁(首访首页拿 cookie)
- 代理池轮换(可选)
- 熔断: 由 DataSourceManager 统一管理(连续 3 次失败熔断 10 分钟)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx
import pandas as pd

from app.core.datasource.base import DataSourceInterface, Quote, StockInfo, guess_market, normalize_kline

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

KLINE_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "daily": 101, "weekly": 102}


class EastmoneyRiskError(Exception):
    """东财风控/限流错误: 直接切源, 不重试."""


class EastmoneySource(DataSourceInterface):
    name = "eastmoney"
    label = "东方财富"

    def __init__(
        self,
        interval_sec: float = 2.0,
        max_workers: int = 1,
        retry: int = 3,
        enable_patch: bool = True,
        proxy_pool: list[str] | None = None,
    ) -> None:
        self._interval = max(interval_sec, 1.5)
        self._sem = asyncio.Semaphore(max_workers)
        self._retry = retry
        self._enable_patch = enable_patch
        self._proxy_pool = proxy_pool or []
        self._proxy_idx = 0
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 基础设施
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            self._client = httpx.AsyncClient(
                headers=headers, timeout=10.0, follow_redirects=True
            )
            if self._enable_patch:
                try:  # 首访首页获取 NID cookie
                    await self._client.get("https://quote.eastmoney.com/")
                except Exception:  # noqa: BLE001
                    logger.debug("东财 NID patch 首页请求失败(可忽略)")
        return self._client

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _next_proxy(self) -> str | None:
        if not self._proxy_pool:
            return None
        proxy = self._proxy_pool[self._proxy_idx % len(self._proxy_pool)]
        self._proxy_idx += 1
        return proxy

    async def _get(self, url: str, params: dict[str, Any]) -> dict:
        """串行 + 降频 + 指数退避. 网络错重试, 风控错抛 EastmoneyRiskError."""
        headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": "https://quote.eastmoney.com/"}
        for attempt in range(self._retry + 1):
            await self._sem.acquire()
            try:
                await self._throttle()
                proxy = self._next_proxy()
                if proxy:
                    async with httpx.AsyncClient(  # type: ignore[call-arg]
                        headers=headers, timeout=10.0, proxy=proxy, follow_redirects=True
                    ) as client:
                        resp = await client.get(url, params=params)
                else:
                    client = await self._get_client()
                    resp = await client.get(url, params=params, headers=headers)
                if resp.status_code in (403, 429, 503):
                    raise EastmoneyRiskError(f"HTTP {resp.status_code}")
                if not resp.text or "系统繁忙" in resp.text:
                    raise EastmoneyRiskError("风控: 系统繁忙")
                return resp.json()
            except EastmoneyRiskError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                if attempt >= self._retry:
                    raise
                backoff = 2**attempt
                logger.debug("东财请求失败(第%d次), %ss后重试: %s", attempt + 1, backoff, exc)
                await asyncio.sleep(backoff)
            finally:
                self._sem.release()
        raise EastmoneyRiskError("东财请求重试耗尽")  # 理论上不可达(循环内必 return/raise)

    @staticmethod
    def _secid(symbol: str) -> str:
        return f"{'1' if guess_market(symbol) == 'sh' else '0'}.{symbol}"

    # ------------------------------------------------------------ 接口实现
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None) -> pd.DataFrame:
        klt = KLINE_KLT.get(period, 101)
        params = {
            "secid": secid or self._secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": klt,
            "fqt": 1,
            "beg": 0,
            "end": 20500101,
            "lmt": count,
        }
        data = await self._get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
        kl = (data.get("data") or {}).get("klines") or []
        rows = []
        for line in kl:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]) if len(parts) > 6 else 0.0,
            })
        return normalize_kline(pd.DataFrame(rows))

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        secids = ",".join(self._secid(s) for s in symbols)
        params = {
            "secids": secids,
            "fltt": 2,
            "invt": 2,
            "fields": "f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f30",
        }
        data = await self._get("https://push2.eastmoney.com/api/qt/ulist.np/get", params)
        diff = (data.get("data") or {}).get("diff") or {}
        items = diff.values() if isinstance(diff, dict) else diff
        out: list[Quote] = []
        for it in items:
            try:
                out.append(Quote(
                    symbol=str(it.get("f12", "")),
                    name=str(it.get("f14", "")),
                    price=_f(it.get("f2")),
                    open=_f(it.get("f17")),
                    high=_f(it.get("f15")),
                    low=_f(it.get("f16")),
                    prev_close=_f(it.get("f18")),
                    volume=_f(it.get("f5")),
                    amount=_f(it.get("f6")),
                    change=_f(it.get("f4")),
                    change_pct=_f(it.get("f3")),
                    timestamp=str(it.get("f30", "")),
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        fs_map = {
            "all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "sh": "m:1+t:2,m:1+t:23",
            "sz": "m:0+t:6,m:0+t:80",
            "bj": "m:0+t:81+s:2048",
        }
        fs = fs_map.get(market, fs_map["all"])
        out: list[StockInfo] = []
        page = 1
        while page <= 200:
            params = {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": fs, "fields": "f12,f13,f14,f100",  # f100=申万行业
            }
            data = await self._get("https://push2.eastmoney.com/api/qt/clist/get", params)
            body = data.get("data") or {}
            diff = body.get("diff") or []
            if not diff:
                break
            for it in diff:
                symbol = str(it.get("f12", ""))
                if not symbol:
                    continue
                mkt = "sh" if it.get("f13") == 1 else ("bj" if symbol.startswith(("4", "8")) else "sz")
                out.append(StockInfo(
                    symbol=symbol, name=str(it.get("f14", "")), market=mkt,
                    industry=str(it.get("f100", "") or ""),
                ))
            total = int(body.get("total", 0))
            if page * 100 >= total:
                break
            page += 1
        return out

    async def health_check(self) -> bool:
        try:
            df = await self.get_kline("000001", "daily", 5)
            return df is not None and not df.empty
        except Exception:  # noqa: BLE001
            return False


def _f(v: Any) -> float:
    """东财字段可能是 '-' 或字符串数字."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("-", "").strip() or 0)
    except ValueError:
        return 0.0
