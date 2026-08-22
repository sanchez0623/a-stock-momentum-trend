"""日内路径模拟单测(方案 v2 §8 扩展): 路径构造边界 / 盘中触发 / T+1 保护."""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.backtest import path_sim as ps
from app.core.backtest import portfolio as pf
from app.core.backtest.portfolio import MANAGE_SIGNAL, Leg, run_portfolio_backtest


# ---------------------------------------------------------------- 路径构造
def test_path_valid_and_deterministic():
    """路径: 首尾锚定 / 全程边界内 / 段高低自洽 / 同参可复现."""
    o, hi, lo, c = 10.0, 11.5, 9.6, 11.0
    p1 = ps.build_intraday_path(o, hi, lo, c, minutes=10)
    p2 = ps.build_intraday_path(o, hi, lo, c, minutes=10)
    assert p1 == p2, "确定性: 同参结果必须一致"
    assert len(p1) == 24, "10 分钟粒度 = 240/10 = 24 段"
    assert ps.validate_path(p1, o, hi, lo, c)

    # 涨/跌方向都自洽(锚点顺序不同)
    p3 = ps.build_intraday_path(11.0, 11.5, 9.6, 10.0, minutes=10)
    assert ps.validate_path(p3, 11.0, 11.5, 9.6, 10.0)

    # 其它粒度段数
    assert len(ps.build_intraday_path(o, hi, lo, c, minutes=5)) == 48
    assert len(ps.build_intraday_path(o, hi, lo, c, minutes=30)) == 8


def test_path_covered_high_low():
    """路径段高低应覆盖当日高低点(做T/止损盘中触发的前提)."""
    o, hi, lo, c = 10.0, 11.8, 9.5, 11.2
    p = ps.build_intraday_path(o, hi, lo, c, minutes=10)
    assert max(s["high"] for s in p) >= hi * 0.999, "应有段 high 接近当日最高"
    assert min(s["low"] for s in p) <= lo * 1.001, "应有段 low 接近当日最低"


# ---------------------------------------------------------------- 盘中触发
class _FakeStore:
    def __init__(self, series: dict[str, pd.DataFrame]) -> None:
        self.series = series

    def ensure_range(self, symbol: str, start: str = "", end: str = "",
                     period: str = "daily", adjust: str = "qfq",
                     force: bool = False, secid: str | None = None) -> dict:
        df = self.series.get(symbol)
        if df is None or df.empty:
            return {"symbol": symbol, "source": "none", "rows": [], "row_count": 0,
                    "fetched": 0, "note": ""}
        rows = df.to_dict("records")
        return {"symbol": symbol, "source": "backtest_kline", "rows": rows,
                "row_count": len(rows), "fetched": 0, "note": ""}

    def load_index(self, secid: str, start: str = "", end: str = "") -> dict:
        df = self.series.get("idx:" + secid)
        if df is None:
            return {"source": "none", "rows": [], "row_count": 0, "fetched": 0, "note": ""}
        rows = df.to_dict("records")
        return {"source": "backtest_kline", "rows": rows, "row_count": len(rows),
                "fetched": 0, "note": ""}


def _kline_df(close_list: list[float], base_date: str = "2024-01-02") -> pd.DataFrame:
    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume = np.full(len(close), 5_000_000.0)
    dates = pd.bdate_range(base_date, periods=len(close))
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close,
    })


def test_intraday_stop_triggers_same_day(monkeypatch):
    """盘中模拟: 当日低点触及止损线 -> 当日成交(而非等次日开盘)."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    # 单日阴线(-6%, 在跌停限制内): 开 10.0 低 9.3 收 9.4(盘中跌破静态止损线 9.5)
    df = _kline_df(flat + [9.4])
    df.loc[len(df) - 1, "open"] = 10.0
    df.loc[len(df) - 1, "high"] = 10.1
    df.loc[len(df) - 1, "low"] = 9.3
    store = _FakeStore({"UP1": df, "idx:0.000300": _kline_df([3000 + i for i in range(len(df))])})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    legs = [Leg(symbol="UP1", name="票", entry_date=str(df["date"].iloc[50])[:10],
                cost=10.0, qty=1000)]
    rep = run_portfolio_backtest(legs, manage=MANAGE_SIGNAL)
    stops = [t for t in rep["trades"] if t["action"] == "sell_stop"]
    assert stops, "阴线应触发止损"
    # 止损应发生在阴线当日(盘中路径触及), 成交价在 9.3~9.4 区间
    big_date = str(df["date"].iloc[-1])[:10]
    same_day = [t for t in stops if t["date"] == big_date]
    assert same_day, f"盘中止损应在当日成交: {stops}"
    # 成交价 = 段最低价 × (1-滑点), 落在当日 low~close 区间
    assert 9.25 <= same_day[0]["price"] <= 9.4, f"止损成交价应在当日区间: {same_day[0]}"


def test_t_plus_1_same_day_buy_not_sellable(monkeypatch):
    """T+1: 止损后盘中再入场(BUY_FIRST)当日, 后续段不再触发卖出类信号."""
    # V 型行情(复用已验证会触发止损->再入场的场景)
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    fall = [10.0 - i * 0.0625 for i in range(1, 41)]  # 10 -> 7.5(触发止损)
    rise = [7.5 + i * 0.055 for i in range(1, 61)]    # 7.5 -> 10.8(触发 BUY_FIRST 再入场)
    df = _kline_df(flat + fall + rise)
    store = _FakeStore({"UP1": df, "idx:0.000300": _kline_df([3000 + i for i in range(len(df))])})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    legs = [Leg(symbol="UP1", name="票", entry_date=str(df["date"].iloc[80])[:10],
                cost=float(df["close"].iloc[80]), qty=2000)]
    rep = run_portfolio_backtest(legs, manage=MANAGE_SIGNAL)
    assert any(t["action"] == "buy_first" for t in rep["trades"]), "应出现止损后再入场"
    # T+1 硬规则: 任何买入动作(再入场/加仓/低吸)当日之后不得出现卖出类
    by_day: dict[str, list[str]] = {}
    for t in rep["trades"]:
        by_day.setdefault(t["date"], []).append(t["action"])
    for day, acts in by_day.items():
        for i, a in enumerate(acts):
            if a in ("buy_first", "buy_add", "t_buy"):
                later = acts[i + 1:]
                assert not any(s in ("sell_stop", "sell_reduce", "t_sell") for s in later), \
                    f"{day} {a} 后当日出现卖出, T+1 保护失效: {acts}"
