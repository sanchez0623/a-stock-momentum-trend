"""信号引擎核心实现.

设计要点:
- 输入: 行情 K线 + 可选持仓 + 可选实时行情 -> 输出 Signal | None
- 规则全部按方案 §4.3 落地, 纯函数式判定, 便于单测与回放
- 配置从 config_manager 实时读取(热更新生效)
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.config import config_manager
from app.core.indicators import compute_all
from app.core.modes import mode_for_ind

# 五类信号类型
signal_types = ("BUY_FIRST", "BUY_ADD", "SELL_REDUCE", "SELL_STOP", "T_BUY", "T_SELL")


@dataclass
class PositionInfo:
    """轻量持仓信息(信号引擎不依赖数据库模型)."""

    symbol: str = ""
    cost: float = 0.0
    qty: int = 0
    peak_price: float = 0.0   # 持仓期间最高价(移动止损用; 0=未记录, 退化为成本)

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
    mode: str = ""  # 触发时所处的交易模式(mode_key), 供计划/复盘解释选型

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
        self._cfg_ts: float = 0.0  # 上次配置快照时间(monotonic)

    def _reload_cfg(self) -> dict:
        """取配置快照(带 5s 节流).

        回测逐日评估会高频调用(每股票每天一次, 一次对比回测可达数万次),
        每次都 deepcopy 整份配置既慢又在多线程场景下放大竞态窗口;
        5s 节流后热更新仍在秒级生效, 回测吞吐显著提升.
        """
        now = time.monotonic()
        if not self._cfg or now - self._cfg_ts > 5.0:
            self._cfg = config_manager.get()
            self._cfg_ts = now
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
        return self.evaluate_with_ind(
            symbol=symbol, name=name, ind=ind, position=position,
            quote_price=quote_price, quote_high=quote_high, quote_low=quote_low,
        )

    def evaluate_with_ind(
        self,
        symbol: str,
        name: str = "",
        ind: pd.DataFrame | None = None,
        position: PositionInfo | None = None,
        quote_price: float | None = None,
        quote_high: float | None = None,
        quote_low: float | None = None,
        end: int | None = None,
        skip_t: bool = False,
    ) -> Signal | None:
        """与 evaluate 同逻辑, 但接收已算好的指标表(回测逐日复用, 避免重复 compute_all).

        end: 可选判定位置(回测逐日传 i, 默认末行). 行为与 evaluate 完全一致.
        skip_t: True 时跳过做T 分支(回测主信号用). 做T 需当日盘中高低价,
        回测中由调用方以 _check_t_trade(want=...) 单独按 T 日高低价判定,
        避免 T_SELL 抢占加仓/首仓, 也不把盘中价传入主信号造成前视.
        """
        if ind is None or len(ind) < 30:
            return None
        n = len(ind)
        if end is None:
            end = n
        if end < 30:
            return None
        cfg = self._reload_cfg()
        mode_decision = mode_for_ind(ind, cfg, end=end)  # Q2: 规则化市况分类, 选出当前交易模式
        last = ind.iloc[end - 1]
        prev = ind.iloc[end - 2] if end > 1 else last
        price = quote_price or _f(last["close"])
        pos = position or PositionInfo(symbol=symbol)

        def _tag(s: Signal | None) -> Signal | None:
            """把当前模式信息写回信号(选型始终走显式规则, 不依赖 LLM)."""
            if s is None:
                return None
            s.mode = mode_decision.mode_key
            snap = dict(s.indicators_snapshot or {})
            snap["mode"] = mode_decision.mode_key
            s.indicators_snapshot = snap
            return s

        # 1. 止损(保命, 优先级最高)
        sig = self._check_stop(cfg, ind, last, prev, pos, price, name, mode_decision)
        if sig:
            return _tag(sig)
        # 2. 减仓(纪律)
        sig = self._check_reduce(cfg, ind, last, prev, pos, price, name)
        if sig:
            return _tag(sig)
        # 3. 做T卖出(顺势超买高抛): 风控性卖出优先于进攻性加仓/首仓(用户重排)
        if not skip_t:
            sig = self._check_t_trade(cfg, ind, last, pos, price, quote_high, quote_low, name,
                                      want="sell", mode_key=mode_decision.mode_key)
            if sig:
                return _tag(sig)
        # 4. 加仓(回踩顺向)
        sig = self._check_add(cfg, ind, last, prev, pos, price, name, mode_decision)
        if sig:
            return _tag(sig)
        # 5. 首仓
        sig = self._check_buy_first(cfg, ind, last, prev, pos, price, name)
        if sig:
            return _tag(sig)
        # 6. 做T买入(低吸, 最低: 绝不抢跑加仓/首仓, 避免亏损中盲目低吸)
        if not skip_t:
            sig = self._check_t_trade(cfg, ind, last, pos, price, quote_high, quote_low, name,
                                      want="buy", mode_key=mode_decision.mode_key)
            if sig:
                return _tag(sig)
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
        """趋势 + 动量 + 量能三共振, 加权得分 >= 入场线.

        仅空仓可触发: 已持仓时加仓/减仓/止损/做T 不满足即保持观望(None),
        绝不再报首仓(否则会把持有中的票误报成新开仓建议).
        """
        if pos.has_position:
            return None
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
    def _check_add(self, cfg, ind, last, prev, pos, price, name, mode_decision) -> Signal | None:
        """已持仓 + 浮盈达模式门槛 + 回踩 MA 中轨不破 + 动量未死叉; 多因子动态打分(Q1)."""
        if not pos.has_position or price <= pos.cost:
            return None
        mode = mode_decision.mode
        # 模式禁止加仓(如防守模式)直接跳过
        if not mode.get("allow_add", True):
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
        # 模式最低浮盈门槛(趋势强攻 2% / 回踩 3% / 震荡 4%)
        min_pct = float(mode.get("min_add_profit_pct", 0.0))
        if profit_pct < min_pct:
            return None
        # 多因子动态打分(回踩深度/ATR、缩量、RSI 均值回归、ADX 信心乘子、浮盈小权重)
        score, subs = self._score_add(cfg, ind, last, prev, pos, price, mode_decision)
        reason = "加仓: " + "、".join(subs) + f"(浮盈{profit_pct:.1f}%, 模式={mode_decision.label})"
        return Signal(
            type="BUY_ADD", symbol=pos.symbol, name=name, direction="buy",
            strength=round(score, 1), reason=reason, price=price,
            indicators_snapshot=self._snapshot(last),
        )

    def _score_add(self, cfg, ind, last, prev, pos, price, mode_decision) -> tuple[float, list[str]]:
        """BUY_ADD 多因子动态打分(0-100, 可解释).

        因子与权重:
        - 回踩 MA 中轨企稳(基础 30)
        - 回踩缩量(15)            —— 量能扩张反向, 健康回踩不放量
        - 首次触及短均线反弹(15)  / 沿短均线强势(10)
        - RSI 自超买回落(15)      / RSI 健康区(8)
        - 良性回踩深度(距高在 0.5~3 ATR, 10)
        - ADX 信心乘子(0.7~1.3)   —— 趋势越强越可信
        - 浮盈仅小权重(封顶 10)   —— 不再主导分数
        """
        trend = cfg["趋势"]
        momentum = cfg["动量"]
        ma_s = f"ma{trend['ma_short']}"
        ma_m = f"ma{trend['ma_mid']}"
        ma_s_v, ma_m_v = _f(last.get(ma_s)), _f(last.get(ma_m))
        ma_s_prev = _f(prev.get(ma_s))
        close = _f(last["close"])
        prev_close = _f(prev["close"])
        vol_now = _f(last.get("volume"))
        vol_prev = _f(prev.get("volume"))
        rsi_now = _f(last.get(f"rsi{momentum['rsi_period']}"), 50)
        rsi_prev = _f(prev.get(f"rsi{momentum['rsi_period']}"), 50)
        rsi_ob = momentum["rsi_overbought"]
        atr_pct = self.atr_pct(last)
        regime = mode_decision.regime
        dist_to_high = float(regime.get("dist_to_high_pct", 0.0))
        profit_pct = (price - pos.cost) / pos.cost * 100

        subs: list[str] = []
        score = 0.0
        # 回踩企稳(价格仍在 MA 中轨上方) — 基础 30
        if close >= ma_m_v * 0.99:
            subs.append(f"回踩{trend['ma_mid']}日线企稳")
            score += 30
        # 缩量回踩 — 15
        if vol_now > 0 and vol_prev > 0 and vol_now <= vol_prev * 0.9:
            subs.append("回踩缩量")
            score += 15
        # 首次触及短均线反弹 — 15 / 沿短均线强势不回头 — 10
        if close >= ma_s_v and prev_close < ma_s_v:
            subs.append(f"首次触及{trend['ma_short']}日线反弹")
            score += 15
        elif close >= ma_s_v and ma_s_v >= ma_s_prev:
            subs.append(f"沿{trend['ma_short']}日线强势")
            score += 10
        # RSI 自超买回落后再拐头 — 15 / RSI 健康区 — 8
        if rsi_prev >= rsi_ob and rsi_now < rsi_prev:
            subs.append("RSI自超买回落")
            score += 15
        elif 40 <= rsi_now <= 65:
            subs.append("RSI健康区")
            score += 8
        # 良性回踩深度(相对 ATR) — 10
        if atr_pct > 0:
            pb_atr = dist_to_high / (atr_pct * 100)
            if 0.5 <= pb_atr <= 3.0:
                subs.append(f"良性回踩(距高{pb_atr:.1f}ATR)")
                score += 10
        # ADX 信心乘子
        adx = float(regime.get("adx", 0.0))
        adx_mult = min(1.3, max(0.7, adx / 30.0))
        score = score * adx_mult
        # 浮盈仅小权重(封顶 10)
        score += min(10.0, max(0.0, profit_pct) * 1.0)
        score = round(min(95.0, max(0.0, score)), 1)
        return score, subs

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

        # 2. 触及分批止盈档(ATR 动态档或 fixed 档)
        hit_level = self._hit_take_profit(price, pos.cost, last, position_cfg)
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
    def _check_stop(self, cfg, ind, last, prev, pos, price, name, mode_decision) -> Signal | None:
        """止损判定(结构性优先): 趋势破坏 > 移动止损(盈利保护) > 静态止损线.

        结构性: MA短穿中 + ADX掉头 -> 趋势已坏, 无论盈亏优先离场(常在浮盈中全身而退).
        移动止损线: 持仓峰值 × (1 - trailing_stop_pct), 高于静态线时接管——
        浮盈保护(让利润奔跑), 与结构无关, 触线即走.
        静态止损线: 亏损触线无条件止损. (回测教训 2026-08-22: 曾试行"结构未坏时放宽到
        硬线8%"让趋势呼吸, 单笔亏损放大60%、总收益-26.76% vs 无条件线+17.54%, 已回退;
        结构确认滞后, 过滤不掉假止损, 只会把该止的损拖到更深.)
        静态线取当前模式的 stop_loss_pct(模式自带风控), 回退全局 风控 配置.
        peak 由调用方维护(回测: 逐日收盘峰值; 实盘: 成交价与实时价的较大值).
        """
        if not pos.has_position:
            return None
        risk = cfg["风控"]
        trend = cfg["趋势"]
        stop_pct = float(mode_decision.mode.get("stop_loss_pct", risk["stop_loss_pct"]))
        static_stop = pos.cost * (1 - stop_pct / 100)
        # 移动止损: 峰值(历史最高价与当前价的较大值, 无前视) × (1 - trailing)
        trailing_pct = float(mode_decision.mode.get("trailing_stop_pct",
                                                    risk.get("trailing_stop_pct", stop_pct)))
        peak = max(pos.peak_price, price) if pos.peak_price > 0 else max(pos.cost, price)
        trail_stop = peak * (1 - trailing_pct / 100)
        ma_s = f"ma{trend['ma_short']}"
        ma_m = f"ma{trend['ma_mid']}"
        ma_s_v, ma_m_v = _f(last.get(ma_s)), _f(last.get(ma_m))
        adx_key = f"adx{trend.get('adx_period', 14)}"
        adx_now = _f(last.get(adx_key))
        adx_prev = _f(prev.get(adx_key)) if prev is not None else adx_now
        reasons = []
        # 结构性(趋势破坏): 最高优先, 浮盈浮亏都离场
        structure_broken = ma_s_v < ma_m_v and adx_now < adx_prev
        if structure_broken:
            reasons.append("MA短穿中且ADX掉头")
        if trail_stop > static_stop:
            # 浮盈保护: 移动止损线接管, 与结构无关, 触线即走
            if price <= trail_stop:
                reasons.append(f"跌破移动止损线{trail_stop:.2f}(峰值{peak:.2f})")
        elif price <= static_stop:
            # 亏损区: 静态线无条件止损(快砍, 不等结构确认)
            reasons.append(f"跌破止损线{static_stop:.2f}")
        if not reasons:
            return None
        return Signal(
            type="SELL_STOP", symbol=pos.symbol, name=name, direction="sell",
            strength=90.0, reason="止损: " + "、".join(reasons),
            price=price, indicators_snapshot=self._snapshot(last),
        )

    # ------------------------------------------------------------ 做T信号
    # 市况系数: 趋势强(易触发) < 震荡(防噪) < 防守(少做T)
    SWING_REGIME_MULT: dict[str, float] = {
        "trend_strong": 0.6,
        "trend_pullback": 0.8,
        "range": 1.2,
        "defense": 1.5,
        "unknown": 1.0,
    }

    def _swing_threshold(self, cfg: dict, last: pd.Series, mode_key: str,
                         symbol: str = "") -> float:
        """做T波幅阈值(%).

        P0 动态阈值: max(ATR% × swing_mult × 市况系数, min_swing_floor)
        - swing_mode="fixed" 时退回旧配置 min_swing_pct(兼容)
        - P1: 若 llm_swing_enabled 且当日有 LLM 建议值, 则优先用建议值 × llm_swing_mult
        """
        t_cfg = cfg["做T"]
        mode = t_cfg.get("swing_mode", "fixed")
        if mode != "dynamic":
            return float(t_cfg.get("min_swing_pct", 1.5))

        # P1: 盘前 LLM 建议值优先(按 symbol 查当日建议; 详见 assistant/t_swing.py)
        if t_cfg.get("llm_swing_enabled", False) and symbol:
            llm_val = self._get_llm_swing(symbol)
            if llm_val and llm_val > 0:
                return round(llm_val * float(t_cfg.get("llm_swing_mult", 1.0)), 2)

        atr = self.atr_pct(last) * 100  # ATR% (如 2.5)
        mult = float(t_cfg.get("swing_mult", 1.0))
        regime_mult = self.SWING_REGIME_MULT.get(mode_key, 1.0)
        floor = float(t_cfg.get("min_swing_floor", 1.0))
        return round(max(atr * mult * regime_mult, floor), 2)

    @staticmethod
    def _get_llm_swing(symbol: str) -> float | None:
        """读取当日 LLM 做T波幅建议(延迟导入避免循环依赖; 失败返回 None 走规则)."""
        if not symbol:
            return None
        try:
            from app.core.assistant.t_swing import get_today_swing
            return get_today_swing(symbol)
        except Exception:
            return None

    def _check_t_trade(self, cfg, ind, last, pos, price, quote_high, quote_low, name,
                       want: str = "both", mode_key: str = "") -> Signal | None:
        """日内波段: T_SELL 冲布林上轨(高抛) / T_BUY 回踩布林下轨(低吸).

        want: "sell" 只判高抛 / "buy" 只判低吸 / "both" 两者(兼容旧调用).
        优先级重排(用户): T_SELL 在加仓/首仓之前(风控性卖出), T_BUY 保持最低(逆势低吸保守).
        波幅阈值: _swing_threshold() 动态计算(ATR×市况), 替代固定 min_swing_pct.
        """
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
        threshold = self._swing_threshold(cfg, last, mode_key or "unknown", symbol=pos.symbol)
        if swing < threshold:
            return None
        # T_SELL: 冲布林上轨
        if want in ("sell", "both") and price >= boll_upper * 0.995:
            return Signal(
                type="T_SELL", symbol=pos.symbol, name=name, direction="sell",
                strength=70.0,
                reason=f"日内冲布林上轨({boll_upper:.2f}),做T卖出(日内波动{swing:.1f}%≥阈值{threshold:.1f}%)",
                price=price, indicators_snapshot=self._snapshot(last),
            )
        # T_BUY: 回踩布林下轨
        if want in ("buy", "both") and price <= boll_lower * 1.005:
            return Signal(
                type="T_BUY", symbol=pos.symbol, name=name, direction="buy",
                strength=70.0,
                reason=f"日内回踩布林下轨({boll_lower:.2f}),做T买入(日内波动{swing:.1f}%≥阈值{threshold:.1f}%)",
                price=price, indicators_snapshot=self._snapshot(last),
            )
        return None

    # ------------------------------------------------------------ 工具
    @staticmethod
    def atr_pct(last: pd.Series, fallback: float = 0.03) -> float:
        """ATR 占收盘价比例(波动率). 数据缺失时用默认 3%."""
        atr14 = _f(last.get("atr14"))
        close = _f(last.get("close"))
        if close <= 0 or atr14 <= 0:
            return fallback
        return atr14 / close

    def take_profit_targets(self, cost: float, last: pd.Series, position_cfg: dict | None = None) -> list[float]:
        """止盈目标价列表: atr 动态档(带下限保护) 或 fixed 档."""
        pc = position_cfg or config_manager.get()["仓位"]
        if pc.get("take_profit_mode", "atr") == "fixed":
            return [cost * lv for lv in pc["take_profit_levels"]]
        ap = self.atr_pct(last)
        min_pct = pc.get("min_tp_pct", 3.0) / 100.0
        return [cost * (1 + max(m * ap, min_pct)) for m in pc.get("atr_multipliers", [1.5, 3.0, 5.0])]

    def _hit_take_profit(self, price: float, cost: float, last: pd.Series, position_cfg: dict | None = None) -> float | None:
        """返回命中的最高止盈档(倍数), 未命中返回 None."""
        hit = None
        for target in self.take_profit_targets(cost, last, position_cfg):
            if price >= target:
                hit = target / cost
        return hit

    def _snapshot(self, last: pd.Series) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for col in ("ma10", "ma20", "ma60", "dif", "dea", "macd_hist", "rsi14", "adx14", "roc12",
                    "boll_upper20", "boll_lower20", "volume_ratio20", "atr14", "close"):
            if col in last.index:
                out[col] = round(float(_f(last[col])), 4)
        return out
