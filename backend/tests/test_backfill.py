"""历史 K 线补拉(backfill) 测试."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from app.core import backfill
from app.core.backfill import DEFAULT_TARGET, backfill_history, pending_symbols


def _df(n: int, last_date: str | None = None) -> pd.DataFrame:
    """构造 n 根日 K 线 DataFrame(最后一根日期可指定)."""
    if last_date is None:
        last_date = dt.date.today().isoformat()
    dates = pd.date_range(end=last_date, periods=n, freq="B").strftime("%Y-%m-%d %H:%M")
    return pd.DataFrame({
        "date": dates,
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
        "close": [10.5] * n, "volume": [1e6] * n, "amount": [1e8] * n,
    })


# ---------------------------------------------------------------- _fresh_date
def test_fresh_date_recent_and_old():
    assert backfill._fresh_date(dt.date.today().isoformat())
    assert backfill._fresh_date((dt.date.today() - dt.timedelta(days=9)).isoformat() + " 15:00")
    assert not backfill._fresh_date((dt.date.today() - dt.timedelta(days=40)).isoformat())
    assert not backfill._fresh_date("bad-date")


def test_is_deeply_insufficient():
    """老股但缓存明显不足(源没给全) -> 重拉; 次新/接近目标 -> 已尽力."""
    today = dt.date.today()
    # 老股(首日 600 天前)只缓存 120 根 -> 源没给全, 需重拉
    bars_old = _df(120, today.isoformat()).to_dict("records")
    bars_old[0]["date"] = (today - dt.timedelta(days=600)).isoformat()
    assert backfill._is_deeply_insufficient(bars_old, 260)
    # 次新股 100 根(首日即上市日附近) -> 已尽力
    assert not backfill._is_deeply_insufficient(_df(100, today.isoformat()).to_dict("records"), 260)
    # 达成度 >= 90%(240 根) -> 接近目标, 已尽力
    assert not backfill._is_deeply_insufficient(_df(240, today.isoformat()).to_dict("records"), 260)


# ---------------------------------------------------------------- pending_symbols
def test_filter_symbols_excludes_92():
    """92 开头(北交所新代码段)不参与补拉."""
    syms = ["920001", "920999", "600111", "300750", "832000"]
    assert backfill._filter_symbols(syms) == ["600111", "300750", "832000"]
    assert backfill._filter_symbols(["920001"]) == []


def test_pending_symbols(monkeypatch):
    monkeypatch.setattr(backfill, "_all_symbols", lambda: ["a", "b", "c", "d"])
    monkeypatch.setattr(backfill, "_load_cache_status",
                        lambda target: {"a": "ok", "b": "stale", "c": "missing"})
    assert pending_symbols(DEFAULT_TARGET) == ["b", "c", "d"]


def test_load_cache_status_classify(monkeypatch):
    """达标 / 新鲜但短(尽力) / 陈旧 / 损坏 四类缓存分类."""
    today = dt.date.today().isoformat()
    old = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    rows = [
        ("s_ok", _df(300, today).to_dict("records"), "ok"),
        ("s_short_fresh", _df(100, today).to_dict("records"), "ok"),      # 次新股, 尽力
        ("s_stale", _df(100, old).to_dict("records"), "stale"),
        ("s_bad", "not-json", "stale"),
        ("s_empty", "[]", "missing"),
    ]
    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec(self, stmt):
            class _R:
                def all(self):
                    return [(s, backfill.json.dumps(r) if not isinstance(r, str) else r) for s, r, _ in rows]
            return _R()

    monkeypatch.setattr(backfill.db, "engine", object())
    monkeypatch.setattr(backfill, "Session", FakeSession)
    status = backfill._load_cache_status(260)
    assert status["s_ok"] == "ok"
    assert status["s_short_fresh"] == "ok"
    assert status["s_stale"] == "stale"
    assert status["s_bad"] == "stale"
    assert status["s_empty"] == "missing"


# ---------------------------------------------------------------- backfill_history
async def _fake_get_kline_router(symbol: str, period: str, count: int, prefer_long: bool = False):
    today = dt.date.today().isoformat()
    old = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    if symbol == "ok1":
        return _df(count, today)
    if symbol == "ok2":
        return _df(count, today)
    if symbol == "short1":            # 新鲜但不足 -> insufficient
        return _df(100, today)
    if symbol == "stale1":            # 陈旧且不足 -> failed
        return _df(100, old)
    if symbol == "err1":
        raise RuntimeError("boom")
    return None                       # 无数据 -> failed


@pytest.mark.asyncio
async def test_backfill_history_classify(monkeypatch):
    monkeypatch.setattr(backfill.data_source_manager, "get_kline", _fake_get_kline_router)
    stats = await backfill_history(
        target=DEFAULT_TARGET, concurrency=2, symbols=["ok1", "ok2", "short1", "stale1", "err1"],
        force=True,
    )
    assert stats["done"] == 2
    assert [x["symbol"] for x in stats["insufficient"]] == ["short1"]
    assert {x["symbol"] for x in stats["failed"]} == {"stale1", "err1"}
    assert stats["pending"] == 5


@pytest.mark.asyncio
async def test_backfill_history_progress_cb(monkeypatch):
    monkeypatch.setattr(backfill.data_source_manager, "get_kline", _fake_get_kline_router)
    calls: list[tuple[int, int]] = []
    await backfill_history(
        target=DEFAULT_TARGET, concurrency=1, symbols=["ok1", "ok2"],
        force=True, progress_cb=lambda d, t: calls.append((d, t)),
    )
    assert calls and calls[-1] == (2, 2)


@pytest.mark.asyncio
async def test_backfill_history_no_pending(monkeypatch):
    """无待补时不调数据源, 直接返回空统计."""
    called = {"n": 0}

    async def _fake(symbol, period, count, prefer_long=False):
        called["n"] += 1
        return _df(count)

    monkeypatch.setattr(backfill.data_source_manager, "get_kline", _fake)
    monkeypatch.setattr(backfill, "_all_symbols", lambda: ["a", "b"])
    monkeypatch.setattr(backfill, "_load_cache_status", lambda target: {"a": "ok", "b": "ok"})
    stats = await backfill_history(target=DEFAULT_TARGET)
    assert stats["pending"] == 0
    assert stats["done"] == 0
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_backfill_uses_prefer_long(monkeypatch):
    """补拉调用 get_kline 必须走 prefer_long=True(长历史优先模式)."""
    seen: dict = {}

    async def _fake(symbol, period, count, prefer_long=False):
        seen["prefer_long"] = prefer_long
        return _df(count)

    monkeypatch.setattr(backfill.data_source_manager, "get_kline", _fake)
    await backfill_history(target=DEFAULT_TARGET, concurrency=1, symbols=["ok1"], force=True)
    assert seen.get("prefer_long") is True


# ---------------------------------------------------------------- manager._fetch_kline prefer_long
class _FakeSource:
    """最小源 mock: 固定返回 n 根 K 线."""

    def __init__(self, name: str, n: int):
        self.name = name
        self.n = n

    def supports_period(self, period: str) -> bool:
        return True

    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None):
        return _df(self.n)


@pytest.mark.asyncio
async def test_fetch_kline_prefer_long_picks_deepest():
    """默认模式首个成功即返回(50根); prefer_long 继续尝试, 返回根数最多的(300根)."""
    from app.core.datasource.manager import DataSourceManager

    m = DataSourceManager()
    m.register(_FakeSource("shallow", 50))
    m.register(_FakeSource("deep", 300))
    m._priority = ["shallow", "deep"]

    df = await m._fetch_kline("000001", "daily", 260, prefer_long=False)
    assert len(df) == 50

    df = await m._fetch_kline("000001", "daily", 260, prefer_long=True)
    assert len(df) == 300


@pytest.mark.asyncio
async def test_fetch_kline_prefer_long_falls_back_to_best():
    """prefer_long 且所有源都不足时, 返回根数最多的结果而非 None."""
    from app.core.datasource.manager import DataSourceManager

    m = DataSourceManager()
    m.register(_FakeSource("a", 50))
    m.register(_FakeSource("b", 120))
    m._priority = ["a", "b"]

    df = await m._fetch_kline("000001", "daily", 260, prefer_long=True)
    assert df is not None
    assert len(df) == 120
