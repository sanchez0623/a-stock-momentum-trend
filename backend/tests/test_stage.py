"""趋势阶段识别(方案B)单测: detect_stage 各阶段 + 加速期前/中/后细分 + 加减分生效 + score_indicators 字段输出."""

from __future__ import annotations

import copy

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


def _accel_ind(n: int = 30, cross_at: int | None = 15, rsi: float = 62.0,
               bias_pct: float = 1.5) -> pd.DataFrame:
    """手工构造"多头排列 + 动量健康"的加速期指标表.

    ma10 于第 cross_at 根上穿 ma20(趋势年龄 = n-1-cross_at, 需 >= 4 才不会落进启动期);
    cross_at=None 表示回看窗口内无金叉的老趋势; bias_pct 为现价相对 ma10 的乖离(%).
    """
    dates = pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d")
    ma10 = np.full(n, 108.0) if cross_at is None \
        else np.where(np.arange(n) < cross_at, 104.0, 108.0)
    return pd.DataFrame({
        "date": dates,
        "close": np.full(n, 108.0 * (1 + bias_pct / 100)),
        "ma10": ma10,
        "ma20": np.full(n, 106.0),
        "ma60": np.full(n, 100.0),
        "adx14": np.full(n, 40.0),           # 强趋势且早已达标(无"首次达标"事件)
        "rsi14": np.full(n, rsi),
        "roc12": np.full(n, 5.0),            # 恒正(无"由负转正"事件)
        "macd_hist": np.full(n, 0.5),        # 恒正(无金叉/无衰竭)
        "volume_ratio20": np.full(n, 1.2),
    })


def _accel_cfg() -> dict:
    """深拷贝配置并强制开启阶段识别(前序用例会原地改配置且不恢复, 防串扰)."""
    cfg = copy.deepcopy(config_manager.get())
    cfg["趋势阶段"]["enabled"] = True
    return cfg


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
    """多头排列 + 动量健康 + 无刚发生事件 -> 加速期(中期), 子阶段加分."""
    cfg = _accel_cfg()
    res = detect_stage(_accel_ind(n=30, cross_at=15, rsi=62.0, bias_pct=1.5), cfg)
    assert res["stage"] == "accelerate", res
    assert res["stage_sub"] == "mid" and res["trend_age"] == 14
    assert res["bonus"] == cfg["趋势阶段"]["accel_mid_bonus"]
    assert res["penalty"] == 0


# ---------------------------------------------------------------- 加速期细分
def test_accel_sub_early():
    """趋势刚理顺(年龄<=12)且乖离/RSI 温和 -> 加速前期(首仓建仓区)."""
    res = detect_stage(_accel_ind(n=30, cross_at=25, rsi=60.0, bias_pct=1.5), _accel_cfg())
    assert res["stage"] == "accelerate" and res["stage_sub"] == "early", res
    assert res["trend_age"] == 4
    assert res["bonus"] > 0


def test_accel_sub_mid():
    """年龄介于前后期阈值之间且热度未达后期线 -> 加速中期(动量最强)."""
    cfg = _accel_cfg()
    res = detect_stage(_accel_ind(n=30, cross_at=15, rsi=62.0, bias_pct=1.5), cfg)
    assert res["stage_sub"] == "mid" and res["trend_age"] == 14, res
    assert res["bonus"] == cfg["趋势阶段"]["accel_mid_bonus"]


def test_accel_sub_late_by_age():
    """趋势年龄超过后期线 -> 加速后期."""
    res = detect_stage(_accel_ind(n=50, cross_at=5, rsi=62.0, bias_pct=1.5), _accel_cfg())
    assert res["stage_sub"] == "late" and res["trend_age"] == 44, res


def test_accel_sub_late_by_bias():
    """年龄虽小但乖离已达后期线(短期暴涨) -> 直接后期, 不宜追."""
    res = detect_stage(_accel_ind(n=30, cross_at=25, rsi=60.0, bias_pct=8.0), _accel_cfg())
    assert res["stage"] == "accelerate" and res["stage_sub"] == "late", res


def test_accel_sub_late_by_rsi():
    """RSI 达后期线(过热线预警带) -> 加速后期."""
    res = detect_stage(_accel_ind(n=30, cross_at=25, rsi=72.0, bias_pct=1.5), _accel_cfg())
    assert res["stage"] == "accelerate" and res["stage_sub"] == "late", res


def test_accel_sub_old_trend_is_late():
    """回看窗口内无金叉的老趋势 -> 年龄未知, 按后期处理."""
    res = detect_stage(_accel_ind(n=30, cross_at=None, rsi=62.0, bias_pct=1.5), _accel_cfg())
    assert res["stage"] == "accelerate" and res["stage_sub"] == "late", res
    assert res["trend_age"] is None


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


def test_score_indicators_exposes_accel_sub_fields():
    """score_indicators 透传 stage_sub/trend_age, 子阶段加分计入总分."""
    cfg = _accel_cfg()
    ind = _accel_ind(n=30, cross_at=15, rsi=62.0, bias_pct=1.5)
    on = score_indicators(ind, cfg)
    assert on["stage"] == "accelerate" and on["stage_sub"] == "mid", on
    assert on["trend_age"] == 14
    assert "主升段" in on["reason"]
    cfg["趋势阶段"]["enabled"] = False
    off = score_indicators(ind, cfg)
    assert on["total"] > off["total"]  # 中期加分生效


# ---------------------------------------------------------------- end 参数(回测逐日评分)
def test_score_indicators_end_consistency():
    """end 参数: score_indicators(ind, end=k) 与对前缀 iloc[:k] 直接评分完全一致(回测防口径漂移)."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    rising = [101, 102.2, 103.6, 105.2] + [round(105.2 + i * 0.4, 2) for i in range(1, 30)]
    ind = compute_all(_make_df(base + rising))
    for k in (40, 90, 150):
        full = score_indicators(ind.iloc[:k])
        part = score_indicators(ind, end=k)
        for key in ("trend_score", "momentum_score", "volume_score", "total",
                    "stage", "stage_sub", "close", "rsi", "consistency", "date"):
            assert part[key] == full[key], f"end={k} {key}: {part[key]} != {full[key]}"


def test_score_indicators_end_default_is_last():
    """end 缺省 = 末行: score_indicators(ind) 与 score_indicators(ind, end=len(ind)) 一致."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    ind = compute_all(_make_df(base + [101, 102.2, 103.6, 105.2]))
    assert score_indicators(ind, end=len(ind))["total"] == score_indicators(ind)["total"]


# ---------------------------------------------------------------- 动量确认打折(回测校准)
def test_momentum_confirm_discount_applied():
    """强趋势+弱动量(高位滞涨画像: adx40+多头+roc5/rsi62) -> 趋势分×0.7, 总分掉出50-60."""
    cfg = _accel_cfg()
    ind = _accel_ind(rsi=62.0)  # roc=5, rsi=62 -> 动量分<20; adx=40+多头 -> 趋势分32
    s = score_indicators(ind, cfg)
    assert s["momentum_score"] < 20, s["momentum_score"]
    assert s["trend_discount"] > 0
    assert abs(s["trend_score"] - 32 * 0.7) < 0.1  # 32 -> 22.4
    assert s["total"] < 50  # 打折前~56, 打折后~46.5, 掉出 50-60 挤占区
    assert "打折" in s["risk"]  # 理由与分数同源


def test_momentum_confirm_no_discount_when_momentum_strong():
    """强趋势+强动量(健康主升票) -> 不打折, 分数不受影响."""
    cfg = _accel_cfg()
    ind = _accel_ind(rsi=62.0)
    ind["roc12"] = 15.0  # roc 分拉满, 动量分 >= 20
    s = score_indicators(ind, cfg)
    assert s["momentum_score"] >= 20, s["momentum_score"]
    assert s["trend_discount"] == 0
    assert abs(s["trend_score"] - 32) < 0.1  # 全额计分


def test_momentum_confirm_no_discount_when_trend_weak():
    """弱趋势(趋势分<25)即使动量弱也不打折: 打折只针对"强趋势无确认"的背离票."""
    cfg = _accel_cfg()
    ind = _accel_ind(rsi=62.0)
    ind["adx14"] = 18.0  # ADX 低于阈值 -> 趋势分骤降
    s = score_indicators(ind, cfg)
    assert s["trend_discount"] == 0


def test_momentum_confirm_disabled_by_config():
    """配置关闭后不打折(回退旧行为)."""
    cfg = _accel_cfg()
    cfg["趋势"]["momentum_confirm_enabled"] = False
    ind = _accel_ind(rsi=62.0)
    s = score_indicators(ind, cfg)
    assert s["trend_discount"] == 0
    assert abs(s["trend_score"] - 32) < 0.1


def test_score_indicators_without_reason():
    """with_reason=False: 不拼装人话理由(回测逐日高频调用提速), 得分字段不受影响."""
    base = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    ind = compute_all(_make_df(base + [101, 102.2, 103.6, 105.2]))
    full = score_indicators(ind)
    nr = score_indicators(ind, with_reason=False)
    for key in ("reason", "risk", "tags", "detail"):
        assert key not in nr
    assert nr["total"] == full["total"]
    assert nr["trend_score"] == full["trend_score"]
