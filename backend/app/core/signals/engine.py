"""信号引擎核心实现.

设计要点:
- 输入: 行情 K线 + 可选持仓 + 可选实时行情 -> 输出 Signal | None
- 规则全部按方案 §4.3 落地, 纯函数式判定, 便于单测与回放
- 配置从 config_manager 实时读取(热更新生效)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.config import config_manager
from app.core.indicators import compute_all

# 五类信号类型
signal_types = ("BUY_FIRST", "BUY_ADD", "SELL_REDUCE", "SELL_STOP", "T_BUY", "T_SELL")


@dataclass
class PositionInfo:
    """轻量持仓信息(信号引擎不依赖数据库模型)."""

    symbol: str = ""
    cost: float = 0.0
    qty: int = 0

    @property
    def has_position(self) -> bool:
        return self.qty > 0


@dataclass
class Signal:
    type: str
    symbol: str
    name: str = ""
    direction: str = ""  # buy / sell
    strength: float = 0.0  # 0-100
    reason: str = ""
    price: float = 0.0
    indicators_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = dataclasses.asdict(self)
        return out


def _f(x: Any, default: float = 0.0) -> float:
    """取数值, NaN -> default."""
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


class SignalEngine:
    """综合评估五类信号."""

    # 入场线(加权总分达到才出首仓信号)
    ENTRY_LINE = 60.0

    def __init__(self) -> None:
        self._cfg: dict = {}

    def _reload_cfg(self) -> dict:
        self._cfg = config_manager.get()
        return self._cfg

    # ------------------------------------------------------------ 主入口
    def evaluate(
        self,
        symbol: str,
        name: str = "",
        kline_df: pd.DataFrame | None = None,
        position: PositionInfo | None = None,
        quote_price: float | None = None,
        quote_high: float | None = None,
        quote_low: float | None = None,
    ) -> Signal | None:
        """按优先级依次判定, 返回最强信号."""
        if kline_df is None or len(kline_df) < 30:
            return None
        cfg = self._reload_cfg()
        ind = self._indicators(kline_df, cfg)
        last = ind.iloc[-1]
        prev = ind.iloc[-2] if len(ind) > 1 else last
        price = quote_price or _f(last["close"])
        pos = position or PositionInfo(symbol=symbol)

        # 1. 止损(优先级最高)
        sig = self._check_stop(cfg, ind, last, pos, price, name)
        if sig:
            return sig
        # 2. 减仓
        sig = self._check_reduce(cfg, ind, last, prev, pos, price, name)
        if sig:
            return sig
        # 3. 加仓
        sig = self._check_add(cfg, ind, last, pos, price, name)
        if sig:
            return sig
        # 4. 首仓
        sig = self._check_buy_first(cfg, ind, last, prev, pos, price, name)
        if sig:
            return sig
        # 5. 做T(需持仓)
        sig = self._check_t_trade(cfg, ind, last, pos, price, quote_high, quote_low, name)
        if sig:
            return sig
        return None

    def _indicators(self, df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
        return compute_all(
            df,
            ma_short=cfg["趋势"]["ma_short"],
            ma_mid=cfg["趋势"]["ma_mid"],
            ma_long=cfg["趋势"]["ma_long"],
            macd_fast=cfg["动量"]["macd_fast"],
            macd_slow=cfg["动量"]["macd_slow"],
            macd_signal=cfg["动量"]["macd_signal"],
            rsi_period=cfg["动量"]["rsi_period"],
            roc_period=cfg["动量"]["roc_period"],
            volume_ma=cfg["量能"]["volume_ma"],
        )

    # ------------------------------------------------------------ 首仓信号
    def _check_buy_first(self, cfg, ind, last, prev, pos, price, name) -> Signal | None:
        """趋势 + 动量 + 量能三共振, 加权得分 >= 入场线."""
        trend = cfg["趋势"]
        momentum = cfg["动量"]
        volume = cfg["量能"]
        ma_s, ma_m, ma_l = f"ma{trend['ma_short']}", f"ma{trend['ma_mid']}", f"ma{trend['ma_long']}"

        adx = _f(last.get(f"adx{trend.get('adx_period', 14)}"))
        adx_threshold = trend["adx_threshold"]
        ma_s_v, ma_m_v, ma_l_v = _f(last.get(ma_s)), _f(last.get(ma_m)), _f(last.get(ma_l))
        bullish = ma_s_v > ma_m_v > ma_l_v

        # 趋势分 0-40: ADX 强度 0-20 + 多头排列 0-20
        trend_score = min(20.0, max(0.0, (adx - adx_threshold) / 25 * 20)) + (20.0 if bullish else 0.0)

        # 动量分 0-40: MACD 多头(15) + ROC 为正(15) + RSI 强势区(10)
        dif, dea = _f(last.get("dif")), _f(last.get("dea"))
        macd_bull = dif > dea
        hist_now = _f(last.get("macd_hist"))
        hist_prev = _f(prev.get("macd_hist"))
        golden_cross = hist_prev <= 0 < hist_now
        roc_now = _f(last.get(f"roc{momentum['roc_period']}"))
        roc_turn = _f(prev.get(f"roc{momentum['roc_period']}")) <= 0 < roc_now
        rsi_now = _f(last.get(f"rsi{momentum['rsi_period']}"), 50)
        momentum_score = (15.0 if macd_bull else 0.0) + (15.0 if roc_now > 0 else 0.0)
        if 50 <= rsi_now <= 70:
            momentum_score += 10.0
        momentum_score = min(40.0, momentum_score)

        # 量能分 0-20: 量比
        vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
        vr_threshold = volume["volume_ratio_threshold"]
        volume_score = min(20.0, max(0.0, (vr - vr_threshold) / 2 * 20))

        total = trend_score + momentum_score + volume_score
        if total < self.ENTRY_LINE:
            return None
        reasons = []
        if bullish:
            reasons.append(f"均线多头({ma_s_v:.2f}>{ma_m_v:.2f}>{ma_l_v:.2f})")
        if golden_cross:
            reasons.append("MACD金叉")
        if roc_turn:
            reasons.append("ROC由负转正")
        if vr > vr_threshold:
            reasons.append(f"放量{vr:.1f}倍")
        reason = "三共振入场: " + "、".join(reasons) if reasons else "加权得分达标"
        return Signal(
            type="BUY_FIRST", symbol=pos.symbol or "", name=name, direction="buy",
            strength=round(min(100.0, total), 1), reason=reason, price=price,
            indicators_snapshot=self._snapshot(last),
        )

    # ------------------------------------------------------------ 加仓信号
    def _check_add(self, cfg, ind, last, pos, price, name) -> Signal | None:
        """已持仓且浮盈 + 回踩 MA 中轨不破 + 动量未死叉."""
        if not pos.has_position or price <= pos.cost:
            return None
        trend = cfg["趋势"]
        ma_m = f"ma{trend['ma_mid']}"
        ma_m_v = _f(last.get(ma_m))
        close = _f(last["close"])
        dif, dea = _f(last.get("dif")), _f(last.get("dea"))
        # 回踩不破: 收盘仍在 MA 中轨上方(允许小幅跌破后收回, 简化用 close >= ma_m * 0.99)
        if close < ma_m_v * 0.99:
            return None
        # 动量未死叉
        if dif < dea:
            return None
        profit_pct = (price - pos.cost) / pos.cost * 100
        strength = min(90.0, 55 + profit_pct * 5)
        return Signal(
            type="BUY_ADD", symbol=pos.symbol, name=name, direction="buy",
            strength=round(strength, 1),
            reason=f"回踩{trend['ma_mid']}日线企稳(现价{price:.2f},浮盈{profit_pct:.1f}%),MACD未死叉",
            price=price, indicators_snapshot=self._snapshot(last),
        )

    # ------------------------------------------------------------ 减仓信号
    def _check_reduce(self, cfg, ind, last, prev, pos, price, name) -> Signal | None:
        """RSI 顶背离 / 触及分批止盈 / 量价背离."""
        if not pos.has_position or price <= pos.cost:
            return None
        momentum = cfg["动量"]
        position_cfg = cfg["仓位"]
        rsi_now = _f(last.get(f"rsi{momentum['rsi_period']}"))
        close_now = _f(last["close"])
        close_prev = _f(prev["close"])
        rsi_prev = _f(prev.get(f"rsi{momentum['rsi_period']}"))
        reasons = []

        # 1. RSI 顶背离: 价格新高而 RSI 不新高
        divergence = close_now > close_prev and rsi_now < rsi_prev and rsi_now > 60
        if divergence:
            reasons.append("RSI顶背离")

        # 2. 触及分批止盈档
        levels = position_cfg["take_profit_levels"]
        hit_level = None
        for level in levels:
            if price >= pos.cost * level:
                hit_level = level
        if hit_level:
            reasons.append(f"触及止盈档{hit_level:.0%}")

        # 3. 量价背离(价升量缩)
        vol_now = _f(last.get("volume"))
        vol_prev = _f(prev.get("volume"))
        if close_now > close_prev and vol_now < vol_prev * 0.8:
            reasons.append("量价背离(价升量缩)")

        if not reasons:
            return None
        strength = 65.0 if "顶背离" in reasons[0] else 60.0
        return Signal(
            type="SELL_REDUCE", symbol=pos.symbol, name=name, direction="sell",
            strength=strength, reason="减仓: " + "、".join(reasons),
            price=price, indicators_snapshot=self._snapshot(last),
        )

    # ------------------------------------------------------------ 止损信号
    def _check_stop(self, cfg, ind, last, pos, price, name) -> Signal | None:
        """跌破止损线 / 移动止损线; 或 MA 短穿中 + ADX 掉头."""
        if not pos.has_position:
            return None
        risk = cfg["风控"]
        trend = cfg["趋势"]
        stop_price = pos.cost * (1 - risk["stop_loss_pct"] / 100)
        ma_s = f"ma{trend['ma_short']}"
        ma_m = f"ma{trend['ma_mid']}"
        ma_s_v, ma_m_v = _f(last.get(ma_s)), _f(last.get(ma_m))
        adx_now = _f(last.get(f"adx{trend.get('adx_period', 14)}"))
        adx_prev = _f(ind.iloc[-2].get(f"adx{trend.get('adx_period', 14)}")) if len(ind) > 1 else adx_now
        reasons = []
        if price <= stop_price:
            reasons.append(f"跌破止损线{stop_price:.2f}")
        if ma_s_v < ma_m_v and adx_now < adx_prev:
            reasons.append("MA短穿中且ADX掉头")
        if not reasons:
            return None
        return Signal(
            type="SELL_STOP", symbol=pos.symbol, name=name, direction="sell",
            strength=90.0, reason="止损: " + "、".join(reasons),
            price=price, indicators_snapshot=self._snapshot(last),
        )

    # ------------------------------------------------------------ 做T信号
    def _check_t_trade(self, cfg, ind, last, pos, price, quote_high, quote_low, name) -> Signal | None:
        """日内波段: T_BUY 回踩布林下轨/日内支撑; T_SELL 冲布林上轨/日内阻力."""
        if not pos.has_position:
            return None
        t_cfg = cfg["做T"]
        if not t_cfg["enable"]:
            return None
        boll_upper = _f(last.get("boll_upper20"))
        boll_lower = _f(last.get("boll_lower20"))
        if boll_upper <= 0 or boll_lower <= 0:
            return None
        high = quote_high or _f(last["high"])
        low = quote_low or _f(last["low"])
        swing = (high - low) / _f(last["close"]) * 100 if _f(last["close"]) else 0
        if swing < t_cfg["min_swing_pct"]:
            return None
        # T_SELL: 冲布林上轨
        if price >= boll_upper * 0.995:
            return Signal(
                type="T_SELL", symbol=pos.symbol, name=name, direction="sell",
                strength=70.0,
                reason=f"日内冲布林上轨({boll_upper:.2f}),做T卖出(日内波动{swing:.1f}%)",
                price=price, indicators_snapshot=self._snapshot(last),
            )
        # T_BUY: 回踩布林下轨
        if price <= boll_lower * 1.005:
            return Signal(
                type="T_BUY", symbol=pos.symbol, name=name, direction="buy",
                strength=70.0,
                reason=f"日内回踩布林下轨({boll_lower:.2f}),做T买入(日内波动{swing:.1f}%)",
                price=price, indicators_snapshot=self._snapshot(last),
            )
        return None

    # ------------------------------------------------------------ 工具
    def _snapshot(self, last: pd.Series) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for col in ("ma10", "ma20", "ma60", "dif", "dea", "macd_hist", "rsi14", "adx14", "roc12",
                    "boll_upper20", "boll_lower20", "volume_ratio20", "atr14", "close"):
            if col in last.index:
                out[col] = round(float(_f(last[col])), 4)
        return out
