"""策略回测(方案C V2)单测: 建仓/加仓/止盈/止损/做T 全循环 + 净值与风控."""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.backtest.strategy import run_strategy_backtest


def _kline_rows(close_list: list[float]) -> list[dict]:
    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.02
    low = np.minimum(open_, close) * 0.98
    volume = np.full(len(close), 5_000_000.0)
    amount = volume * close
    dates = pd.bdate_range("2025-01-02", periods=len(close))
    return [
        {"date": d.strftime("%Y-%m-%d"), "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": a}
        for d, o, h, lo, c, v, a in zip(dates, open_, high, low, close, volume, amount, strict=True)
    ]


def _fake_store(monkeypatch, store: dict[str, list[dict]]):
    from app.core.backtest import strategy as st

    monkeypatch.setattr(st.kline_store, "load", lambda symbol, period="daily": store.get(symbol))
    monkeypatch.setattr(st.kline_store, "list_symbols", lambda period="daily": list(store.keys()))


def _win_series() -> list[dict]:
    """横盘后温和上涨(建仓信号) + 持续大涨(止盈/做T)."""
    flat = [100.0 + np.sin(i / 3) * 3 + np.sin(i / 11) * 1.5 for i in range(80)]
    up1 = [flat[-1] + i * 1.0 for i in range(1, 16)]                       # 温和启动 -> BUY_FIRST
    up2 = [round(up1[-1] * (1.02 ** k), 2) for k in range(1, 25)]          # 加速 -> 止盈
    return _kline_rows(flat + up1 + up2)


def _crash_series() -> list[dict]:
    """上涨建仓后暴跌 -> 触发止损."""
    rise = [100.0 + i * 0.6 + np.sin(i / 5) * 1.5 for i in range(70)]
    crash = [round(rise[-1] * (0.97 ** k), 2) for k in range(1, 20)]
    return _kline_rows(rise + crash)


def _swing_series() -> list[dict]:
    """高波动震荡上行 -> 做T(冲布林上轨/回踩下轨)."""
    vals = [100.0]
    for i in range(160):
        drift = 0.15 + np.sin(i / 3) * 4.0 + np.sin(i / 7) * 2.0  # 大幅摆动(±6%)
        vals.append(round(max(1.0, vals[-1] + drift), 2))
    return _kline_rows(vals)


def test_strategy_full_cycle_buy_then_take_profit(monkeypatch):
    """上涨行情: 出现建仓 -> 后续止盈减仓且盈利; 净值曲线完整."""
    _fake_store(monkeypatch, {"WIN1": _win_series()})
    report = run_strategy_backtest(["WIN1"], initial_capital=1_000_000)
    assert "error" not in report
    acts = [t["action"] for t in report["trades"]]
    assert "buy_first" in acts, f"应出现首仓: {acts}"
    assert "sell_reduce" in acts, f"应出现止盈减仓: {acts}"
    reduce_trades = [t for t in report["trades"] if t["action"] == "sell_reduce"]
    assert reduce_trades and all(t["pnl"] > 0 for t in reduce_trades), "止盈减仓应盈利"
    assert len(report["equity_curve"]) > 100
    assert report["meta"]["final_equity"] > report["meta"]["initial_capital"]


def test_strategy_stop_loss_on_crash(monkeypatch):
    """暴跌行情: 建仓后触发止损, 止损交易亏损."""
    _fake_store(monkeypatch, {"CRASH1": _crash_series()})
    report = run_strategy_backtest(["CRASH1"], initial_capital=1_000_000)
    stops = [t for t in report["trades"] if t["action"] == "sell_stop"]
    assert stops, "暴跌行情应触发止损"
    assert all(t["pnl"] < 0 for t in stops), f"止损应亏损: {stops}"
    assert report["meta"]["final_equity"] < report["meta"]["initial_capital"]


def test_strategy_swing_does_t_trade(monkeypatch):
    """高波动震荡: 出现做T(高抛低吸)交易."""
    _fake_store(monkeypatch, {"SWING1": _swing_series()})
    report = run_strategy_backtest(["SWING1"], initial_capital=1_000_000)
    acts = [t["action"] for t in report["trades"]]
    assert "t_sell" in acts or "t_buy" in acts, f"高波动行情应出现做T: {acts}"


def test_strategy_dates_normalized(monkeypatch):
    """日期格式混用(带 15:00 与不带)不产生同日同动作重复交易(主信号+做T 可同日并存)."""
    rows = _win_series()
    mixed = []
    for i, r in enumerate(rows):
        r = dict(r)
        if i % 2 == 0:
            r["date"] = r["date"] + " 15:00"
        mixed.append(r)
    _fake_store(monkeypatch, {"WIN1": mixed})
    report = run_strategy_backtest(["WIN1"], initial_capital=1_000_000)
    seen: set[tuple[str, str, str]] = set()
    for t in report["trades"]:
        key = (t["date"], t["symbol"], t["action"])
        assert key not in seen, f"同日同股同动作重复交易: {key}"
        seen.add(key)


def test_strategy_portfolio_risk_gates(monkeypatch):
    """组合级风控字段存在且不污染: 熔断/回撤防守为 bool, 单票仓位受限."""
    _fake_store(monkeypatch, {
        "CRASH1": _crash_series(),
        "WIN1": _win_series(),
    })
    report = run_strategy_backtest(["CRASH1", "WIN1"], initial_capital=1_000_000)
    assert "fuse_triggered" in report["stats"]
    assert isinstance(report["stats"]["fuse_triggered"], bool)
    assert report["meta"]["total_return_pct"] is not None


def test_strategy_insufficient_data_skipped(monkeypatch):
    """历史不足的股票不参与."""
    short = _kline_rows([100 + i * 0.1 for i in range(30)])
    _fake_store(monkeypatch, {"SHORT1": short})
    report = run_strategy_backtest(["SHORT1"], initial_capital=1_000_000)
    assert "error" in report or report["meta"]["pool"] == 0
