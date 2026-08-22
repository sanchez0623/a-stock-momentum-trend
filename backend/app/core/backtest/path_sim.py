"""日内路径模拟(方案 v2 §8 扩展): 用日线 OHLC 构造确定性盘中路径.

背景: 回测历史区间拿不到真实分钟线(数据源仅保留近 1-2 个月), 为模拟「盘中实时」
效果(止损/做T/买入信号在盘中触发而非收盘判定), 用当日 OHLC 构造 N 段日内路径:
- 每段有 open/high/low/close(10 分钟粒度: 4 小时交易 -> 24 段)
- 路径确定性强(固定 seed 可复现), 全程约束在 [low, high] 内, 首尾锚定 open/close
- 构造法: 三段式锚点(open -> 触及 low -> 触及 high -> close, 按开收相对位置调整
  顺序), 段内线性插值 + 残余振幅, 保证 seg_high >= max(seg_open, seg_close) 且
  seg_low <= min(seg_open, seg_close)

回测执行层逐段推进: 每段末用「T-1 收盘指标 + 段内价格」调信号引擎(与实盘盘中
评估同语义), 命中即按段价成交 —— 全部信号(止损/减仓/加仓/首仓/做T)均为盘中触发.
"""

from __future__ import annotations

import math

# 交易时段 240 分钟(9:30-11:30 + 13:00-15:00)
TRADING_MINUTES = 240

DEFAULT_MINUTES = 10
ALLOWED_MINUTES = (5, 10, 15, 30)


def segments_for(minutes: int = DEFAULT_MINUTES) -> int:
    """粒度 -> 每日段数(不足 1 段按 1 段)."""
    if minutes not in ALLOWED_MINUTES:
        minutes = DEFAULT_MINUTES
    return max(1, TRADING_MINUTES // minutes)


def _anchor_points(o: float, h: float, low: float, c: float) -> list[float]:
    """三段式锚点: 从 open 出发依次触及 low/high, 终点 close.

    按 open/close 相对位置决定 low/high 的访问顺序, 保证路径单调段不回头:
    - 收高于开(涨): open -> low(回踩) -> high(冲高) -> close
    - 收低于开(跌): open -> high(反抽) -> low(探底) -> close
    锚点值约束在 [l, h] 内(与真实 OHLC 自洽).
    """
    if c >= o:
        return [o, low, h, c]
    return [o, h, low, c]


def build_intraday_path(open_px: float, high: float, low: float, close: float,
                        segments: int | None = None,
                        minutes: int = DEFAULT_MINUTES,
                        seed: int = 42) -> list[dict[str, float]]:
    """构造确定性日内路径.

    返回 [{open, high, low, close}, ...] 共 N 段:
    - 第 1 段 open = 当日 open, 最后段 close = 当日 close
    - 全程价格在 [low, high] 内, 段 high/low 与段 open/close 自洽
    - 同一 (ohlc, segments, seed) 结果恒定(可复现)
    """
    n = segments or segments_for(minutes)
    if n <= 1:
        return [{"open": open_px, "high": max(high, open_px, close),
                 "low": min(low, open_px, close), "close": close}]
    hi = max(high, open_px, close)
    lo = min(low, open_px, close)
    if hi <= lo:
        hi = lo + 1e-9
    anchors = _anchor_points(open_px, hi, lo, close)

    def _interp(a: float, b: float, t: float) -> float:
        return a + (b - a) * t

    # 扰动: 确定性正弦(seed 派生相位), 幅度 = 当日波幅的小比例, 不越界
    amp = (hi - lo) * 0.15
    phase = (seed * 7 + 3) % 360
    out: list[dict[str, float]] = []
    for i in range(n):
        t0 = i / n
        t1 = (i + 1) / n
        # 锚点区间 [k, k+1] 内线性: 定位锚点分段
        k = t0 * (len(anchors) - 1)
        ki = min(int(k), len(anchors) - 2)
        kf = k - ki
        seg_o = _interp(anchors[ki], anchors[ki + 1], kf)
        k2 = t1 * (len(anchors) - 1)
        ki2 = min(int(k2), len(anchors) - 2)
        kf2 = k2 - ki2
        seg_c = _interp(anchors[ki2], anchors[ki2 + 1], kf2)
        # 段 high/low 覆盖该段经过的锚点极值(真实路径会扫到当日高低点, 做T/止损才能盘中触发)
        seg_anchors = anchors[ki: ki2 + 2]
        seg_high = min(max(max(seg_anchors), seg_o, seg_c), hi)
        seg_low = max(min(min(seg_anchors), seg_o, seg_c), lo)
        # 残余正弦扰动(确定性), 保证段内高低不越界
        wob = math.sin(math.radians(phase + i * 37)) * amp
        seg_o = min(max(seg_o, lo), hi)
        seg_c = min(max(seg_c + wob, lo), hi)
        seg_high = min(max(seg_high, seg_o, seg_c), hi)
        seg_low = max(min(seg_low, seg_o, seg_c), lo)
        out.append({
            "open": round(seg_o, 4),
            "high": round(seg_high, 4),
            "low": round(seg_low, 4),
            "close": round(seg_c, 4),
        })
    # 锚定首尾(保证当日 open/close 精确)
    out[0]["open"] = round(open_px, 4)
    out[-1]["close"] = round(close, 4)
    return out


def validate_path(path: list[dict[str, float]], open_px: float, high: float,
                  low: float, close: float) -> bool:
    """路径自检: 首尾锚定 / 全程边界内 / 段高低与开收自洽."""
    if not path:
        return False
    if abs(path[0]["open"] - open_px) > 1e-6 or abs(path[-1]["close"] - close) > 1e-6:
        return False
    hi = max(high, open_px, close)
    lo = min(low, open_px, close)
    for seg in path:
        if seg["high"] > hi + 1e-6 or seg["low"] < lo - 1e-6:
            return False
        if seg["high"] < max(seg["open"], seg["close"]) - 1e-6:
            return False
        if seg["low"] > min(seg["open"], seg["close"]) + 1e-6:
            return False
        if seg["high"] < seg["low"]:
            return False
    return True


# 保留类型导出(供执行层引用路径段结构)
PathSeg = dict[str, float]
