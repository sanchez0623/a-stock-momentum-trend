"""数据源管理器(方案 §3.2).

- 维护优先级队列 + 健康分(连续失败/延迟/熔断)
- 取数按 近期偏好 + 配置优先级 依次尝试, 首个成功即返回
- 后台每 60s 探活
- 配置热更新后刷新顺序与代理池
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Callable

import pandas as pd

from app.core.config import config_manager
from app.core.datasource.base import DataSourceInterface, HealthState, Quote, StockInfo
from app.core.datasource.cache import kline_store, quote_cache

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 15.0
CIRCUIT_AFTER = 5  # 连续失败 5 次才熔断(原 3, 腾讯偶发抖动 3 次易误熔断)
CIRCUIT_SECS = 600
HEALTH_INTERVAL = 60.0


class DataSourceManager:
    """业务唯一入口: 换源零成本, 业务不感知上游."""

    def __init__(self) -> None:
        self._sources: dict[str, DataSourceInterface] = {}
        self._health: dict[str, HealthState] = {}
        self._priority: list[str] = []  # 配置优先级
        self._preference: list[str] = []  # 近期偏好(成功置顶)
        self._proxy_pool: list[str] = []
        self._bg_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 注册与启动
    def register(self, source: DataSourceInterface) -> None:
        self._sources[source.name] = source
        self._health[source.name] = HealthState()

    def setup(self, source_factories: list[tuple[str, Callable[[], DataSourceInterface]]]) -> None:
        """创建并注册启用的源. source_factories: [(name, 工厂函数), ...]."""
        cfg = config_manager.get()
        ds_cfg = cfg.get("数据源", {})
        enabled = ds_cfg.get("enabled", {})
        self._proxy_pool = ds_cfg.get("proxy_pool", [])
        self._priority = list(ds_cfg.get("priority", []))
        for name, factory in source_factories:
            if not enabled.get(name, True):
                logger.info("数据源 %s 未启用, 跳过", name)
                continue
            try:
                self.register(factory())
                logger.info("数据源 %s 已注册", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 初始化失败(可选依赖缺失?), 跳过: %s", name, exc)
        config_manager.register_listener(self._on_config_change)

    def _on_config_change(self, cfg: dict) -> None:
        ds_cfg = cfg.get("数据源", {})
        self._proxy_pool = ds_cfg.get("proxy_pool", [])
        new_priority = list(ds_cfg.get("priority", []))
        # 保留仍启用的顺序, 移除禁用的
        enabled = ds_cfg.get("enabled", {})
        self._priority = [n for n in new_priority if self._sources.get(n) and enabled.get(n, True)]
        logger.info("数据源优先级已更新: %s", self._priority)

    async def start_health_checks(self) -> None:
        if self._bg_task is None:
            self._bg_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            for name, src in self._sources.items():
                try:
                    ok = await asyncio.wait_for(src.health_check(), timeout=5)
                    self._record(name, ok, 0.0)
                except Exception:  # noqa: BLE001
                    self._record(name, False, 0.0)

    # ------------------------------------------------------------ 状态
    def _active_order(self) -> list[str]:
        """近期偏好优先, 其次配置优先级; 过滤熔断中的源."""
        order: list[str] = []
        seen: set[str] = set()
        for n in self._preference + self._priority:
            if n in seen or n not in self._sources:
                continue
            seen.add(n)
            order.append(n)
        return order

    def _record(self, name: str, ok: bool, latency_ms: float) -> None:
        st = self._health.setdefault(name, HealthState())
        st.record(ok, latency_ms, circuit_after=CIRCUIT_AFTER, circuit_secs=CIRCUIT_SECS)
        if not ok:
            logger.warning(
                "数据源 %s 请求失败(连续 %d 次), 熔断至 %s",
                name, st.consecutive_failures,
                dt.datetime.fromtimestamp(st.circuit_open_until).strftime("%H:%M:%S") if st.is_circuit_open() else "-",
            )

    def _bump_preference(self, name: str) -> None:
        self._preference = [name] + [n for n in self._preference if n != name]
        self._preference = self._preference[:8]

    def status(self) -> list[dict]:
        """各源状态(供 /api/data-sources/status)."""
        out = []
        for name in self._active_order():
            st = self._health[name]
            out.append({
                "name": name,
                "label": self._sources[name].label or name,
                "enabled": True,
                "circuit_open": st.is_circuit_open(),
                "consecutive_failures": st.consecutive_failures,
                "avg_latency_ms": round(st.avg_latency_ms, 1),
                "request_count": st.request_count,
                "success_count": st.success_count,
                "preferred": self._preference[:1] == [name],
            })
        return out

    # ------------------------------------------------------------ 数据入口
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120) -> pd.DataFrame:
        """K线: 缓存优先, 缺失/过期回源, 合并后返回最近 count 条."""
        cached = kline_store.get_dataframe(symbol, period)
        if cached is not None and len(cached) >= count and self._cache_fresh(cached, period):
            return cached.tail(count).reset_index(drop=True)
        fresh = await self._fetch_kline(symbol, period, count)
        if fresh is not None and not fresh.empty:
            merged = kline_store.merge_and_save(symbol, period, fresh.to_dict("records"))
            if not merged.empty:
                return merged.tail(count).reset_index(drop=True)
        if cached is not None and not cached.empty:
            logger.info("回源失败, 降级返回缓存: %s %s", symbol, period)
            return cached.tail(count).reset_index(drop=True)
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])

    async def _fetch_kline(self, symbol: str, period: str, count: int) -> pd.DataFrame | None:
        for name in self._active_order():
            if self._health[name].is_circuit_open():
                continue
            src = self._sources[name]
            try:
                t0 = time.perf_counter()
                df = await asyncio.wait_for(src.get_kline(symbol, period, count), timeout=FETCH_TIMEOUT)
                latency = (time.perf_counter() - t0) * 1000
                if df is not None and not df.empty:
                    self._record(name, True, latency)
                    self._bump_preference(name)
                    return df
                self._record(name, False, latency)
            except TimeoutError:
                self._record(name, False, 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 取 K线失败 %s: %s", name, symbol, exc)
                self._record(name, False, 0.0)
        return None

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        """实时行情: 内存 LRU 5s, 未命中回源."""
        if not symbols:
            return []
        cached = quote_cache.get(symbols)
        missing = [s for s in symbols if s not in cached]
        if missing:
            for name in self._active_order():
                if self._health[name].is_circuit_open():
                    continue
                src = self._sources[name]
                try:
                    quotes = await asyncio.wait_for(src.get_realtime_quote(missing), timeout=8)
                    if quotes:
                        self._record(name, True, 0.0)
                        self._bump_preference(name)
                        quote_cache.set(quotes)
                        for q in quotes:
                            cached[q.symbol] = q
                        missing = [s for s in missing if s not in cached]
                        if not missing:
                            break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("数据源 %s 实时行情失败: %s", name, exc)
                    self._record(name, False, 0.0)
        return [cached[s] for s in symbols if s in cached]

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        for name in self._active_order():
            if self._health[name].is_circuit_open():
                continue
            src = self._sources[name]
            try:
                stocks = await asyncio.wait_for(src.get_stock_list(market), timeout=30)
                if stocks:
                    self._record(name, True, 0.0)
                    return stocks
                self._record(name, False, 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 股票列表失败: %s", name, exc)
                self._record(name, False, 0.0)
        return []

    async def test_source(self, name: str) -> dict:
        """手动测试某数据源: 探活 + 取一只票的 K线验证."""
        src = self._sources.get(name)
        if src is None:
            return {"name": name, "ok": False, "rows": 0, "latency_ms": 0.0, "error": "源不存在或未启用"}
        try:
            ok = await asyncio.wait_for(src.health_check(), timeout=15)
            latency, rows = 0.0, 0
            if ok:
                t0 = time.perf_counter()
                df = await asyncio.wait_for(src.get_kline("000001", "daily", 5), timeout=15)
                latency = (time.perf_counter() - t0) * 1000
                rows = 0 if df is None else len(df)
            self._record(name, ok, latency)
            return {"name": name, "ok": ok, "rows": rows, "latency_ms": round(latency, 1), "error": ""}
        except Exception as exc:  # noqa: BLE001
            self._record(name, False, 0.0)
            return {"name": name, "ok": False, "rows": 0, "latency_ms": 0.0, "error": str(exc)}

    @staticmethod
    def _cache_fresh(df: pd.DataFrame, period: str) -> bool:
        """日线: 最后日期 >= 今天即视为新鲜(盘后增量另行调度)."""
        if df is None or df.empty:
            return False
        last = str(df.iloc[-1]["date"])[:10]
        if period == "daily":
            return last >= dt.date.today().strftime("%Y-%m-%d")
        if period == "weekly":
            return True  # 周线低频, 直接信任缓存
        # 分钟线: 5 分钟内新鲜
        try:
            last_dt = dt.datetime.fromisoformat(last)
            return (dt.datetime.now() - last_dt).total_seconds() < 300
        except ValueError:
            return False


# 全局单例
data_source_manager = DataSourceManager()
