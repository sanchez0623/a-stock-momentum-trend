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
# 盘中日线缓存 TTL: 交易日 9:15~15:05 期间, 最后日期=今天的缓存超过该秒数视为过期重拉
# (盘中 bar 实时变化, 缓存只记日期会把当天早盘价当新鲜; 盘后不设 TTL)
INTRADAY_TTL_SEC = 600


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
        self._heal_priority()
        config_manager.register_listener(self._on_config_change)

    def _heal_priority(self) -> None:
        """已注册但不在 priority 里的源补到队尾.

        旧库里持久化的 priority 是"整列表覆盖"语义, 新增源(如 baostock)不会自动出现,
        会导致源注册了却永远轮不到. 这里做一次自愈, 保证新增源至少能被尝试.
        """
        missing = [n for n in self._sources if n not in self._priority]
        if missing:
            self._priority.extend(missing)
            logger.info("数据源优先级自愈: 补入 %s -> %s", missing, self._priority)

    def _on_config_change(self, cfg: dict) -> None:
        ds_cfg = cfg.get("数据源", {})
        self._proxy_pool = ds_cfg.get("proxy_pool", [])
        new_priority = list(ds_cfg.get("priority", []))
        # 保留仍启用的顺序, 移除禁用的
        enabled = ds_cfg.get("enabled", {})
        self._priority = [n for n in new_priority if self._sources.get(n) and enabled.get(n, True)]
        self._heal_priority()
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

    def source_circuit_open(self, name: str) -> bool:
        """某源当前是否处于熔断期(供业务模块判断是否跳过该源专属逻辑, 如分类刷新)."""
        st = self._health.get(name)
        return st.is_circuit_open() if st else False

    def report_source(self, name: str, ok: bool, latency_ms: float = 0.0) -> None:
        """业务模块在绕过通用取数方法(直连某源专属接口)时, 仍可回报该源健康, 接入统一熔断.

        例: classification 直连 akshare 专属接口(申万/板块), 其成败也应计入 akshare 熔断,
        而非游离于健康体系之外. 源未注册(如被禁用)时安全跳过.
        """
        if name not in self._health:
            return
        self._record(name, ok, latency_ms)

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
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120,
                        secid: str | None = None, prefer_long: bool = False,
                        force: bool = False) -> pd.DataFrame:
        """K线: 缓存优先, 缺失/过期回源, 合并后返回最近 count 条.

        secid: 显式 secid(如东财指数 "1.000001"). 提供时跳过按代码前缀推断交易所,
        用于拉取指数 K 线(普通代码前缀规则对指数会错位).

        prefer_long: 长历史优先模式(补拉历史用). 默认 False 行为不变(首个源成功即返回);
        True 时根数不足 count 的源不终止尝试, 继续试能给更多历史的源(baostock/东财),
        全部不足则返回根数最多的结果. 该模式不调整全局源偏好, 避免污染日常取数顺序.

        force: 强制跳过缓存重新回源(如盘中信号评估需要最新价, 日线缓存只记日期
        会把当天早盘的 bar 视为新鲜, 导致信号基于旧数据). 拉取后仍写缓存.
        """
        cached = kline_store.get_dataframe(symbol, period)
        # 缓存命中条件含盘中 TTL 检查(_intraday_fresh: 收盘后恒新鲜)
        if (not force and cached is not None and len(cached) >= count
                and self._cache_fresh(cached, period) and self._intraday_fresh(symbol, period, cached)):
            return cached.tail(count).reset_index(drop=True)
        fresh = await self._fetch_kline(symbol, period, count, secid=secid, prefer_long=prefer_long)
        if fresh is not None and not fresh.empty:
            merged = kline_store.merge_and_save(symbol, period, fresh.to_dict("records"))
            if not merged.empty:
                return merged.tail(count).reset_index(drop=True)
        if cached is not None and not cached.empty:
            logger.info("回源失败, 降级返回缓存: %s %s", symbol, period)
            return cached.tail(count).reset_index(drop=True)
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])

    async def get_index_kline(self, secid: str, period: str = "daily", count: int = 250) -> pd.DataFrame | None:
        """指数 K 线: 显式 secid(东财格式, 如 沪深300="0.000300"/创业板指="0.399006"/上证指数="1.000001").

        取数顺序: 东财 -> 具备 index_kline 能力的源(baostock, 自动做 secid->代码 转换).
        绕过个股缓存(用 idx: 前缀独立缓存), 供择时闸门使用.
        全部失败才返回 None(闸门应降级为不生效).
        """
        cache_key = f"idx:{secid}"
        cached = kline_store.get_dataframe(cache_key, period)
        if cached is not None and len(cached) >= count:
            return cached.tail(count).reset_index(drop=True)

        # 候选源: 东财优先(secid 原生格式), 其次任何声明 index_kline 能力的源
        candidates: list[str] = []
        if "eastmoney" in self._sources:
            candidates.append("eastmoney")
        candidates += [n for n in self._capable_sources("index_kline") if n not in candidates]
        if not candidates:
            logger.warning("择时闸门: 无可用指数源, 无法获取 %s", secid)
            return None

        for name in candidates:
            src = self._sources[name]
            try:
                df = await asyncio.wait_for(
                    src.get_kline(symbol="", period=period, count=count, secid=secid),
                    timeout=FETCH_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("择时闸门: 指数 K 线获取失败 %s@%s: %s", secid, name, exc)
                continue
            if df is not None and not df.empty:
                if name != "eastmoney":
                    logger.info("择时闸门: 指数 %s 由备用源 %s 提供(%d 根)", secid, name, len(df))
                kline_store.merge_and_save(cache_key, period, df.to_dict("records"))
                return df.tail(count).reset_index(drop=True)
        return None

    # ------------------------------------------------------------ 可选能力路由
    def _capable_sources(self, capability: str) -> list[str]:
        """按当前优先级返回声明了该能力的源名."""
        return [n for n in self._active_order()
                if capability in getattr(self._sources[n], "supports", ())]

    def capabilities(self) -> dict[str, list[str]]:
        """能力 -> 提供该能力的源列表(供前端/诊断展示)."""
        out: dict[str, list[str]] = {}
        for name in self._active_order():
            for cap in getattr(self._sources[name], "supports", ()):
                out.setdefault(cap, []).append(name)
        return out

    async def _try_capability(self, capability: str, method: str, *args,
                              timeout: float = 60.0, allow_empty: bool = False, **kwargs):
        """依次尝试具备该能力的源, 首个成功即返回; 全部失败返回 None."""
        names = self._capable_sources(capability)
        if not names:
            logger.debug("无数据源提供能力 %s", capability)
            return None
        for name in names:
            if self._health[name].is_circuit_open():
                continue
            src = self._sources[name]
            try:
                t0 = time.perf_counter()
                res = await asyncio.wait_for(getattr(src, method)(*args, **kwargs), timeout=timeout)
                latency = (time.perf_counter() - t0) * 1000
                if res or allow_empty:
                    self._record(name, True, latency)
                    return res
            except NotImplementedError:
                continue
            except TimeoutError:
                logger.warning("数据源 %s 能力 %s 超时", name, capability)
                self._record(name, False, 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 能力 %s 失败: %s", name, capability, exc)
                self._record(name, False, 0.0)
        return None

    async def get_index_constituents(self, index_key: str) -> list[StockInfo]:
        """指数成分股(hs300/zz500/sz50). 无源提供时返回空列表."""
        res = await self._try_capability("constituents", "get_index_constituents", index_key, timeout=60)
        return res or []

    async def get_industry_map(self, symbols: list[str] | None = None) -> dict[str, dict[str, str]]:
        """全市场行业映射(证监会行业等). 无源提供时返回空字典."""
        res = await self._try_capability("industry", "get_industry_map", symbols, timeout=120)
        return res or {}

    async def get_fundamentals(self, symbol: str, *, full: bool = False):
        """单只股票最近一期基本面. 无源提供/无数据返回 None."""
        return await self._try_capability("fundamental", "get_fundamentals", symbol, full=full, timeout=60)

    async def get_earnings_events(self, symbol: str, start_date: str, end_date: str) -> list:
        """区间内业绩预告/快报. 无源提供时返回空列表."""
        res = await self._try_capability("earnings", "get_earnings_events", symbol,
                                         start_date, end_date, timeout=60, allow_empty=True)
        return res or []

    async def _fetch_kline(self, symbol: str, period: str, count: int,
                           secid: str | None = None, prefer_long: bool = False) -> pd.DataFrame | None:
        best: pd.DataFrame | None = None
        best_name = ""
        for name in self._active_order():
            if self._health[name].is_circuit_open():
                continue
            src = self._sources[name]
            # 源明确不支持该周期(如 baostock 无分钟线): 直接跳过, 不计失败/不触发熔断
            if not src.supports_period(period):
                continue
            try:
                t0 = time.perf_counter()
                df = await asyncio.wait_for(src.get_kline(symbol, period, count, secid=secid), timeout=FETCH_TIMEOUT)
                latency = (time.perf_counter() - t0) * 1000
                if df is not None and not df.empty:
                    self._record(name, True, latency)
                    if not prefer_long or len(df) >= count:
                        self._bump_preference(name)
                        return df
                    # prefer_long: 根数不足继续试更全的源, 先记录根数最多的
                    if best is None or len(df) > len(best):
                        best, best_name = df, name
                else:
                    self._record(name, False, latency)
            except TimeoutError:
                self._record(name, False, 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 取 K线失败 %s: %s", name, symbol, exc)
                self._record(name, False, 0.0)
        if best is not None:
            # 不 bump 偏好: 补拉模式不应污染日常取数顺序
            logger.info("prefer_long 无源满足 %d 根, 返回最长 %d 根(%s): %s",
                        count, len(best), best_name, symbol)
            return best
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
                if not getattr(src, "supports_realtime", True):
                    continue  # 盘后型数据源(baostock)不参与实时行情
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
                    self._bump_preference(name)
                    return stocks
                self._record(name, False, 0.0)
            except NotImplementedError:
                # 源未实现全量列表(如 mootdx/tencent): 跳过, 不计入失败/熔断,
                # 避免污染其 K线/实时价能力(熔断是 per-source 而非 per-method)
                continue
            except TimeoutError:
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

    def _intraday_fresh(self, symbol: str, period: str, df: pd.DataFrame) -> bool:
        """盘中缓存新鲜判定: 交易日盘中 + 日线 + 最后日期=今天 时, 写入时间距现在须 ≤ TTL.

        解决盘中选股拿到「当天第一次拉取时点」旧价的问题;
        盘前/盘后/周末/无时间戳(迁移前旧缓存)一律视为新鲜.
        """
        if period != "daily" or df is None or df.empty:
            return True
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        if now.weekday() >= 5:
            return True
        if not (dt.time(9, 15) <= now.time() <= dt.time(15, 5)):
            return True  # 盘外不设 TTL(收盘后数据不再变化)
        if str(df.iloc[-1]["date"])[:10] != now.strftime("%Y-%m-%d"):
            return True  # 缓存最后日期非今天(盘前旧数据), 维持原判定
        updated = kline_store.get_updated_at(symbol, period)
        if not updated:
            return True  # 无时间戳(旧库迁移), 保守视为新鲜, 下次写入即带时间戳
        try:
            updated_dt = dt.datetime.strptime(updated[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=dt.timezone(dt.timedelta(hours=8)))
            return (now - updated_dt).total_seconds() <= INTRADAY_TTL_SEC
        except ValueError:
            return True

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
