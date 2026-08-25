"""通用动态阈值工具: 基于 ATR 波动率 × 倍数 × 市况系数, 替代各处一刀切的固定百分比.

动机(2026-08-25):
- 固定阈值对高波动股太松(预警形同虚设/日常噪音), 对低波动股太紧(永不触发/误报).
- 止盈档与做T波幅已动态化; 本模块把同一模式抽成通用函数, 供:
  * 盘中监控: 止损线逼近(0.5×ATR%)、异动涨跌幅(max(2×ATR%, 3%))
  * 趋势阶段: 过热乖离(3×ATR%, 下限8%)
  * 市况分类: 回踩深度(2.5×ATR%, 下限5%)
  * 信号引擎: 做T波幅(见 SignalEngine._swing_threshold, 已实现)

设计要点:
- 纯函数, 不依赖数据库/网络, 可回测;
- 所有调用方保留配置开关(dynamic/fixed)与原固定值兜底, 动态计算失败自动回退;
- 市况系数与做T保持一致(SWING_REGIME_MULT), 语义统一.
"""

from __future__ import annotations

from typing import Any

# 市况系数: 趋势强(阈值降低易触发) < 震荡(收紧防噪) < 防守(最难触发)
REGIME_MULT: dict[str, float] = {
    "trend_strong": 0.6,
    "trend_pullback": 0.8,
    "range": 1.2,
    "defense": 1.5,
    "unknown": 1.0,
}


def dynamic_threshold(
    base_mult: float,
    atr_pct: float,
    *,
    regime: str = "",
    floor: float = 0.0,
    ceil: float = 0.0,
) -> float:
    """通用动态阈值计算.

    阈值 = base_mult × ATR% × 市况系数, 再做上下限钳位.

    Args:
        base_mult: ATR 倍数(如 0.5 表示半个日波动率).
        atr_pct: ATR 占价比(小数, 如 0.025 = 2.5%; 传入百分数亦可, 单位随 base_mult 语义).
        regime: 市况键(trend_strong/...), 空则系数=1.0.
        floor: 下限(与 atr_pct 同单位), 0=不限.
        ceil: 上限(与 atr_pct 同单位), 0=不限.

    Returns:
        动态阈值(与 atr_pct 同单位).

    Examples:
        >>> dynamic_threshold(0.5, 0.025)          # 止损逼近: 0.5×2.5% = 1.25%
        0.0125
        >>> dynamic_threshold(2.0, 0.04, floor=0.03)  # 异动: 2×4%=8%
        0.08
    """
    if atr_pct <= 0:
        # ATR 缺失(数据不足): 返回 0 让调用方走自身兜底
        return 0.0
    mult = REGIME_MULT.get(regime, 1.0) if regime else 1.0
    v = base_mult * atr_pct * mult
    if floor > 0 and v < floor:
        v = floor
    if ceil > 0 and v > ceil:
        v = ceil
    return v


def atr_pct_from_ind(last: dict[str, Any] | Any) -> float:
    """从指标末行取 ATR 占价比(小数). 失败返回 0(调用方走兜底).

    Args:
        last: pandas Series(指标表末行, 含 atr14/close) 或 dict.
    """
    try:
        atr = float(last.get("atr14"))
        close = float(last.get("close"))
        if close > 0 and atr > 0:
            return atr / close
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.0
