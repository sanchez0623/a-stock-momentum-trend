"""趋势阶段识别(方案B)单测: detect_stage 各阶段 + 加减分生效 + score_indicators 字段输出."""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.config import config_manager
from app.core.indicators import compute_all
from app.core.screener.engine import detect_stage, score_indicators


def _make_df(close_list: list[float]) -> pd.DataFrame:
    """由收盘价序列构造 K 线(平开简化, 恒定量能)."""
    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = np.full(len(close), 5_000_000.0)
    amount = volume * close
    dates = pd.bdate_range("2025-01-02", periods=len(close))
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount,
    })


def _stage(close_list: list[float]) -> dict:
    ind = compute_all(_make_df(close_list))
    return detect_stage(ind, config_manager.get())


def _manual_ind() -> pd.DataFrame:
    """手工构造"多头排列 + 动量健康"的指标表(加速期稳态)."""
    n = 30
    dates = pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "close": np.full(n, 110.0),
        "ma10": np.full(n, 108.0),
        "ma20": np.full(n, 106.0),
        "ma60": np.full(n, 100.0),
        "adx14": np.full(n, 40.0),           # 强趋势且早已达标(无"首次达标"事件)
        "rsi14": np.full(n, 68.0),           # 健康区
        "roc12": np.full(n, 5.0),            # 恒正(无"由负转正"事件)
        "macd_hist": np.linspace(0.3, 0.8, n),  # 持续放大(无金叉/无衰竭)
        "volume_ratio20": np.full(n, 1.2),
    })


# ---------------------------------------------------------------- 各阶段
def test_stage_launch_on_breakout():
    """震荡横盘后温和突破: 金叉+ROC转正 -> 启动期, bonus > 0."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    res = _stage(base + [101, 102.2, 103.6, 105.2])
    assert res["stage"] == "launch", res
    assert res["bonus"] > 0
    assert set(res["events"]) & {"macd_golden", "roc_turn"}


def test_stage_launch_bonus_capped():
    """启动加分超过 launch_bonus_max 时按封顶值."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    ind = compute_all(_make_df(base + [101, 102.2, 103.6, 105.2]))
    cfg = config_manager.get()
    cfg["趋势阶段"]["launch_bonus_max"] = 3.0
    res = detect_stage(ind, cfg)
    assert res["stage"] == "launch"
    assert res["bonus"] <= 3.0


def test_stage_accelerate_on_healthy_uptrend():
    """多头排列 + 动量健康 + 无刚发生事件 -> 加速期, 无加减分."""
    res = detect_stage(_manual_ind(), config_manager.get())
    assert res["stage"] == "accelerate", res
    assert res["bonus"] == 0 and res["penalty"] == 0


def test_stage_overheat_on_extended_rise():
    """缓涨后连续大涨: 乖离过大 + RSI 过热 -> 过热期, penalty 叠加."""
    base = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)]
    res = _stage(base + [150, 155, 160, 165, 170, 175, 180, 185, 190, 195, 200, 205])
    assert res["stage"] == "overheat", res
    assert res["penalty"] >= 3.0  # 至少乖离扣分; RSI 扣分叠加


def test_stage_exhaust_on_rsi_top_and_hist_shrink():
    """冲高回落: RSI 超买 + MACD 柱收窄 -> 衰竭期."""
    base = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)]
    res = _stage(base + [150, 157, 164, 172, 180, 189, 198, 208, 215, 213])
    assert res["stage"] == "exhaust", res
    assert res["penalty"] >= 5.0


def test_stage_downtrend_is_none():
    """单边下跌: 不判阶段, 无加减分."""
    res = _stage([100 - i * 0.5 + np.sin(i / 7) * 1 for i in range(120)])
    assert res["stage"] == "none", res
    assert res["bonus"] == 0 and res["penalty"] == 0


def test_stage_disabled_returns_none():
    """配置 enabled=False 时阶段识别关闭, 无加减分."""
    cfg = config_manager.get()
    cfg["趋势阶段"]["enabled"] = False
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    ind = compute_all(_make_df(base + [101, 102.2, 103.6, 105.2]))
    res = detect_stage(ind, cfg)
    assert res["stage"] == "none" and res["bonus"] == 0


# ---------------------------------------------------------------- 分数联动
def test_score_indicators_exposes_stage_fields():
    """score_indicators 输出 stage 系列字段, 且分数 = 三因子 ± 阶段调整."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    ind = compute_all(_make_df(base + [101, 102.2, 103.6, 105.2]))
    score = score_indicators(ind)
    assert score["stage"] in ("launch", "accelerate", "overheat", "exhaust", "none")
    assert "stage_bonus" in score and "stage_penalty" in score
    base_total = score["trend_score"] + score["momentum_score"] + score["volume_score"]
    assert abs(score["total"] - (base_total + score["stage_bonus"] - score["stage_penalty"])) < 0.05


def test_score_overheat_penalty_reduces_total():
    """过热扣分: 同行情下, 阶段识别开启比关闭总分更低."""
    close_list = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)] \
        + [150, 155, 160, 165, 170, 175, 180, 185, 190, 195, 200, 205]
    ind = compute_all(_make_df(close_list))
    on = score_indicators(ind)
    cfg = config_manager.get()
    cfg["趋势阶段"]["enabled"] = False
    off = score_indicators(ind, cfg)
    assert on["stage"] == "overheat"
    assert on["stage_penalty"] > 0
    assert on["total"] < off["total"]
