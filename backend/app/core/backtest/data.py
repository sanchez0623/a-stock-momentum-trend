"""回测专用数据通道(方案 v2 §4): backtest_kline 冻结快照 + baostock 区间补拉.

核心语义(与实盘数据隔离):
- **冻结**: 同 (symbol, period, adjust, date) 的行一旦写入不再覆盖(除非显式 force).
  回测区间数据不参与日常增量刷新, 从根上消除「动态前复权 = 未来函数」(§4.3);
- **隔离**: 独立表 backtest_kline, 永不回写实盘 kline_cache;
- **降级**: baostock 拉取失败(网络/北交所无覆盖等)时回退实盘缓存, 并标注
  source="kline_cache"(未冻结, 可能有复权污染, 报告需提示);
- **补拉**: 已有快照覆盖度不足(区间扩大/首尾缺失)时整段重拉, 已存在日期保留(冻结),
  只补缺失日期 —— 快照内自洽, 不因后续复权基准变化而改写历史行.

同步实现: 回测引擎跑在后台线程(to_thread), baostock 调用本身串行锁保护(线程安全),
因此本模块直接同步调用, 无需事件循环.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd
from sqlmodel import select

from app import db
from app.core.datasource.baostock_src import fetch_daily_range
from app.core.datasource.cache import kline_store
from app.models.models import BacktestKline

logger = logging.getLogger(__name__)

DEFAULT_YEARS = 3          # 回测默认区间(自然年)
WARMUP_BARS = 120          # 指标预热根数(MA60/MACD 需要), 起始日期自动回退
ADJUST_DEFAULT = "qfq"     # 回测统一前复权(冻结快照, §4.3)
_DAYS_PER_BAR = 1.6        # 自然日/交易日粗略折算(含节假日缓冲)
_EDGE_TOLERANCE_DAYS = 10  # 快照覆盖度判定缓冲(容忍停牌/最近交易日滞后)
_IDX_PREFIX = "idx:"

_KLINE_COLS = ("open", "high", "low", "close", "volume", "amount")


def _now() -> str:
    """东八区时间戳(与 models._now 口径一致, 避免引私有函数)."""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _f(x: Any, default: float = 0.0) -> float:
    """数值兜底: NaN/None -> default(入库禁止 NaN)."""
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def resolve_range(start: str = "", end: str = "", years: int = DEFAULT_YEARS) -> tuple[str, str]:
    """解析回测区间: 空 end=今天; 空 start=end 前推 years 自然年再回退预热根数.

    返回 (start_date, end_date) YYYY-MM-DD. 预热回退保证 MA60/MACD 等指标有足够历史.
    """
    end_date = (end or dt.date.today().strftime("%Y-%m-%d"))[:10]
    if not start:
        back = int(years * 365.25 + WARMUP_BARS * _DAYS_PER_BAR)
        start_date = (dt.date.fromisoformat(end_date) - dt.timedelta(days=back)).strftime("%Y-%m-%d")
    else:
        start_date = start[:10]
    return start_date, end_date


class BacktestDataStore:
    """回测数据通道: 冻结快照读写 + 区间补拉 + 降级."""

    # ------------------------------------------------------------ 读取
    def load_range(self, symbol: str, start: str, end: str,
                   period: str = "daily", adjust: str = ADJUST_DEFAULT) -> pd.DataFrame | None:
        """读冻结快照中 [start, end] 区间的行, 升序 DataFrame; 无数据返回 None."""
        try:
            with db.session_scope() as s:
                stmt = (
                    select(BacktestKline)
                    .where(BacktestKline.symbol == symbol,
                           BacktestKline.period == period,
                           BacktestKline.adjust == adjust,
                           BacktestKline.date >= start,
                           BacktestKline.date <= end)
                    .order_by(BacktestKline.date)
                )
                rows = s.exec(stmt).all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("backtest_kline 读取失败 %s: %s", symbol, exc)
            return None
        if not rows:
            return None
        return pd.DataFrame([
            {"date": r.date, "open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume, "amount": r.amount}
            for r in rows
        ])

    @staticmethod
    def _covered(df: pd.DataFrame | None, start: str, end: str) -> bool:
        """快照是否覆盖目标区间: 首行不晚于 start+缓冲 且 末行不早于 end-缓冲."""
        if df is None or df.empty:
            return False
        tol = dt.timedelta(days=_EDGE_TOLERANCE_DAYS)
        d_min = dt.date.fromisoformat(str(df["date"].iloc[0])[:10])
        d_max = dt.date.fromisoformat(str(df["date"].iloc[-1])[:10])
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
        return d_min <= s + tol and d_max >= e - tol

    # ------------------------------------------------------------ 核心: 确保区间
    def ensure_range(self, symbol: str, start: str, end: str,
                     period: str = "daily", adjust: str = ADJUST_DEFAULT,
                     force: bool = False, secid: str | None = None) -> dict[str, Any]:
        """确保 [start, end] 区间数据可用, 返回:
        {symbol, start, end, rows, source, fetched, note}
        source: backtest_kline(冻结快照命中) / baostock(本次补拉) / kline_cache(降级)
        secid: 指数场景传入(如 0.000300), 内部转发给 baostock 指数代码转换.
        """
        start, end = resolve_range(start, end)
        if start >= end:
            return {"symbol": symbol, "start": start, "end": end, "rows": 0,
                    "source": "empty", "fetched": 0, "note": "区间非法(start>=end)"}

        existing = self.load_range(symbol, start, end, period, adjust)
        if not force and self._covered(existing, start, end):
            return {"symbol": symbol, "start": start, "end": end, "rows": len(existing),
                    "source": "backtest_kline", "fetched": 0, "note": "冻结快照命中"}

        # ---- 快照缺失/覆盖不足: baostock 整段补拉(已存在日期保留)
        fetched = 0
        try:
            df = fetch_daily_range(symbol, start, end, adjustflag="2", secid=secid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回测数据补拉失败 %s: %s", symbol, exc)
            df = pd.DataFrame()
        if df is not None and not df.empty:
            fetched = self._upsert_rows(symbol, period, adjust, df, overwrite=force)
            existing = self.load_range(symbol, start, end, period, adjust)

        if existing is not None and not existing.empty:
            source = "baostock" if fetched > 0 else "backtest_kline"
            note = f"本次补拉 {fetched} 行" if fetched > 0 else "快照已有覆盖, 无新增"
            return {"symbol": symbol, "start": start, "end": end, "rows": len(existing),
                    "source": source, "fetched": fetched, "note": note}

        # ---- 降级: 实盘缓存(未冻结, 标注风险)
        cached = self._fallback_cache(symbol, start, end, period)
        if cached is not None:
            return {"symbol": symbol, "start": start, "end": end, "rows": len(cached),
                    "source": "kline_cache", "fetched": 0,
                    "note": "baostock 无数据, 降级实盘缓存(未冻结, 可能有复权污染)"}
        return {"symbol": symbol, "start": start, "end": end, "rows": 0,
                "source": "none", "fetched": 0, "note": "所有数据源均无数据"}

    def _fallback_cache(self, symbol: str, start: str, end: str, period: str) -> pd.DataFrame | None:
        """降级读实盘 kline_cache 区间(仅在 baostock 失败时使用)."""
        try:
            df = kline_store.get_dataframe(symbol, period)
            if df is None or df.empty:
                return None
            df = df[(df["date"].astype(str).str[:10] >= start) & (df["date"].astype(str).str[:10] <= end)]
            return df if not df.empty else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("降级读实盘缓存失败 %s: %s", symbol, exc)
            return None

    def _upsert_rows(self, symbol: str, period: str, adjust: str, df: pd.DataFrame,
                     overwrite: bool = False) -> int:
        """批量写入缺失行(冻结: 已存在日期不覆盖); overwrite=True(force 路径)时覆盖价格.

        返回写入/更新的行数. 冻结语义保证常规路径下历史快照不被复权基准变化改写.
        """
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        df = df.drop_duplicates(subset=["date"]).sort_values("date")
        new_dates = set(df["date"].tolist())
        try:
            with db.session_scope() as s:
                existing_rows = {
                    str(r.date): r for r in s.exec(
                        select(BacktestKline).where(
                            BacktestKline.symbol == symbol,
                            BacktestKline.period == period,
                            BacktestKline.adjust == adjust,
                            BacktestKline.date.in_(new_dates),
                        )
                    )
                }
                add: list[BacktestKline] = []
                updated = 0
                for _, r in df.iterrows():
                    d = str(r["date"])
                    vals = dict(
                        open=_f(r.get("open")), high=_f(r.get("high")), low=_f(r.get("low")),
                        close=_f(r.get("close")), volume=_f(r.get("volume")), amount=_f(r.get("amount")),
                    )
                    row = existing_rows.get(d)
                    if row is None:
                        add.append(BacktestKline(
                            symbol=symbol, period=period, adjust=adjust, date=d, **vals,
                        ))
                    elif overwrite:
                        for col, v in vals.items():
                            setattr(row, col, v)
                        row.fetched_at = _now()
                        updated += 1
                if add:
                    s.add_all(add)
                if add or updated:
                    s.commit()
                    logger.info(
                        "backtest_kline %s %s: +%d 新增, %d 覆盖(区间 %s~%s)",
                        symbol, "重拉" if overwrite else "补拉", len(add), updated,
                        df["date"].iloc[0], df["date"].iloc[-1],
                    )
                return len(add) + updated
        except Exception as exc:  # noqa: BLE001
            logger.warning("backtest_kline 写入失败 %s: %s", symbol, exc)
            return 0

    # ------------------------------------------------------------ 指数(基准)
    def load_index(self, secid: str, start: str = "", end: str = "") -> dict[str, Any]:
        """沪深300 等基准指数区间数据(内部 symbol=idx:secid, adjust=raw)."""
        start, end = resolve_range(start, end)
        return self.ensure_range(f"{_IDX_PREFIX}{secid}", start, end,
                                 adjust="raw", secid=secid)

    # ------------------------------------------------------------ 批量预热/状态
    def warmup(self, symbols: list[str], start: str = "", end: str = "",
               force: bool = False) -> dict[str, Any]:
        """批量预热: 逐只 ensure_range, 返回逐 symbol 结果汇总(不中断于单只失败)."""
        start, end = resolve_range(start, end)
        results: dict[str, dict] = {}
        src_count: dict[str, int] = {}
        total_rows = 0
        for sym in dict.fromkeys(symbols):
            r = self.ensure_range(sym, start, end, force=force)
            results[sym] = r
            src_count[r["source"]] = src_count.get(r["source"], 0) + 1
            total_rows += r["rows"]
        meta = {
            "start": start, "end": end, "symbols": len(results),
            "source_distribution": src_count, "total_rows": total_rows,
            "frozen": True,
            "note": "前复权冻结快照: 已拉取日期不再随日常增量刷新(§4.3)",
        }
        return {"meta": meta, "results": results}

    def status(self) -> dict[str, Any]:
        """冻结快照汇总(诊断/前端展示用)."""
        try:
            with db.session_scope() as s:
                row = s.exec(
                    select(BacktestKline.symbol, BacktestKline.adjust)
                    .where(BacktestKline.period == "daily")
                    .distinct()
                ).all()
                symbols = sorted({sym for sym, _ in row})
                adjusts = sorted({adj for _, adj in row})
                last = s.exec(
                    select(BacktestKline.fetched_at).order_by(BacktestKline.fetched_at.desc())
                ).first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("backtest_kline 状态统计失败: %s", exc)
            return {"symbols": 0, "adjusts": [], "last_fetched_at": ""}
        return {
            "symbols": len(symbols),
            "adjusts": adjusts,
            "last_fetched_at": str(last or ""),
            "note": "回测专用前复权冻结快照, 与实盘缓存隔离",
        }


backtest_data = BacktestDataStore()
