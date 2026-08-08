"""市况分类器 + 交易模式注册表(方案 Q2: 多模式 + 规则化市况分类器).

设计原则
- 选型由**规则**做决策, LLM 只做"解说员"不碰选型与仓位: 本模块输出的 mode 选择
  与仓位/止损/加仓比率, 全部来自 config 中的阈值与模式定义, 可被回测与单测确定性验证。
- 每种模式是一个**配置对象**(各自的比率/止损/加仓规则), 由数据(而非代码分支)切换:
  模式定义放在 config["交易模式"]["modes"], 判据阈值在 config["交易模式"]["classifier"]。
- 安全护栏: "交易模式" 分组已加入 tuning.FORBIDDEN_GROUPS, AI 调参无法改动选型与仓位
  (与 "仓位" 分组同等级别)。

对外入口
- mode_for_ind(ind, cfg): 入参为已算好指标的 DataFrame(引擎内部复用, 避免重复计算)。
- active_mode(symbol, kline_df, cfg): 由原始 K 线算指标后分类(API 层使用)。
两者都返回 ModeDecision(mode_key, mode, regime, reason, label)。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.config import config_manager
from app.core.indicators import compute_all


# 模式分组在 config 中的键与默认值来源
MODE_GROUP = "交易模式"

# 默认模式键(行情不足/未启用时回退)
DEFAULT_MODE_KEY = "trend_pullback"


@dataclass
class ModeDecision:
    """一次市况分类的结果."""

    mode_key: str
    mode: dict[str, Any]
    regime: dict[str, Any]              # 原始市况特征(供解释/复盘)
    reason: str                        # 人为可读的"为什么选了这个模式"(规则生成, 供 LLM 解说)
    label: str = ""


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def _compute_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """与 SignalEngine._indicators 保持一致的指标计算(避免重复定义参数)."""
    trend = cfg["趋势"]
    momentum = cfg["动量"]
    volume = cfg["量能"]
    return compute_all(
        df,
        ma_short=trend["ma_short"],
        ma_mid=trend["ma_mid"],
        ma_long=trend["ma_long"],
        macd_fast=momentum["macd_fast"],
        macd_slow=momentum["macd_slow"],
        macd_signal=momentum["macd_signal"],
        rsi_period=momentum["rsi_period"],
        adx_period=trend.get("adx_period", 14),
        roc_period=momentum["roc_period"],
        volume_ma=volume["volume_ma"],
        atr_period=cfg["仓位"].get("atr_period", 14),
    )


def regime_features(ind: pd.DataFrame, cfg: dict) -> dict[str, Any]:
    """从指标表末行提取市况特征."""
    if ind is None or len(ind) == 0:
        return {}
    last = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else last
    trend = cfg["趋势"]
    volume = cfg["量能"]
    ma_s = f"ma{trend['ma_short']}"
    ma_m = f"ma{trend['ma_mid']}"
    ma_l = f"ma{trend['ma_long']}"
    adx_p = trend.get("adx_period", 14)
    rsi_p = cfg["动量"]["rsi_period"]
    cl = cfg[MODE_GROUP]["classifier"]
    dc_p = cl.get("donchian_period", 20)
    dc_upper = _f(last.get(f"dc_upper{dc_p}"))
    close = _f(last["close"])
    atr14 = _f(last.get("atr14"))
    atr_pct = (atr14 / close) if (close > 0 and atr14 > 0) else 0.03
    ma_s_v, ma_m_v, ma_l_v = _f(last.get(ma_s)), _f(last.get(ma_m)), _f(last.get(ma_l))
    pdi = _f(last.get(f"pdi{adx_p}"))
    mdi = _f(last.get(f"mdi{adx_p}"))
    adx = _f(last.get(f"adx{adx_p}"))
    vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
    vol_now = _f(last.get("volume"))
    vol_prev = _f(prev.get("volume"))
    rsi_now = _f(last.get(f"rsi{rsi_p}"), 50)
    rsi_prev = _f(prev.get(f"rsi{rsi_p}"), 50)
    dist_to_high_pct = ((dc_upper - close) / close * 100) if (dc_upper > 0 and close > 0) else 0.0
    ma_bull = ma_s_v > ma_m_v > ma_l_v
    return {
        "close": close,
        "atr_pct": round(atr_pct, 4),
        "adx": round(adx, 2),
        "pdi": round(pdi, 2),
        "mdi": round(mdi, 2),
        "ma_bull": ma_bull,
        "ma_s_v": ma_s_v,
        "ma_m_v": ma_m_v,
        "ma_l_v": ma_l_v,
        "volume_ratio": round(vr, 2),
        "vol_now": vol_now,
        "vol_prev": vol_prev,
        "rsi_now": round(rsi_now, 2),
        "rsi_prev": round(rsi_prev, 2),
        "dist_to_high_pct": round(dist_to_high_pct, 2),
    }


def classify(regime: dict, cfg: dict) -> tuple[str, str]:
    """依据市况特征确定性选 mode_key, 返回 (mode_key, reason).

    判定顺序(互斥, 先到先得):
      1. 防守: 空头占优(-DI>+DI) 且有一定趋势(ADX>=弱线)
      2. 趋势强攻: 趋势强(ADX>=强线) + 接近/突破 N日高 + 放量
      3. 趋势回踩: 趋势仍在(多头排列+ADX>=弱线) 但已离开高点回踩
      4. 震荡: 趋势弱(ADX<弱线)
      5. 兜底: 多头强趋势但距高偏深 -> 仍判回踩
    """
    cl = cfg[MODE_GROUP]["classifier"]
    adx_strong = cl["adx_strong"]
    adx_weak = cl["adx_weak"]
    breakout_dist = cl["breakout_dist_pct"]
    pullback_band = cl.get("pullback_dist_pct", 8.0)
    vol_active = cl["volume_ratio_active"]
    dc_p = cl.get("donchian_period", 20)
    adx = regime.get("adx", 0.0)
    dist = regime.get("dist_to_high_pct", 0.0)
    vr = regime.get("volume_ratio", 0.0)
    ma_bull = regime.get("ma_bull", False)
    pdi = regime.get("pdi", 0.0)
    mdi = regime.get("mdi", 0.0)

    # 1. 防守: 空头占优
    if mdi > pdi and adx >= adx_weak:
        return "defense", (
            f"防守: -DI({mdi:.0f})>+DI({pdi:.0f}) 空头占优, ADX={adx:.0f} 趋势未散, "
            "仅执行止损, 不加仓不新建"
        )
    # 2. 趋势强攻: 强趋势 + 突破区 + 放量
    if adx >= adx_strong and dist <= breakout_dist and vr >= vol_active:
        return "trend_strong", (
            f"趋势强攻: ADX={adx:.0f}(>={adx_strong}) 接近{dc_p}日高"
            f"(距高{dist:.1f}%<={breakout_dist}%) 量比{vr:.1f}(>={vol_active})"
        )
    # 3. 趋势回踩: 趋势仍在但已离开高点回踩
    if adx >= adx_weak and ma_bull and breakout_dist < dist <= pullback_band:
        return "trend_pullback", (
            f"趋势回踩: ADX={adx:.0f}(>={adx_weak}) 多头排列, 距高{dist:.1f}%"
            f"(回踩区间 {breakout_dist}%~{pullback_band}%)"
        )
    # 4. 震荡: 趋势弱
    if adx < adx_weak:
        return "range", f"震荡: ADX={adx:.0f}(<{adx_weak}) 无明确趋势, 轻仓小加"
    # 5. 兜底: 多头强趋势但距高偏深
    if adx >= adx_weak and ma_bull:
        return "trend_pullback", (
            f"趋势回踩: ADX={adx:.0f} 多头排列, 距高{dist:.1f}% 偏深但趋势未破"
        )
    # 极端兜底
    return cfg[MODE_GROUP].get("default_mode", DEFAULT_MODE_KEY), "默认模式(无明确市况特征)"


def _default_decision(cfg: dict) -> ModeDecision:
    modes = cfg[MODE_GROUP]["modes"]
    key = cfg[MODE_GROUP].get("default_mode", DEFAULT_MODE_KEY)
    md = modes.get(key, {})
    return ModeDecision(
        mode_key=key, mode=md, regime={},
        reason="模式分类未启用或行情不足, 使用默认模式", label=md.get("label", key),
    )


def mode_for_ind(ind: pd.DataFrame | None, cfg: dict | None = None) -> ModeDecision:
    """由已算好指标的 DataFrame 分类(引擎内部复用)."""
    cfg = cfg or config_manager.get()
    if not cfg.get(MODE_GROUP, {}).get("enabled", True):
        return _default_decision(cfg)
    if ind is None or len(ind) < 2:
        return _default_decision(cfg)
    regime = regime_features(ind, cfg)
    key, reason = classify(regime, cfg)
    modes = cfg[MODE_GROUP]["modes"]
    md = modes.get(key, modes.get(cfg[MODE_GROUP].get("default_mode", DEFAULT_MODE_KEY), {}))
    return ModeDecision(
        mode_key=key, mode=md, regime=regime, reason=reason, label=md.get("label", key),
    )


def active_mode(symbol: str, kline_df: pd.DataFrame | None, cfg: dict | None = None) -> ModeDecision:
    """由原始 K 线分类(API 层使用). 行情不足或模式关闭时回退默认模式."""
    cfg = cfg or config_manager.get()
    if not cfg.get(MODE_GROUP, {}).get("enabled", True) or kline_df is None or len(kline_df) < 30:
        return _default_decision(cfg)
    ind = _compute_indicators(kline_df, cfg)
    return mode_for_ind(ind, cfg)
