"""回测中心(方案C)单测: 阶段分桶因子回测 + 费用计算 + 缓存股票列表."""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.config import config_manager


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


def _fake_env(monkeypatch):
    """monkeypatch 缓存层: 两只构造行情(启动后上涨 / 过热后回落)."""
    from app.core.backtest import factor as f

    flat = [100.0 + np.sin(i / 3) * 3 + np.sin(i / 11) * 1.5 for i in range(100)]
    launch_rows = _kline_rows(flat + [101, 102.2, 103.6, 105.2] + [round(105.2 + i * 0.5, 2) for i in range(1, 35)])

    # 缓涨后暴涨(乖离/RSI 过热 -> 过热期信号)再暴跌 -> 过热信号后买入多数亏损
    slow = [100.0 + i * 0.2 + np.sin(i / 6) * 1.5 for i in range(120)]
    spike = [round(130 + i * 2.5, 2) for i in range(5)]           # 130 -> 140, +2.5/根
    crash = [round(spike[-1] - i * 1.5, 2) for i in range(1, 41)]  # 140 -> ~81
    overheat_rows = _kline_rows(slow + spike + crash)

    store = {"LAUNCH1": launch_rows, "OVER1": overheat_rows}

    monkeypatch.setattr(f.kline_store, "load", lambda symbol, period="daily": store.get(symbol))
    monkeypatch.setattr(f.kline_store, "list_symbols", lambda period="daily": list(store.keys()))
    return f, store


def test_backtest_launch_beats_overheat(monkeypatch):
    """启动期(信号后持续上涨)20日胜率高且期望为正; 且启动期期望显著高于过热期(信号后回落)."""
    f, _ = _fake_env(monkeypatch)
    report = f.backtest_factors(hold_days=(5, 20), min_bars=60, cost=False)

    assert report["meta"]["symbols_used"] == 2
    launch = report["by_stage"]["launch"]["holds"]["hold_20"]
    overheat = report["by_stage"]["overheat"]["holds"]["hold_20"]
    assert launch["n"] > 0
    assert launch["win_rate"] > 60.0, f"启动期20日胜率应高: {launch}"
    assert launch["avg"] > 0
    assert launch["avg"] > overheat["avg"], (
        f"启动期20日期望应高于过热期: launch={launch['avg']}% overheat={overheat['avg']}%")
    # 阶段分布里有启动/过热/无趋势
    dist = report["stage_distribution"]
    assert dist.get("launch", 0) > 0 and dist.get("overheat", 0) > 0


def test_backtest_cost_reduces_expectancy(monkeypatch):
    """扣费后期望不高于不扣费."""
    f, _ = _fake_env(monkeypatch)
    r_no = f.backtest_factors(hold_days=(5,), min_bars=60, cost=False)
    r_cost = f.backtest_factors(hold_days=(5,), min_bars=60, cost=True)
    for stage, info in r_no["by_stage"].items():
        a = info["holds"]["hold_5"]["avg"]
        b = r_cost["by_stage"][stage]["holds"]["hold_5"]["avg"]
        assert b <= a + 1e-9, f"{stage}: 扣费后({b})不应高于不扣费({a})"


def test_backtest_filters_short_history(monkeypatch):
    """历史不足 min_bars 的股票不参与."""
    from app.core.backtest import factor as f

    short = _kline_rows([100 + i * 0.1 for i in range(20)])  # 仅 20 根
    monkeypatch.setattr(f.kline_store, "load", lambda symbol, period="daily": short)
    monkeypatch.setattr(f.kline_store, "list_symbols", lambda period="daily": ["SHORT1"])
    report = f.backtest_factors(hold_days=(5,), min_bars=60, cost=False)
    assert report["meta"]["symbols_used"] == 0
    assert report["by_stage"] == {}


# ---------------------------------------------------------------- 总分分桶
def test_score_bucket_edges():
    """总分 -> 分桶边界: 整十归上桶, 上限以上归 70+."""
    from app.core.backtest.factor import _score_bucket

    assert _score_bucket(0.0) == "0-40"
    assert _score_bucket(39.9) == "0-40"
    assert _score_bucket(40.0) == "40-50"
    assert _score_bucket(49.9) == "40-50"
    assert _score_bucket(50.0) == "50-60"
    assert _score_bucket(59.9) == "50-60"
    assert _score_bucket(60.0) == "60-70"
    assert _score_bucket(69.9) == "60-70"
    assert _score_bucket(70.0) == "70+"
    assert _score_bucket(105.0) == "70+"  # 理论上限100出头, 归最高桶


def test_backtest_by_score_structure(monkeypatch):
    """总分分桶: 输出结构完整、按分数从低到高有序、与阶段分桶样本数守恒(同一信号日两种切法).

    注: 分数与未来收益的方向性(高分桶是否更优)是回测要回答的业务结论,
    由真实数据回测验证, 不在构造数据下断言(构造行情 spike 后 100% 暴跌, 过于极端).
    """
    f, _ = _fake_env(monkeypatch)
    report = f.backtest_factors(hold_days=(5, 20), min_bars=60, cost=False)

    assert report["by_score"], "总分分桶结果不应为空"
    keys = list(report["by_score"].keys())
    order = [k for k, _, _ in f.SCORE_BUCKETS]
    # 键有序(低分在前, 便于看单调性)且都在预定义集合内
    assert keys == sorted(keys, key=order.index)
    assert set(keys) <= set(order)
    # 至少能分出两个分数档(评分有区分度, 否则分桶无意义)
    assert len(keys) >= 2, f"构造数据下分数应有区分度: {report['score_distribution']}"
    # 每桶字段结构与 by_stage 同构
    for key, info in report["by_score"].items():
        assert info["label"] == f"{key}分"
        for h in ("hold_5", "hold_20"):
            s = info["holds"][h]
            assert {"n", "win_rate", "avg", "median", "expectancy"} <= set(s)
            assert s["n"] > 0
    # 样本守恒: 同一批信号日, 按阶段/按总分两种切法总数一致
    n_score = sum(report["score_distribution"].values())
    n_stage = sum(report["stage_distribution"].values())
    assert n_score == n_stage and n_score > 0
    # meta 口径说明包含总分
    assert "总分" in report["meta"]["notes"]


def test_net_return_fees():
    """净收益扣双边手续费: 大收益为正但小于毛收益; 微利被费用吃掉(含最低佣金5元)."""
    from app.core.backtest.factor import _net_return

    fee = config_manager.get()["手续费"]
    r = _net_return(10.0, 12.0, fee)     # +20% 毛收益
    assert 0 < r < 20.0
    r2 = _net_return(10.0, 10.01, fee)   # +0.1% 毛收益
    assert r2 < 0, "0.1% 收益应被双边费用吃掉"


def test_stage_label_map():
    from app.core.backtest.factor import stage_label

    assert stage_label("launch") == "启动期"
    assert stage_label("unknown") == "unknown"
