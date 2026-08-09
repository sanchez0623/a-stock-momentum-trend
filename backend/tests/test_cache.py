"""缓存层单测: K线 SQLite 缓存 + 实时行情内存缓存."""

from __future__ import annotations

import time

from app.core.datasource.base import Quote
from app.core.datasource.cache import KlineStore, QuoteCache


def _sample_rows(n: int = 30) -> list[dict]:
    return [
        {"date": f"2025-01-{i + 1:02d}", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 1000.0, "amount": 10500.0}
        for i in range(n)
    ]


def test_kline_store_roundtrip(tmp_engine):
    store = KlineStore()
    rows = _sample_rows()
    store.save("300750", "daily", rows)
    loaded = store.load("300750", "daily")
    assert loaded == rows
    df = store.get_dataframe("300750", "daily")
    assert len(df) == 30
    assert df.iloc[-1]["close"] == 10.5


def test_kline_store_merge_dedup(tmp_engine):
    store = KlineStore()
    old = _sample_rows(10)
    store.save("000001", "daily", old)
    fresh = _sample_rows(20)  # 前 10 条与 old 相同 date, 后 10 条新增
    df = store.merge_and_save("000001", "daily", fresh)
    assert len(df) == 20  # 去重后共 20 条
    assert df["date"].is_monotonic_increasing


def test_kline_store_list_symbols(tmp_engine):
    """list_symbols 只返回非空段且返回完整 symbol(回归: 曾取字符串首字符变 '0')."""
    from app import db
    from app.models.models import KlineCache
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(KlineCache(symbol="000001", period="daily", ohlcv_json='[{"date":"2026-01-02"}]'))
        s.add(KlineCache(symbol="600000", period="daily", ohlcv_json='[{"date":"2026-01-02"}]'))
        s.add(KlineCache(symbol="000002", period="daily", ohlcv_json="[]"))  # 空段应被过滤
        s.add(KlineCache(symbol="300750", period="weekly", ohlcv_json='[{"date":"2026-01-02"}]'))  # 不同周期
        s.commit()
    store = KlineStore()
    syms = store.list_symbols("daily")
    assert syms == ["000001", "600000"]


def test_quote_cache_ttl():
    cache = QuoteCache(ttl=0.1)
    q = Quote(symbol="300750", price=100.0)
    cache.set([q])
    hit = cache.get(["300750"])
    assert "300750" in hit
    time.sleep(0.15)
    assert "300750" not in cache.get(["300750"])


def test_quote_cache_missing_only():
    cache = QuoteCache()
    cache.set([Quote(symbol="300750", price=1.0)])
    hit = cache.get(["300750", "600519"])
    assert "300750" in hit
    assert "600519" not in hit  # 缺失的不返回
