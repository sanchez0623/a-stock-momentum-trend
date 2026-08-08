"""Q2 多模式(规则化市况分类器) + Q1 动态多因子打分 单测.

覆盖:
- classify(): 5 类市况的确定性判定 + 互斥优先级 + 可复现性(可回测/可测试)
- mode_for_ind()/active_mode(): 由指标/原始 K 线选出模式; 关闭/行情不足时回退默认
- engine._score_add(): Q1 多因子打分, 子条件文案可区分(回踩缩量 / 首次触及短均线反弹 /
  RSI 自超买回落 / 沿短均线强势 ...), 浮盈仅小权重
- engine._check_add(): 加仓信号携带 mode + 子条件 reason
- engine._check_stop(): 止损线优先取当前模式的 stop_loss_pct(模式自带风控)
- generator: 计划文案使用当前模式的金字塔比例 + "当前模式"解说行
- manager.pyramid_plan(): 提供 K 线时取当前模式的金字塔比例
"""

from __future__ import annotations

import copy

import pandas as pd

from app.core.config import config_manager
from app.core.modes import (
    ModeDecision,
    active_mode,
    classify,
    mode_for_ind,
    regime_features,
)
from app.core.plan.generator import PlanGenerator
from app.core.position import position_manager
from app.core.signals.engine import PositionInfo, Signal, SignalEngine


gen = PlanGenerator()
eng = SignalEngine()


# ---------------------------------------------------------------- 指标表构造
def _ind_df(last: dict, prev: dict | None = None) -> pd.DataFrame:
    """构造 2 行指标表(行0=prev, 行1=last), 供 regime_features / classify 使用."""
    if prev is None:
        prev = dict(last)
    return pd.DataFrame([prev, last])


def _kline(n: int = 40, seed: int = 3) -> pd.DataFrame:
    """确定性模拟 K 线(>=30 行), 供给 active_mode / evaluate."""
    import numpy as np

    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = 100 + np.cumsum(rng.normal(0, 0.8, n)) + np.linspace(0, 20, n)
    open_ = close + rng.normal(0, 0.2, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.4, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.4, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "amount": volume * close,
        }
    )


# ---------------------------------------------------------------- classify 判定
def test_classify_defense():
    cfg = config_manager.get()
    # 空头占优(-DI>+DI) 且 ADX>=弱线
    regime = {"pdi": 10, "mdi": 30, "adx": 20, "dist_to_high_pct": 5.0,
              "volume_ratio": 1.0, "ma_bull": False}
    key, reason = classify(regime, cfg)
    assert key == "defense"
    assert "防守" in reason


def test_classify_trend_strong():
    cfg = config_manager.get()
    # 强趋势 + 接近 N 日高 + 放量
    regime = {"pdi": 25, "mdi": 10, "adx": 35, "dist_to_high_pct": 2.0,
              "volume_ratio": 1.5, "ma_bull": True}
    key, reason = classify(regime, cfg)
    assert key == "trend_strong"
    assert "趋势强攻" in reason


def test_classify_trend_pullback():
    cfg = config_manager.get()
    # 趋势仍在(多头排列) 但已离开高点回踩(距高在 3%~8%)
    regime = {"pdi": 25, "mdi": 10, "adx": 22, "dist_to_high_pct": 6.0,
              "volume_ratio": 1.0, "ma_bull": True}
    key, reason = classify(regime, cfg)
    assert key == "trend_pullback"
    assert "趋势回踩" in reason


def test_classify_range():
    cfg = config_manager.get()
    # 趋势弱(ADX<弱线) -> 震荡
    regime = {"pdi": 20, "mdi": 18, "adx": 12, "dist_to_high_pct": 5.0,
              "volume_ratio": 1.0, "ma_bull": False}
    key, reason = classify(regime, cfg)
    assert key == "range"
    assert "震荡" in reason


def test_classify_fallback_pullback_when_too_deep():
    cfg = config_manager.get()
    # 多头强趋势但距高偏深(>8%) -> 兜底仍判回踩
    regime = {"pdi": 25, "mdi": 10, "adx": 25, "dist_to_high_pct": 20.0,
              "volume_ratio": 1.0, "ma_bull": True}
    key, reason = classify(regime, cfg)
    assert key == "trend_pullback"


def test_classify_priority_defense_overrides_strong():
    """互斥优先级: 同时满足防守与强攻时, 防守(先判定)胜出."""
    cfg = config_manager.get()
    regime = {"pdi": 10, "mdi": 30, "adx": 35, "dist_to_high_pct": 2.0,
              "volume_ratio": 1.5, "ma_bull": True}
    key, _ = classify(regime, cfg)
    assert key == "defense"


def test_classify_is_deterministic():
    """同一输入必得同一输出(可回测/可测试的核心前提)."""
    cfg = config_manager.get()
    regime = {"pdi": 25, "mdi": 10, "adx": 22, "dist_to_high_pct": 6.0,
              "volume_ratio": 1.0, "ma_bull": True}
    a = classify(regime, cfg)
    b = classify(regime, cfg)
    assert a == b


# ---------------------------------------------------------------- mode_for_ind / active_mode
def test_mode_for_ind_picks_each_mode():
    cfg = config_manager.get()
    base = dict(close=100.0, atr14=3.0, ma10=105.0, ma20=100.0, ma60=95.0,
                pdi14=25.0, mdi14=10.0, adx14=22.0, volume_ratio20=1.0,
                volume=2_000_000.0, rsi14=60.0, dc_upper20=102.0)
    cases = {
        "defense": dict(base, mdi14=30.0, pdi14=10.0, adx14=20.0),
        "trend_strong": dict(base, adx14=35.0, dc_upper20=101.5, volume_ratio20=1.5),
        "trend_pullback": dict(base, adx14=22.0, dc_upper20=106.0),
        "range": dict(base, adx14=12.0),
    }
    for expected, last in cases.items():
        d = mode_for_ind(_ind_df(last), cfg)
        assert d.mode_key == expected, f"{expected} got {d.mode_key}"
        assert d.mode == cfg["交易模式"]["modes"][expected]


def test_active_mode_fallbacks():
    cfg = config_manager.get()
    # K 线为 None -> 默认模式
    d = active_mode("X", None, cfg)
    assert d.mode_key == cfg["交易模式"]["default_mode"]
    # K 线不足 30 行 -> 默认模式
    d = active_mode("X", _kline(n=20), cfg)
    assert d.mode_key == cfg["交易模式"]["default_mode"]
    # 模式组禁用 -> 默认模式
    cfg_off = copy.deepcopy(cfg)
    cfg_off["交易模式"]["enabled"] = False
    d = mode_for_ind(_ind_df(dict(close=100.0, atr14=3.0, ma10=105.0, ma20=100.0,
                                  ma60=95.0, pdi14=25.0, mdi14=10.0, adx14=22.0,
                                  volume_ratio20=1.0, volume=2_000_000.0, rsi14=60.0,
                                  dc_upper20=106.0)), cfg_off)
    assert d.mode_key == cfg_off["交易模式"]["default_mode"]
    assert "默认模式" in d.reason


# ---------------------------------------------------------------- Q1 动态多因子打分
def _score_inputs(scenario: dict, profit_price: float, cost: float = 100.0):
    """构造 _score_add 所需的 last/prev/pos/mode_decision."""
    cfg = config_manager.get()
    last = pd.Series({**scenario["last"]})
    prev = pd.Series({**scenario["prev"]})
    pos = PositionInfo(symbol="T", cost=cost, qty=100)
    mode = ModeDecision(
        mode_key="trend_pullback",
        mode={"allow_add": True, "min_add_profit_pct": 3.0, "label": "趋势回踩"},
        regime={"dist_to_high_pct": scenario["dist"], "adx": scenario["adx"]},
        reason="", label="趋势回踩",
    )
    return cfg, last, prev, pos, profit_price, mode


def test_score_add_three_stocks_differentiated():
    """复现用户 72/63/79 场景: 同样浮盈下, 三支票因技术面不同得分不同(子条件可区分).

    - 场景1: 首次触及10日线反弹 + 回踩缩量 + RSI自超买回落 + 良性回踩
    - 场景2: 沿10日线强势 + RSI健康区 + 良性回踩(无缩量)
    - 场景3: 回踩缩量 + RSI健康区(距高偏深, ADX弱 -> 信心乘子低)
    """
    # 场景1
    s1 = {
        "last": {"ma10": 105, "ma20": 100, "close": 106, "volume": 1_000_000,
                 "rsi14": 64, "atr14": 2.0, "dif": 1.0, "dea": 0.5},
        "prev": {"ma10": 105, "close": 104, "volume": 1_500_000, "rsi14": 72},
        "dist": 2.0, "adx": 30.0,
    }
    # 场景2 (同浮盈 6%, 但无缩量/无 RSI 回落)
    s2 = {
        "last": {"ma10": 105, "ma20": 100, "close": 106, "volume": 1_500_000,
                 "rsi14": 50, "atr14": 2.0, "dif": 1.0, "dea": 0.5},
        "prev": {"ma10": 105, "close": 105, "volume": 1_000_000, "rsi14": 50},
        "dist": 2.0, "adx": 30.0,
    }
    # 场景3 (浮盈 4%, 距高偏深, ADX 弱)
    s3 = {
        "last": {"ma10": 105, "ma20": 100, "close": 104, "volume": 1_000_000,
                 "rsi14": 45, "atr14": 2.0, "dif": 1.0, "dea": 0.5},
        "prev": {"ma10": 105, "close": 104, "volume": 1_500_000, "rsi14": 45},
        "dist": 12.0, "adx": 15.0,
    }
    cfg, l1, p1, pos1, price1, m1 = _score_inputs(s1, 106.0)
    cfg, l2, p2, pos2, price2, m2 = _score_inputs(s2, 106.0)
    cfg, l3, p3, pos3, price3, m3 = _score_inputs(s3, 104.0)

    sc1, sub1 = eng._score_add(cfg, None, l1, p1, pos1, price1, m1)
    sc2, sub2 = eng._score_add(cfg, None, l2, p2, pos2, price2, m2)
    sc3, sub3 = eng._score_add(cfg, None, l3, p3, pos3, price3, m3)

    # 子条件文案明显区分
    assert set(sub1) != set(sub2) != set(sub3)
    assert "首次触及10日线反弹" in sub1
    assert "回踩缩量" in sub1 and "回踩缩量" in sub3 and "回踩缩量" not in sub2
    assert "沿10日线强势" in sub2
    assert "RSI自超买回落" in sub1
    # 分数区分且有序
    assert sc1 > sc2 > sc3
    # 浮盈不再是主导: 场景1/2 浮盈同为 6%, 但分数不同
    assert sc1 != sc2


def test_check_add_tags_mode_and_reason():
    """_check_add 返回加仓信号并写回 mode + 可解释子条件 reason."""
    cfg = config_manager.get()
    last = pd.Series({"ma10": 105, "ma20": 100, "close": 106, "volume": 1_000_000,
                      "rsi14": 64, "atr14": 2.0, "dif": 1.0, "dea": 0.5})
    prev = pd.Series({"ma10": 105, "close": 104, "volume": 1_500_000, "rsi14": 72})
    pos = PositionInfo(symbol="300750", cost=100.0, qty=100)
    mode = ModeDecision(
        mode_key="trend_pullback",
        mode={"allow_add": True, "min_add_profit_pct": 3.0, "label": "趋势回踩"},
        regime={"dist_to_high_pct": 2.0, "adx": 30.0},
        reason="趋势回踩: ADX=30", label="趋势回踩",
    )
    sig = eng._check_add(cfg, None, last, prev, pos, 106.0, "宁德时代", mode)
    assert sig is not None
    assert sig.type == "BUY_ADD"
    # 模式标签经由 reason 透传(供 LLM 解说); .mode 字段由 evaluate 的 _tag 统一写回,
    # 该路径由 test_evaluate_tags_signal_with_mode 覆盖。
    assert "加仓:" in sig.reason
    assert "模式=趋势回踩" in sig.reason
    assert "首次触及10日线反弹" in sig.reason


def test_check_add_defense_mode_blocks_add():
    """防守模式 allow_add=False -> 即使浮盈达标也不加仓."""
    cfg = config_manager.get()
    last = pd.Series({"ma10": 105, "ma20": 100, "close": 106, "volume": 1_000_000,
                      "rsi14": 64, "atr14": 2.0, "dif": 1.0, "dea": 0.5})
    prev = pd.Series({"ma10": 105, "close": 104, "volume": 1_500_000, "rsi14": 72})
    pos = PositionInfo(symbol="X", cost=100.0, qty=100)
    mode = ModeDecision(
        mode_key="defense",
        mode={"allow_add": False, "min_add_profit_pct": 999.0, "label": "防守"},
        regime={"dist_to_high_pct": 2.0, "adx": 30.0},
        reason="防守", label="防守",
    )
    sig = eng._check_add(cfg, None, last, prev, pos, 106.0, "X", mode)
    assert sig is None


# ---------------------------------------------------------------- Q2 模式驱动风控/仓位
def _stop_ind() -> pd.DataFrame:
    return pd.DataFrame([{"ma10": 95.0, "ma20": 100.0, "adx14": 25.0}])


def test_check_stop_uses_mode_stop_loss():
    """止损线优先取当前模式的 stop_loss_pct; 缺失时回退全局风控."""
    cfg = config_manager.get()
    ind = _stop_ind()
    last = ind.iloc[-1]
    pos = PositionInfo(symbol="X", cost=100.0, qty=100)

    # 趋势强攻模式止损 6% -> 94.5 元(>-6%) 不触发
    strong = ModeDecision(mode_key="trend_strong", mode={"stop_loss_pct": 6.0},
                          regime={}, reason="", label="趋势强攻")
    assert eng._check_stop(cfg, ind, last, pos, 94.5, "X", strong) is None

    # 防守模式止损 3% -> 94.5 元(<=-3%) 触发
    defense = ModeDecision(mode_key="defense", mode={"stop_loss_pct": 3.0},
                           regime={}, reason="", label="防守")
    s = eng._check_stop(cfg, ind, last, pos, 94.5, "X", defense)
    assert s is not None and s.type == "SELL_STOP"

    # 模式未定义 stop_loss_pct -> 回退全局 5% -> 94.5 触发, 96 不触发
    fallback = ModeDecision(mode_key="trend_pullback", mode={},
                            regime={}, reason="", label="趋势回踩")
    assert eng._check_stop(cfg, ind, last, pos, 94.5, "X", fallback) is not None
    assert eng._check_stop(cfg, ind, last, pos, 96.0, "X", fallback) is None


def test_advice_buy_first_and_add_use_mode_ratios():
    """计划文案使用当前模式的金字塔比例(震荡首仓70% / 趋势首仓50%)."""

    class _P:
        pyramid_stage = 0

    range_mode = ModeDecision(
        mode_key="range",
        mode={"pyramid_ratios": [0.7, 0.3], "max_stages": 2},
        regime={}, reason="", label="震荡",
    )
    trend_mode = ModeDecision(
        mode_key="trend_pullback",
        mode={"pyramid_ratios": [0.5, 0.3, 0.2], "max_stages": 3},
        regime={}, reason="", label="趋势回踩",
    )
    assert "70%" in PlanGenerator._advice_buy_first(100.0, range_mode)
    assert "50%" in PlanGenerator._advice_buy_first(100.0, trend_mode)

    # 已持仓(stage 0) -> 建议下一档
    p = _P()
    assert "第 2 档" in PlanGenerator._advice_buy_add(p, None, range_mode)
    assert "第 2 档" in PlanGenerator._advice_buy_add(p, None, trend_mode)
    # 趋势模式已加 1 档(stage 1) -> 第 3 档 20%
    p.pyramid_stage = 1
    assert "第 3 档" in PlanGenerator._advice_buy_add(p, None, trend_mode)
    assert "20%" in PlanGenerator._advice_buy_add(p, None, trend_mode)


def test_plan_generate_includes_mode_line(tmp_engine):
    """生成计划含"当前模式"解说行, 且止损线取模式 stop_loss_pct."""
    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    mode = ModeDecision(
        mode_key="trend_pullback",
        mode={"pyramid_ratios": [0.5, 0.3, 0.2], "stop_loss_pct": 5.0,
              "take_profit_ratios": [0.2, 0.3, 0.5],
              "atr_multipliers": [1.5, 3.0, 5.0]},
        regime={}, reason="趋势回踩: ADX=25 多头排列, 距高5.0%", label="趋势回踩",
    )
    sig = Signal(type="BUY_ADD", symbol="300750", name="宁德时代", direction="buy",
                 strength=78.0, reason="回踩20日线企稳", price=110.0)
    plan = gen.generate("300750", "宁德时代", sig,
                        portfolio={"total_pct": 65.0}, mode=mode)
    assert "当前模式" in plan["content"]
    assert "趋势回踩" in plan["content"]
    # 止损线取模式 5%(成本含费约 100.06 -> 95.06), 文案标注"成本下移5%"
    assert "(成本下移5%)" in plan["content"]


def test_pyramid_plan_uses_mode_ratios(tmp_engine):
    """持仓详情金字塔计划: 提供 K 线时取当前模式的金字塔比例."""
    position_manager.open_or_add("600000", "浦发银行", 100, 10.0, "首仓", None)
    kline = _kline(n=40)
    with __import__("app").db.session_scope() as s:
        plan = position_manager.pyramid_plan("600000", session=s, kline_df=kline)
    modes = config_manager.get()["交易模式"]["modes"]
    assert plan["mode"] in modes
    assert plan["ratios"] == modes[plan["mode"]]["pyramid_ratios"]


def test_evaluate_tags_signal_with_mode():
    """evaluate 返回的任意信号都写回当前交易模式(选型规则化, 不依赖 LLM)."""
    kline = _kline(n=80, seed=7)  # 强趋势
    # 放大斜率确保出信号
    kline = kline.copy()
    kline["close"] = kline["close"] * (1.004 ** kline.index.to_series())
    kline["open"] = kline["open"] * (1.004 ** kline.index.to_series())
    kline["high"] = kline["high"] * (1.004 ** kline.index.to_series())
    kline["low"] = kline["low"] * (1.004 ** kline.index.to_series())
    sig = eng.evaluate("300750", "宁德时代", kline_df=kline, position=None)
    assert sig is not None
    assert sig.mode in config_manager.get()["交易模式"]["modes"]
    assert sig.indicators_snapshot.get("mode") == sig.mode
