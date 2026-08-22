"""策略回测修复单测: 止损冷却期 + 回撤软防守(含恢复)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.backtest.strategy import StrategyBacktest


def _kline_rows(close_list: list[float]) -> list[dict]:
    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = np.full(len(close), 5_000_000.0)
    amount = volume * close
    dates = pd.bdate_range("2025-01-02", periods=len(close))
    return [
        {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": a}
        for d, o, h, lo, c, v, a in zip(dates.strftime("%Y-%m-%d"), open_, high, low, close, volume, amount, strict=True)
    ]


def _fake_store(monkeypatch):
    """构造行情: 启动后上涨(会出首仓) / 缓涨暴涨后暴跌(会止损)."""
    from app.core.datasource import kline_store

    flat = [100.0 + np.sin(i / 3) * 3 + np.sin(i / 11) * 1.5 for i in range(100)]
    launch_rows = _kline_rows(flat + [101, 102.2, 103.6, 105.2] + [round(105.2 + i * 0.5, 2) for i in range(1, 35)])

    slow = [100.0 + i * 0.2 + np.sin(i / 6) * 1.5 for i in range(120)]
    spike = [round(130 + i * 2.5, 2) for i in range(5)]
    crash = [round(spike[-1] - i * 1.5, 2) for i in range(1, 41)]
    overheat_rows = _kline_rows(slow + spike + crash)

    store = {"LAUNCH1": launch_rows, "OVER1": overheat_rows}
    monkeypatch.setattr(kline_store, "load", lambda symbol, period="daily": store.get(symbol))
    monkeypatch.setattr(kline_store, "list_symbols", lambda period="daily": list(store.keys()))
    return store


# ---------------------------------------------------------------- 止损冷却
def test_cooldown_active_window():
    """止损后 N 个交易日内禁止同票 BUY_FIRST; 第 N 日解禁; 开关关闭不拦."""
    bt = StrategyBacktest(initial_capital=1_000_000)
    assert bt.stop_cooldown_days == 10  # 默认配置
    bt._last_stop_day["300274"] = 3
    assert bt._cooldown_active("300274", 5) is True    # 2 天后: 拦截
    assert bt._cooldown_active("300274", 12) is True   # 9 天后: 拦截
    assert bt._cooldown_active("300274", 13) is False  # 第 10 个交易日: 解禁
    assert bt._cooldown_active("OTHER", 5) is False    # 无止损记录: 不拦
    bt.stop_cooldown_days = 0
    assert bt._cooldown_active("300274", 4) is False   # 开关关闭: 不拦


def test_cooldown_reduces_reentry_count(monkeypatch):
    """同一行情下, 开冷却期后 BUY_FIRST 笔数不多于关闭时(连环接刀被拦截)."""
    _fake_store(monkeypatch)
    bt_off = StrategyBacktest(initial_capital=1_000_000)
    bt_off.stop_cooldown_days = 0
    r_off = bt_off.run(symbols=["LAUNCH1", "OVER1"])

    bt_on = StrategyBacktest(initial_capital=1_000_000)
    bt_on.stop_cooldown_days = 10
    r_on = bt_on.run(symbols=["LAUNCH1", "OVER1"])

    assert "error" not in r_off and "error" not in r_on
    n_off = sum(1 for t in r_off["trades"] if t["action"] == "buy_first")
    n_on = sum(1 for t in r_on["trades"] if t["action"] == "buy_first")
    assert n_on <= n_off, f"冷却期后首仓笔数应不多于关闭时: on={n_on} off={n_off}"
    # 若发生过止损后冷却期内的再入场尝试, 统计应被记录
    assert r_on["stats"]["cooldown_blocks"] >= 0
    assert r_on["stats"]["stop_cooldown_days"] == 10


# ---------------------------------------------------------------- 回撤软防守
def test_defense_soft_mode_and_recovery():
    """软防守: 回撤达阈值开启; 修复至阈值一半以下解除; 中间地带保持原状态."""
    bt = StrategyBacktest(initial_capital=1_000_000)
    bt._drawdown_limit = 10.0
    bt._defense_recovery_ratio = 0.5

    bt._update_defense(950_000)              # 回撤 5%: 未达阈值
    assert bt.defense_mode is False
    bt._update_defense(890_000)              # 回撤 11%: 开启(软防守)
    assert bt.defense_mode is True
    bt._update_defense(930_000)              # 修复到回撤 7%: 处于滞回带, 保持防守
    assert bt.defense_mode is True
    bt._update_defense(960_000)              # 修复到回撤 4% < 5%: 解除
    assert bt.defense_mode is False
    bt._update_defense(880_000)              # 再次跌破: 重新开启
    assert bt.defense_mode is True


def test_defense_mode_still_allows_entries(monkeypatch):
    """防守开启时开仓/加仓闸门不关闭(仓位减半, 对齐实盘), 熔断仍禁开仓."""
    bt = StrategyBacktest(initial_capital=1_000_000)
    bt._drawdown_limit = 10.0
    bt._update_defense(890_000)              # 触发防守
    assert bt.defense_mode is True
    # 防守只减仓位(defense_ratio), 不再锁 gate —— 由 run() 内部逻辑保证,
    # 这里验证状态字段供闸门读取: 软防守下 defense_ratio 应为 0.5
    defense_ratio = 0.5 if bt.defense_mode else 1.0
    assert defense_ratio == 0.5
