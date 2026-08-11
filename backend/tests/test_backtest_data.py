"""回测专用数据通道单测(方案 v2 §4): 冻结快照 / 区间补拉 / 降级."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
from app.core.backtest import data
from app.models.models import BacktestKline, KlineCache
from sqlalchemy import func
from sqlmodel import select


def _fake_fetch_factory():
    """假 baostock 区间拉取: 每次调用价格基准递增(模拟复权基准变化), 记录调用参数.

    返回 (fetch, calls). fetch(symbol, start, end, adjustflag, secid) -> DataFrame.
    """
    calls: list[tuple] = []
    counter = [0]

    def _fetch(symbol: str, start_date: str, end_date: str,
               adjustflag: str = "2", secid: str | None = None) -> pd.DataFrame:
        calls.append((symbol, start_date, end_date, adjustflag, secid))
        counter[0] += 1
        dates = pd.bdate_range(start_date, end_date)
        n = len(dates)
        seed = float(counter[0]) * 100.0
        close = seed + np.arange(n) * 0.5
        return pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": close, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": np.full(n, 1_000_000.0), "amount": close * 1_000_000.0,
        })

    return _fetch, calls


def test_ensure_range_first_fetch_writes_snapshot(tmp_engine, monkeypatch):
    """首次拉取: source=baostock, 行全部落库冻结快照."""
    fetch, calls = _fake_fetch_factory()
    monkeypatch.setattr(data, "fetch_daily_range", fetch)

    r = data.backtest_data.ensure_range("600000", "2024-01-01", "2024-06-30")
    assert r["source"] == "baostock"
    assert r["fetched"] > 0 and r["rows"] == r["fetched"]
    assert len(calls) == 1
    assert calls[0][0] == "600000" and calls[0][1] == "2024-01-01" and calls[0][2] == "2024-06-30"

    with data.db.session_scope() as s:
        n = s.exec(select(func.count()).select_from(BacktestKline)).one()
    assert n == r["rows"]
    assert n > 100  # 半年工作日量级


def test_frozen_snapshot_no_refetch(tmp_engine, monkeypatch):
    """冻结语义: 同区间二次调用命中快照, 不重复拉取."""
    fetch, calls = _fake_fetch_factory()
    monkeypatch.setattr(data, "fetch_daily_range", fetch)

    ds = data.backtest_data
    r1 = ds.ensure_range("600000", "2024-01-01", "2024-06-30")
    r2 = ds.ensure_range("600000", "2024-01-01", "2024-06-30")
    assert r2["source"] == "backtest_kline"
    assert r2["fetched"] == 0 and r2["rows"] == r1["rows"]
    assert len(calls) == 1


def test_extended_range_refetch_keeps_frozen_prices(tmp_engine, monkeypatch):
    """区间扩大触发补拉: 只补缺失日期, 已存在行价格不被复权基准变化覆盖(冻结)."""
    fetch, calls = _fake_fetch_factory()
    monkeypatch.setattr(data, "fetch_daily_range", fetch)

    ds = data.backtest_data
    r1 = ds.ensure_range("600000", "2024-01-01", "2024-03-31")   # 第 1 次拉(seed=100)
    r2 = ds.ensure_range("600000", "2024-01-01", "2024-06-30")   # 第 2 次拉(seed=200)

    assert r2["source"] == "baostock"
    assert r2["rows"] > r1["rows"], "区间扩大后行数应增加"
    assert len(calls) == 2

    with data.db.session_scope() as s:
        rows = s.exec(select(BacktestKline).where(BacktestKline.symbol == "600000")).all()
    by_date = {r.date: r for r in rows}
    jan_dates = sorted(d for d in by_date if d.startswith("2024-01"))
    assert jan_dates, "应有 1 月行"
    # 1 月行是第 1 次拉取(seed=100)写入, 第 2 次(seed=200)不得覆盖
    first_close = by_date[jan_dates[0]].close
    assert abs(first_close - 100.0) < 1.0, f"已有行价格应保持首次快照值, got {first_close}"
    # 4 月行是第 2 次补拉(seed=200 基准 + 递增), 价格应明显高于第一次基准
    apr_dates = sorted(d for d in by_date if d.startswith("2024-04"))
    assert apr_dates, "应有 4 月补拉行"
    assert 200.0 < by_date[apr_dates[0]].close < 300.0


def test_force_refetch_overwrites(tmp_engine, monkeypatch):
    """force=True 显式重拉并覆盖已有行(诊断/修复路径)."""
    fetch, calls = _fake_fetch_factory()
    monkeypatch.setattr(data, "fetch_daily_range", fetch)

    ds = data.backtest_data
    ds.ensure_range("600000", "2024-01-01", "2024-03-31")       # seed=100
    r = ds.ensure_range("600000", "2024-01-01", "2024-03-31", force=True)  # seed=200
    assert r["source"] == "baostock" and r["fetched"] > 0
    assert len(calls) == 2

    with data.db.session_scope() as s:
        rows = s.exec(select(BacktestKline).where(BacktestKline.symbol == "600000")).all()
    by_date = {r.date: r for r in rows}
    jan_dates = sorted(d for d in by_date if d.startswith("2024-01"))
    # force 重拉后同日期行被覆盖为 seed=200 的价格
    assert abs(by_date[jan_dates[0]].close - 200.0) < 1.0


def test_fallback_to_kline_cache(tmp_engine, monkeypatch):
    """baostock 拉取抛异常 -> 降级实盘缓存并标注未冻结."""
    def _boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("baostock down")
    monkeypatch.setattr(data, "fetch_daily_range", _boom)

    dates = pd.bdate_range("2024-01-01", "2024-06-30")
    n = len(dates)
    close = 10.0 + np.arange(n) * 0.1
    rows = [
        {"date": d, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c,
         "volume": 1e6, "amount": c * 1e6}
        for d, c in zip(dates.strftime("%Y-%m-%d"), close, strict=True)
    ]
    with data.db.session_scope() as s:
        s.add(KlineCache(symbol="600000", period="daily", ohlcv_json=json.dumps(rows), date=rows[-1]["date"]))
        s.commit()

    r = data.backtest_data.ensure_range("600000", "2024-01-01", "2024-06-30")
    assert r["source"] == "kline_cache"
    assert r["rows"] > 100
    assert "未冻结" in r["note"]


def test_no_data_any_source(tmp_engine, monkeypatch):
    """baostock 返回空且实盘缓存无数据 -> source=none."""
    monkeypatch.setattr(data, "fetch_daily_range",
                        lambda *_a, **_k: pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"]))
    r = data.backtest_data.ensure_range("600000", "2024-01-01", "2024-06-30")
    assert r["source"] == "none" and r["rows"] == 0


def test_warmup_multi_and_status(tmp_engine, monkeypatch):
    """批量预热: 逐只落库, 单只失败不中断; 状态汇总准确."""
    fetch, _ = _fake_fetch_factory()

    def _fetch(symbol: str, start_date: str, end_date: str,
               adjustflag: str = "2", secid: str | None = None) -> pd.DataFrame:
        if symbol == "830001":  # 北交所无覆盖 -> 空
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        return fetch(symbol, start_date, end_date, adjustflag, secid)

    monkeypatch.setattr(data, "fetch_daily_range", _fetch)
    rep = data.backtest_data.warmup(["600000", "000001", "830001"], "2024-01-01", "2024-06-30")
    assert rep["meta"]["symbols"] == 3
    assert rep["results"]["600000"]["source"] == "baostock"
    assert rep["results"]["830001"]["source"] == "none"

    st = data.backtest_data.status()
    assert st["symbols"] == 2
    assert st["adjusts"] == ["qfq"]
    assert st["last_fetched_at"]


def test_resolve_range_defaults():
    """默认区间: end=今天, start 前推约 3 年并含预热回退."""
    start, end = data.resolve_range("", "")
    assert end == dt.date.today().strftime("%Y-%m-%d")
    d0 = dt.date.fromisoformat(start)
    assert d0 < dt.date.fromisoformat(end) - dt.timedelta(days=1000)
    # 显式区间原样返回
    assert data.resolve_range("2024-01-01", "2024-06-30") == ("2024-01-01", "2024-06-30")


def test_load_index_uses_idx_symbol(tmp_engine, monkeypatch):
    """基准指数: 内部用 idx:secid 前缀 + adjust=raw 落库."""
    fetch, calls = _fake_fetch_factory()
    monkeypatch.setattr(data, "fetch_daily_range", fetch)

    r = data.backtest_data.load_index("0.000300", "2024-01-01", "2024-06-30")
    assert r["source"] == "baostock" and r["rows"] > 100
    assert calls[0][0] == "idx:0.000300"
    assert calls[0][4] == "0.000300", "secid 应转发给 baostock 指数代码转换"

    st = data.backtest_data.status()
    assert st["adjusts"] == ["raw"]
