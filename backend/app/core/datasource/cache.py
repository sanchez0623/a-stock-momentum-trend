"""缓存层(方案 §3.4).

- K线缓存: SQLite 表 kline_cache, 每个 (symbol, period) 存一整段 JSON, 记录最后日期用于增量
- 实时行情: 内存 LRU, TTL 5s
"""

from __future__ import annotations

import json
import logging
import time

import pandas as pd
from sqlmodel import Session, select

from app import db
from app.core.datasource.base import Quote
from app.models.models import KlineCache

logger = logging.getLogger(__name__)

# 单段缓存最大条数(日线保留 ~800 条, 分钟线 ~2000)
_MAX_ROWS = {"daily": 800, "weekly": 600, "1m": 2000, "5m": 2000, "15m": 2000, "30m": 2000, "60m": 2000}


class KlineStore:
    """SQLite K线缓存."""

    def load(self, symbol: str, period: str) -> list[dict] | None:
        """读取缓存段, 无则返回 None."""
        try:
            with Session(db.engine) as s:
                stmt = select(KlineCache).where(
                    KlineCache.symbol == symbol, KlineCache.period == period
                )
                row = s.exec(stmt).first()
                if row is None:
                    return None
                rows = json.loads(row.ohlcv_json)
                return rows if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("KlineStore.load 失败: %s", exc)
            return None

    def save(self, symbol: str, period: str, rows: list[dict]) -> None:
        """整段覆盖保存(调用方负责合并去重)."""
        try:
            with Session(db.engine) as s:
                stmt = select(KlineCache).where(
                    KlineCache.symbol == symbol, KlineCache.period == period
                )
                row = s.exec(stmt).first()
                if row is None:
                    row = KlineCache(symbol=symbol, period=period, ohlcv_json="[]")
                    s.add(row)
                row.ohlcv_json = json.dumps(rows[-_MAX_ROWS.get(period, 800):], ensure_ascii=False)
                row.date = rows[-1]["date"] if rows else ""
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("KlineStore.save 失败: %s", exc)

    def get_dataframe(self, symbol: str, period: str) -> pd.DataFrame | None:
        rows = self.load(symbol, period)
        if not rows:
            return None
        return pd.DataFrame(rows)

    def list_symbols(self, period: str = "daily") -> list[str]:
        """列出缓存了指定周期且有数据(非空段)的 symbol, 升序. 供回测/预热统计用."""
        try:
            with Session(db.engine) as s:
                stmt = select(KlineCache.symbol).where(
                    KlineCache.period == period,
                    KlineCache.ohlcv_json.not_in(["[]", ""]),
                )
                # 注意: 单列查询返回标量 str(非 Row 元组), 直接排序即可
                return sorted(r for r in s.exec(stmt).all())
        except Exception as exc:  # noqa: BLE001
            logger.warning("KlineStore.list_symbols 失败: %s", exc)
            return []

    def merge_and_save(self, symbol: str, period: str, fresh_rows: list[dict]) -> pd.DataFrame:
        """缓存合并: 旧缓存 + 新取, 按 date 去重(新覆盖旧), 升序返回 DataFrame."""
        old = self.load(symbol, period) or []
        by_date: dict[str, dict] = {r["date"]: r for r in old}
        for r in fresh_rows:
            by_date[r["date"]] = r
        merged = sorted(by_date.values(), key=lambda r: r["date"])
        self.save(symbol, period, merged)
        return pd.DataFrame(merged)


class QuoteCache:
    """实时行情内存缓存(TTL 默认 5s)."""

    def __init__(self, ttl: float = 5.0, maxsize: int = 2000) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._data: dict[str, tuple[float, Quote]] = {}

    def get(self, symbols: list[str]) -> dict[str, Quote]:
        now = time.time()
        out: dict[str, Quote] = {}
        for sym in symbols:
            item = self._data.get(sym)
            if item and now - item[0] < self._ttl:
                out[sym] = item[1]
        return out

    def set(self, quotes: list[Quote]) -> None:
        now = time.time()
        for q in quotes:
            self._data[q.symbol] = (now, q)
        if len(self._data) > self._maxsize:
            # 简单淘汰: 清掉最旧的一半
            items = sorted(self._data.items(), key=lambda kv: kv[1][0])
            for k, _ in items[: len(items) // 2]:
                self._data.pop(k, None)


kline_store = KlineStore()
quote_cache = QuoteCache()
