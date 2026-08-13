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


def test_kline_store_merge_dedup_cross_format(tmp_engine):
    """回归: 不同源日期格式不一致('2025-01-01' vs '2025-01-01 15:00')必须按归一化去重,
    否则同一天会存两条导致缓存膨胀与指标污染."""
    store = KlineStore()
    old = [
        {"date": "2025-01-01 15:00", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 1000.0, "amount": 10500.0},
        {"date": "2025-01-02 15:00", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 1000.0, "amount": 10500.0},
    ]
    store.save("600111", "daily", old)
    fresh = [  # 腾讯格式: 无时间部分, 且 01-01 数值更新
        {"date": "2025-01-01", "open": 10.1, "high": 11.1, "low": 9.1,
         "close": 10.6, "volume": 1100.0, "amount": 0.0},
        {"date": "2025-01-03", "open": 10.2, "high": 11.2, "low": 9.2,
         "close": 10.7, "volume": 1200.0, "amount": 0.0},
    ]
    df = store.merge_and_save("600111", "daily", fresh)
    assert len(df) == 3  # 01-01 两条跨格式合并为 1 条(新覆盖旧)
    row_0101 = df[df["date"].str.startswith("2025-01-01")].iloc[0]
    assert row_0101["close"] == 10.6  # 新数据覆盖旧数据
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


async def test_get_kline_force_bypasses_fresh_cache(tmp_engine, monkeypatch):
    """force=True 时即使日线缓存新鲜(最后日期=今天)也重新回源——盘中信号评估场景."""
    import datetime as dt

    import pandas as pd
    from app.core.datasource import manager as mgr_mod
    from app.core.datasource.cache import kline_store

    today = dt.date.today().isoformat()
    rows = [
        {"date": f"2025-01-{i + 1:02d}", "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 1000.0, "amount": 10500.0}
        for i in range(29)
    ] + [{"date": today, "open": 10.0, "high": 11.0, "low": 9.0,
          "close": 10.5, "volume": 1000.0, "amount": 10500.0}]
    kline_store.save("600519", "daily", rows)

    calls = {"n": 0}

    async def fake_fetch(*a, **k):
        calls["n"] += 1
        return pd.DataFrame(rows)

    monkeypatch.setattr(mgr_mod.data_source_manager, "_fetch_kline", fake_fetch)
    # 固定「新鲜判定」为真: 本测试只验证 force 语义, 与交易时段无关(盘中时段规则下不缓存是设计行为)
    monkeypatch.setattr(mgr_mod.DataSourceManager, "_intraday_ttl", staticmethod(lambda now, last, upd: True))

    # 默认: 缓存根数足够且新鲜(最后日期=今天) -> 直接命中, 不回源
    df = await mgr_mod.data_source_manager.get_kline("600519", "daily", 30)
    assert calls["n"] == 0
    assert df.iloc[-1]["date"][:10] == today

    # force=True: 跳过缓存重新回源(盘中评估要最新价), 拉取后仍写缓存
    await mgr_mod.data_source_manager.get_kline("600519", "daily", 30, force=True)
    assert calls["n"] == 1

def test_intraday_ttl_outside_session_hits_intraday_cache():
    """盘后(15:30后): 今日收盘数据可复用; 今日盘中截断/无时点/无今日数据 -> 重拉."""
    import datetime as dt

    from app.core.datasource.manager import DataSourceManager

    tz = dt.timezone(dt.timedelta(hours=8))
    today = "2026-08-11"  # 周二(交易日)
    now = dt.datetime(2026, 8, 11, 17, 0, tzinfo=tz)
    # 缓存是当天 11:00 盘中写入 -> 截断 bar, 重拉收盘数据
    assert DataSourceManager._intraday_ttl(now, today, "2026-08-11 11:00:00") is False
    # 缓存是当天盘后写入 -> 收盘数据, 复用
    assert DataSourceManager._intraday_ttl(now, today, "2026-08-11 16:30:00") is True
    # 无时间戳(迁移前缓存) -> 重拉一次
    assert DataSourceManager._intraday_ttl(now, today, "") is False
    # 最后日期非今天(昨日收盘) -> 重拉今日收盘
    assert DataSourceManager._intraday_ttl(now, "2026-08-10", "2026-08-10 16:00:00") is False


def test_intraday_ttl_during_session():
    """盘中(9:15~11:30 / 13:00~15:30): 不缓存, 每次强制刷新."""
    import datetime as dt

    from app.core.datasource.manager import DataSourceManager

    tz = dt.timezone(dt.timedelta(hours=8))
    today = "2026-08-11"
    # 上午盘中 10:00: 无论缓存何时写入都强制刷新
    assert DataSourceManager._intraday_ttl(
        dt.datetime(2026, 8, 11, 10, 0, tzinfo=tz), today, "2026-08-11 09:55:00") is False
    # 下午盘中 14:00: 同样强制刷新
    assert DataSourceManager._intraday_ttl(
        dt.datetime(2026, 8, 11, 14, 0, tzinfo=tz), today, "2026-08-11 11:30:00") is False
    # 周末 -> 复用最近收盘数据
    assert DataSourceManager._intraday_ttl(
        dt.datetime(2026, 8, 9, 10, 0, tzinfo=tz), "2026-08-08", "2026-08-08 15:30:00") is True


def test_intraday_ttl_lunch_break():
    """午休(11:30~13:00): 有当天数据即可复用; 无当天数据 -> 重拉."""
    import datetime as dt

    from app.core.datasource.manager import DataSourceManager

    tz = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=tz)
    assert DataSourceManager._intraday_ttl(now, "2026-08-11", "2026-08-11 11:25:00") is True
    assert DataSourceManager._intraday_ttl(now, "2026-08-10", "") is False


def test_intraday_ttl_early_morning():
    """盘前(9:15 前): 复用最近收盘数据, 无需强制刷新."""
    import datetime as dt

    from app.core.datasource.manager import DataSourceManager

    tz = dt.timezone(dt.timedelta(hours=8))
    # 盘前 08:30, 缓存为昨日收盘 -> 复用
    assert DataSourceManager._intraday_ttl(
        dt.datetime(2026, 8, 11, 8, 30, tzinfo=tz), "2026-08-10", "2026-08-10 16:00:00") is True
