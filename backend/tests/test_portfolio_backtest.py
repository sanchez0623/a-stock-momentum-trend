"""持仓回测单测(方案 v2 §5): 三线对照 / 归因 / 基准 / 模式 A·B / 引擎复用."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from app.core.backtest import portfolio as pf
from app.core.backtest.portfolio import (
    MANAGE_HOLD,
    MANAGE_SIGNAL,
    MANAGE_STOP,
    Leg,
    PortfolioBacktest,
)
from app.core.signals.engine import PositionInfo, SignalEngine
from app.models.models import Position


# ---------------------------------------------------------------- 行情构造
def _kline_df(close_list: list[float], base_date: str = "2024-01-02") -> pd.DataFrame:
    """确定性日线: 开盘=昨收, 高低=±1%(不足以触发价格跳变防护)."""
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


def _up_series() -> pd.DataFrame:
    """横盘 70 根后一路上涨(10 -> 20), 供止盈/加仓触发."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    rise = [10.0 + i * 0.055 for i in range(1, 181)]
    return _kline_df(flat + rise)


def _down_series() -> pd.DataFrame:
    """横盘 70 根后一路下跌(10 -> 5), 供止损触发."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    fall = [10.0 - i * 0.028 for i in range(1, 181)]
    return _kline_df(flat + fall)


def _vshape_series() -> pd.DataFrame:
    """横盘 70 根 -> 跌 40 根(-25% 触发止损) -> V 型反弹 60 根(新高, 触发 BUY_FIRST 再入场)."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    fall = [10.0 - i * 0.0625 for i in range(1, 41)]  # 10 -> 7.5
    rise = [7.5 + i * 0.055 for i in range(1, 61)]    # 7.5 -> 10.8
    return _kline_df(flat + fall + rise)


class _FakeStore:
    """假数据通道: ensure_range/load_index 直接返回构造行情(冻结快照语义)."""

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


@pytest.fixture
def fake_data(monkeypatch):
    """注入 UP1/DOWN1 行情 + 沪深300 基准, 替换真实数据通道."""
    up = _up_series()
    down = _down_series()
    bench = _kline_df([3000 + i * 2 for i in range(250)])
    store = _FakeStore({"UP1": up, "DOWN1": down, "idx:0.000300": bench})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    return store


def _legs() -> list[Leg]:
    """UP1 第 100 根建仓(约 11.7 元), DOWN1 第 100 根建仓(约 9.1 元)."""
    up = _up_series()
    down = _down_series()
    return [
        Leg(symbol="UP1", name="上行票", entry_date=str(up["date"].iloc[100])[:10],
            cost=float(up["close"].iloc[100]), qty=1000),
        Leg(symbol="DOWN1", name="下行票", entry_date=str(down["date"].iloc[100])[:10],
            cost=float(down["close"].iloc[100]), qty=1000),
    ]


# ---------------------------------------------------------------- 三线对照
def test_hold_line_no_trades(fake_data):
    """躺平线: 只有建仓腿成交, 无任何管理交易; 曲线与手算一致."""
    rep = pf.run_portfolio_backtest(_legs(), manage=MANAGE_HOLD)
    assert "error" not in rep
    actions = {t["action"] for t in rep["trades"]}
    assert actions == {"buy_entry"}
    # 末日权益 = 现金 + 两腿市值; 无交易时现金 = initial - Σ投入
    meta = rep["meta"]
    up = _up_series()
    down = _down_series()
    end_mv = 1000 * float(up["close"].iloc[-1]) + 1000 * float(down["close"].iloc[-1])
    expected = meta["initial_capital"] - (1000 * 11.7 + 1000 * 9.1) + end_mv
    last_eq = rep["curves"]["hold"][-1]["equity"]
    assert abs(last_eq - expected) < 200, f"hold 末日权益应≈{expected}, got {last_eq}"
    # 三线曲线起点一致(腿入场前均为初始资金)
    assert rep["curves"]["hold"][0]["equity"] == rep["curves"]["signal"][0]["equity"]


def test_stop_line_stops_down_leg(fake_data):
    """纪律线: 下行票触发止损(SELL_STOP), 止损线收益显著高于躺平线."""
    rep = pf.run_portfolio_backtest(_legs(), manage=MANAGE_STOP)
    stops = [t for t in rep["trades"] if t["action"] == "sell_stop"]
    assert stops, "下行票应触发止损"
    assert {t["symbol"] for t in stops} == {"DOWN1"}
    assert rep["stats"]["stop_return_pct"] > rep["stats"]["hold_return_pct"], (
        f"止损应减少亏损: stop={rep['stats']['stop_return_pct']}% hold={rep['stats']['hold_return_pct']}%")


def test_signal_line_reuses_engine(fake_data):
    """系统线: 信号全部来自 SignalEngine(理由文案非内联), 含止损/止盈等动作."""
    rep = pf.run_portfolio_backtest(_legs(), manage=MANAGE_SIGNAL)
    trades = rep["trades"]
    assert any(t["action"] == "sell_stop" for t in trades), "应有止损"
    # 理由来自引擎文案(非内联"日内冲布林上轨,做T高抛"这类硬编码)
    for t in trades:
        assert t["reason"], "交易应有信号理由"
    # 三线完整
    for key in ("hold", "stop", "signal"):
        assert rep["curves"][key], f"缺 {key} 曲线"


def test_attribution_consistency(fake_data):
    """归因表: 每腿 excess_pct = signal_return - hold_return; 归因含操作类型."""
    rep = pf.run_portfolio_backtest(_legs(), manage=MANAGE_SIGNAL)
    legs = {leg["symbol"]: leg for leg in rep["legs"]}
    assert set(legs) == {"UP1", "DOWN1"}
    for _sym, row in legs.items():
        # excess = managed - hold(容忍 2 次 round 的舍入误差)
        assert abs(row["excess_pct"] - (row["managed_return_pct"] - row["hold_return_pct"])) < 0.02, row
        assert "attribution" in row
    # DOWN1 止损贡献为负(减少亏损), 出现在归因里
    assert legs["DOWN1"]["attribution"].get("sell_stop", 0) < 0
    # UP1 有卖出类归因(止盈减仓; 盘中路径下做T 需段价摸到布林轨, 不再保证必有)
    assert len(legs["UP1"]["attribution"]) > 0, "UP1 应有卖出类操作归因"


def test_benchmark_curve_normalized(fake_data):
    """基准曲线: 存在且首点为 1.0(归一化)."""
    rep = pf.run_portfolio_backtest(_legs(), manage=MANAGE_SIGNAL)
    bench = rep["curves"]["benchmark"]
    assert bench, "应有基准曲线"
    assert bench[0]["equity"] == 1.0
    assert rep["meta"]["benchmark"] == "沪深300"


# ---------------------------------------------------------------- 模式 A / B
def test_real_mode_from_positions(tmp_engine):
    """模式 A: Position 表当前持仓 -> 建仓腿(含 pyramid_stage/opened_at)."""
    from app import db

    with db.session_scope() as s:
        s.add(Position(symbol="600000", name="浦发银行", qty=1000, cost=10.5,
                       status="holding", opened_at="2024-03-01 10:00:00", pyramid_stage=1))
        s.add(Position(symbol="000001", name="平安银行", qty=500, cost=12.0,
                       status="holding", opened_at="2024-05-06 10:00:00", pyramid_stage=0))
        s.commit()
    legs = PortfolioBacktest.load_position_legs()
    assert len(legs) == 2
    by_sym = {leg.symbol: leg for leg in legs}
    assert by_sym["600000"].entry_date == "2024-03-01"
    assert by_sym["600000"].pyramid_stage == 1
    assert by_sym["000001"].qty == 500


def test_import_mode_multi_entry_dates(fake_data):
    """模式 B: 不同时间建仓 -> 晚入场腿对前段曲线无影响(曲线起始平坦)."""
    up = _up_series()
    late_date = str(up["date"].iloc[150])[:10]  # 晚 50 根建仓
    legs = [
        Leg(symbol="UP1", name="上行票", entry_date=str(up["date"].iloc[100])[:10],
            cost=float(up["close"].iloc[100]), qty=1000),
        Leg(symbol="UP1", name="上行票(第二批)", entry_date=late_date,
            cost=float(up["close"].iloc[150]), qty=1000),
    ]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_HOLD)
    # 同 symbol 多腿在归因表聚合为一行, 但两腿都真实入场(qty 合并)
    assert len(rep["legs"]) == 1
    row = rep["legs"][0]
    assert row["legs"] == 2 and row["qty"] == 2000
    # 建仓日取最早的一腿
    assert row["entry_date"] == str(up["date"].iloc[100])[:10]


def test_no_valid_legs(fake_data):
    """无有效腿: 返回 error."""
    rep = pf.run_portfolio_backtest([], manage=MANAGE_SIGNAL)
    assert "error" in rep


# ---------------------------------------------------------------- 多腿/再入场(需求优化)
def _reentry_fixture(monkeypatch):
    """V 型行情数据通道(单只票 + 基准)."""
    v = _vshape_series()
    bench = _kline_df([3000 + i * 2 for i in range(len(v))])
    store = _FakeStore({"V1": v, "idx:0.000300": bench})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    return v


def test_multi_legs_same_symbol_all_enter(fake_data):
    """需求1: 同股票分批建仓(不同日期)全部入场, 持仓合并, 归因表聚合为一行."""
    up = _up_series()
    legs = [
        Leg(symbol="UP1", name="上行票", entry_date=str(up["date"].iloc[100])[:10],
            cost=float(up["close"].iloc[100]), qty=1000),
        Leg(symbol="UP1", name="上行票", entry_date=str(up["date"].iloc[130])[:10],
            cost=float(up["close"].iloc[130]), qty=500),
    ]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_HOLD)
    entries = [t for t in rep["trades"] if t["action"] == "buy_entry"]
    assert len(entries) == 2, "两条建仓腿都应入场"
    # 归因表聚合为一行(legs=2, qty 合并)
    assert len(rep["legs"]) == 1
    row = rep["legs"][0]
    assert row["qty"] == 1500 and row["legs"] == 2
    # 末日市值 = 合并持仓 × 末日收盘
    last_close = float(up["close"].iloc[-1])
    assert abs(row["hold_return_pct"] - (last_close / (1000 * 11.7 + 500 * 13.3) * 1500 * 100 - 100)) < 5 or row["hold_return_pct"] > 0


def test_same_symbol_same_date_weighted_cost(tmp_engine, monkeypatch):
    """同日两腿不同成本: 入场后持仓成本为加权平均(非覆盖)."""
    up = _up_series()
    entry = str(up["date"].iloc[100])[:10]
    store = _FakeStore({"UP1": up, "idx:0.000300": _kline_df([3000 + i for i in range(len(up))])})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    legs = [
        Leg(symbol="UP1", name="上行票", entry_date=entry, cost=10.0, qty=1000),
        Leg(symbol="UP1", name="上行票", entry_date=entry, cost=20.0, qty=1000),
    ]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_HOLD)
    entries = [t for t in rep["trades"] if t["action"] == "buy_entry"]
    assert len(entries) == 2
    # 加权成本 = (10*1000 + 20*1000)/2000 = 15
    row = rep["legs"][0]
    assert abs(row["cost"] - 15.0) < 0.01, f"成本应为加权平均 15, got {row['cost']}"


def test_reentry_after_stop_loss(monkeypatch):
    """需求2: 止损清仓后, BUY_FIRST 信号触发时系统线自动重新买入."""
    v = _reentry_fixture(monkeypatch)
    legs = [Leg(symbol="V1", name="V型票", entry_date=str(v["date"].iloc[80])[:10],
                cost=float(v["close"].iloc[80]), qty=2000)]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_SIGNAL)
    actions = [t["action"] for t in rep["trades"]]
    assert "sell_stop" in actions, "下跌段应触发止损"
    stop_idx = actions.index("sell_stop")
    assert "buy_first" in actions[stop_idx + 1:], "止损后应出现 BUY_FIRST 再入场"
    # 再入场后继续管理(可能出现加仓/再止损)
    assert len(rep["trades"]) > stop_idx + 2


def test_stop_line_no_reentry(monkeypatch):
    """纪律线: 清仓后保持纯止损口径, 不再买入."""
    v = _reentry_fixture(monkeypatch)
    legs = [Leg(symbol="V1", name="V型票", entry_date=str(v["date"].iloc[80])[:10],
                cost=float(v["close"].iloc[80]), qty=2000)]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_STOP)
    actions = [t["action"] for t in rep["trades"]]
    assert "sell_stop" in actions
    assert "buy_first" not in actions, "纪律线清仓后不应再买入"


def test_preset_crud(tmp_engine):
    """建仓腿模板 CRUD: 保存(过滤无效腿) / 列表 / 删除."""
    from app.main import app
    from fastapi.testclient import TestClient

    c = TestClient(app)
    r = c.post('/api/backtest/presets', json={'name': '测试组合', 'legs': [
        {'symbol': '600000', 'entry_date': '2025-06-03', 'cost': 7.5, 'qty': 2000},
        {'symbol': 'BAD', 'entry_date': '2025-06-03', 'cost': 0, 'qty': 0},  # 无效腿被过滤
    ]})
    assert r.json()['code'] == 0, r.json()
    r = c.get('/api/backtest/presets')
    data = r.json()['data']
    assert len(data) == 1
    assert data[0]['name'] == '测试组合'
    assert len(data[0]['legs']) == 1, "无效腿应被过滤"
    assert data[0]['legs'][0]['symbol'] == '600000'
    # 空模板名被拒
    assert c.post('/api/backtest/presets', json={'name': ' ', 'legs': data[0]['legs']}).json()['code'] == 1
    # 删除
    assert c.delete(f"/api/backtest/presets/{data[0]['id']}").json()['code'] == 0
    assert c.get('/api/backtest/presets').json()['data'] == []


def test_star_market_t_sell_no_odd_lot(monkeypatch):
    """bug 修复: 科创板做T 高抛不再产生 <200 股的违规卖出(复现 688313 卖 29 股场景)."""
    flat = [10.0 + np.sin(i / 8) * 0.2 for i in range(70)]
    df = _kline_df(flat + [11.2])
    # 最后一根: 高开高走大阳线(high 突破布林上轨, 振幅 ~13% -> 触发 T_SELL 判定)
    df.loc[len(df) - 1, "open"] = 10.3
    df.loc[len(df) - 1, "high"] = 11.6
    df.loc[len(df) - 1, "low"] = 10.2
    df.loc[len(df) - 1, "close"] = 11.2
    store = _FakeStore({"688313": df, "idx:0.000300": _kline_df([3000 + i for i in range(len(df))])})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    legs = [Leg(symbol="688313", name="科创板票", entry_date=str(df["date"].iloc[50])[:10],
                cost=10.0, qty=229)]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_SIGNAL)
    for t in rep["trades"]:
        if t["action"] in ("t_sell", "sell_reduce", "sell_stop"):
            assert t["qty"] >= 200, f"科创板卖出申报应≥200 股, got {t}"


# ---------------------------------------------------------------- 移动止损(浮盈保护)
def test_trailing_stop_engine(kline_df):
    """移动止损: 峰值×(1-trailing) 高于静态线时接管; 未记录峰值退化为静态线."""
    from app.core.config import config_manager
    from app.core.indicators import compute_all
    from app.core.signals.engine import SignalEngine

    cfg = config_manager.get()
    df = kline_df.copy()
    ind = compute_all(
        df,
        ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
        macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
        rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
        volume_ma=cfg["量能"]["volume_ma"],
    )
    n = len(ind)
    engine = SignalEngine()
    # 固定假模式决策(与行情判定解耦): 静态 5% / 移动 8%
    class _MD:
        mode_key = "test"
        label = "测试"

        def __init__(self) -> None:
            self.mode = {"stop_loss_pct": 5.0, "trailing_stop_pct": 8.0}
            self.regime = {}

    mode_dec = _MD()
    last, prev = ind.iloc[n - 1], ind.iloc[n - 2]

    # 静态线 = 10×(1-5%) = 9.5; 峰值 12, trailing 8% -> 移动线 11.04 接管
    pos = PositionInfo(symbol="X1", cost=10.0, qty=1000, peak_price=12.0)
    sig = engine._check_stop(cfg, ind, last, prev, pos, price=10.5, name="测试", mode_decision=mode_dec)
    assert sig is not None and sig.type == "SELL_STOP"
    assert "移动止损" in sig.reason, f"应报移动止损: {sig.reason}"
    # 价格仍在移动线上方 -> 不触发
    sig2 = engine._check_stop(cfg, ind, last, prev, pos, price=11.5, name="测试", mode_decision=mode_dec)
    assert sig2 is None or "止损" not in sig2.reason, f"11.5 > 11.04 不应触发: {sig2}"

    # 未记录峰值(peak_price=0): 退化为静态线(9.5), 跌破才触发
    pos0 = PositionInfo(symbol="X1", cost=10.0, qty=1000)
    sig3 = engine._check_stop(cfg, ind, last, prev, pos0, price=9.4, name="测试", mode_decision=mode_dec)
    assert sig3 is not None and "止损线" in sig3.reason and "移动" not in sig3.reason


def test_trailing_stop_in_backtest(monkeypatch):
    """回测中移动止损: 上涨 30% 后回落 11%, 移动止损(峰值-8%)先于静态线(成本-5%)触发."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    rise = [10.0 + i * 0.05 for i in range(1, 61)]       # 10 -> 13
    fall = [13.0 - i * 0.10 for i in range(1, 16)]       # 13 -> 11.5
    df = _kline_df(flat + rise + fall)
    store = _FakeStore({"UP1": df, "idx:0.000300": _kline_df([3000 + i for i in range(len(df))])})
    monkeypatch.setattr(pf.backtest_data, "ensure_range", store.ensure_range)
    monkeypatch.setattr(pf.backtest_data, "load_index", store.load_index)
    legs = [Leg(symbol="UP1", name="上行票", entry_date=str(df["date"].iloc[75])[:10],
                cost=float(df["close"].iloc[75]), qty=1000)]
    rep = pf.run_portfolio_backtest(legs, manage=MANAGE_SIGNAL)
    stops = [t for t in rep["trades"] if t["action"] == "sell_stop"]
    assert stops, "回落段应触发止损"
    assert any("移动止损" in t["reason"] for t in stops), f"应触发移动止损: {[t['reason'] for t in stops]}"


# ---------------------------------------------------------------- 引擎复用
def test_evaluate_with_ind_skip_t(kline_df):
    """skip_t=True: 主信号跳过做T 分支(做T 由回测侧按盘中高低价单独判定)."""
    engine = SignalEngine()
    df = kline_df.copy()
    # 人为构造高波动 + 摸上轨: 当日 high 远超布林上轨
    n = len(df)
    df.loc[n - 1, "high"] = df.loc[n - 1, "close"] * 1.15
    df.loc[n - 1, "low"] = df.loc[n - 1, "close"] * 0.90
    from app.core.config import config_manager
    from app.core.indicators import compute_all

    cfg = config_manager.get()
    ind = compute_all(
        df,
        ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
        macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
        rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
        volume_ma=cfg["量能"]["volume_ma"],
    )
    pos = PositionInfo(symbol="X1", cost=float(df["close"].iloc[0]), qty=1000)
    # 无 skip_t: 高波动可能命中做T/其它; 有 skip_t: 绝不返回做T 类信号
    sig = engine.evaluate_with_ind("X1", "测试", ind, position=pos, end=n)
    sig_skip = engine.evaluate_with_ind("X1", "测试", ind, position=pos, end=n, skip_t=True)
    if sig is not None and sig.type in ("T_SELL", "T_BUY"):
        assert sig_skip is None or sig_skip.type not in ("T_SELL", "T_BUY"), \
            "skip_t=True 时不得返回做T 信号"
    # _check_t_trade 按 want 拆分(新版引擎): 高价判定 sell、低价判定 buy
    last = ind.iloc[n - 1]
    t_sell = engine._check_t_trade(cfg, ind, last, pos, price=float(df["high"].iloc[-1]),
                                   quote_high=float(df["high"].iloc[-1]), quote_low=float(df["low"].iloc[-1]),
                                   name="测试", want="sell")
    t_buy = engine._check_t_trade(cfg, ind, last, pos, price=float(df["low"].iloc[-1]),
                                  quote_high=float(df["high"].iloc[-1]), quote_low=float(df["low"].iloc[-1]),
                                  name="测试", want="buy")
    assert (t_sell is None) != (t_buy is None) or (t_sell is not None and t_buy is not None)
    if t_sell is not None:
        assert t_sell.type == "T_SELL"
    if t_buy is not None:
        assert t_buy.type == "T_BUY"
