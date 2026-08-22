"""全流程策略回测(方案C V2: 真实交易循环).

在阶段分桶因子回测(MVP)基础上, 把 建仓/加仓/减仓/止损/做T 五类信号接入事件驱动循环,
模拟真实交易: 信号日 T 收盘判定 -> T+1 开盘成交(做T 用当日高低价近似) -> 每日净值结算。

时序与撮合规则(与 docs/回测中心-回测功能设计方案.md §4 一致):
- 信号判定: 用 T 日收盘指标(无前视), 复用 SignalEngine.evaluate_with_ind(逐日判定, 指标只算一次)
- 成交: T+1 开盘价(建仓/加仓/减仓/止损); 做T: T_BUY 在 T+1 最低价、T_SELL 在 T+1 最高价(乐观口径, 用户确认)
- 涨跌停: 开盘触及涨停不买、触及跌停不卖(按板块 10%/20%/30%)
- T+1 制度: 当日买入不可卖(信号到执行天然隔日, 满足)
- 费用: 复用 compute_trade_fee(佣金万0.5最低5元+印花税万5+三费)

组合与风控(独立状态, 不污染真实 RiskState):
- 单票仓位上限 single_position_pct / 总仓位上限 total_position_pct
- 加仓按 pyramid_ratios 档位推进; 减仓(SELL_REDUCE)减 1/3
- 做T 按 t_position_ratio 比例, 波幅不足跳过
- 三道闸门: 日亏熔断(禁止新开仓)/ 连亏降仓(单票仓位减半)/ 回撤防守(只减不加)
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from app.core.backtest.path_sim import DEFAULT_MINUTES, build_intraday_path
from app.core.config import config_manager
from app.core.datasource import kline_store
from app.core.fees import compute_trade_fee
from app.core.indicators import compute_all
from app.core.lot_rules import (
    round_buy_qty as _round_buy_qty,
)
from app.core.lot_rules import (
    sell_qty as _sell_qty,
)
from app.core.modes import mode_for_ind
from app.core.signals.engine import PositionInfo, SignalEngine

logger = logging.getLogger(__name__)

MIN_BARS = 60
SLIPPAGE = 0.001          # 滑点(买卖各 0.1%, 做T 除外: 已按高低价近似)
REDUCE_RATIO = 1 / 3      # SELL_REDUCE 减仓比例

# 板块涨跌停限制(按代码前缀)
_LIMIT_PREFIX = {
    ("300", "301", "302"): 0.20,
    ("688", "689"): 0.20,
    ("43", "83", "87", "88", "92"): 0.30,
}
DEFAULT_LIMIT = 0.10


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def _limit_pct(symbol: str) -> float:
    for prefixes, pct in _LIMIT_PREFIX.items():
        if symbol.startswith(prefixes):
            return pct
    return DEFAULT_LIMIT


# ---------------------------------------------------------------- 申报数量规则(交易所合规)
# 科创板(688/689): 买入 ≥200 股, 1 股递增; 北交所(43/83/87/88/92): 买入 ≥100 股, 1 股递增;
# 主板/创业板: 买入 ≥100 股, 100 股整数倍; 卖出后剩余不足最小单位(碎股)须一次性全部卖出。
# 规则统一收敛在 app.core.lot_rules(回测/计划/录入共用), 此处仅做别名导入保持内部调用不变.


@dataclass
class Position:
    """回测持仓(与 SignalEngine.PositionInfo 互转)."""

    symbol: str
    name: str = ""
    qty: int = 0
    cost: float = 0.0          # 加权平均成本(不含费)
    stage: int = 0             # 已用加仓档位(0=首仓已用)
    peak_price: float = 0.0    # 持仓期间最高收盘价(移动止损用)

    @property
    def has_position(self) -> bool:
        return self.qty > 0


@dataclass
class TradeRecord:
    date: str
    symbol: str
    name: str
    action: str          # buy_first/buy_add/sell_reduce/sell_stop/t_buy/t_sell
    price: float
    qty: int
    fee: float
    pnl: float = 0.0     # 卖出类: 该笔实现盈亏(含费); 买入类: 0
    reason: str = ""


class StrategyBacktest:
    """事件驱动策略回测(单进程, 自选+持仓池规模, 分钟级)."""

    def __init__(self, initial_capital: float = 1_000_000.0,
                 intraday_minutes: int = DEFAULT_MINUTES,
                 defense: str = "soft") -> None:
        self.engine = SignalEngine()
        self.cfg = config_manager.get()
        self.fee_cfg = self.cfg.get("手续费", {})
        self.initial = float(initial_capital)
        self.cash = self.initial
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict[str, Any]] = []
        # 盘中路径模拟粒度(分钟): 全部信号在构造的日内路径上逐段触发(用户确认口径)
        self.intraday_minutes = intraday_minutes if intraday_minutes in (5, 10, 15, 30) else DEFAULT_MINUTES
        # 风控状态(独立)
        self.consecutive_losses = 0
        self.peak_equity = self.initial
        self.fuse_date: str | None = None    # 日亏熔断生效日
        # 回撤防守模式: soft=软防守(仓位减半, 修复自动解除, 对齐实盘) /
        # hard=硬防守(触发后禁开仓只减不加, 不解除, 旧实盘口径) / off=关闭
        self.defense_kind = defense if defense in ("soft", "hard", "off") else "soft"
        self.defense_mode = False            # 回撤防守是否触发
        # 止损冷却(防同票连环接刀): symbol -> 最近一次止损的交易日序号
        risk = self.cfg.get("风控", {})
        self.stop_cooldown_days = int(risk.get("stop_cooldown_days", 10))
        self._drawdown_limit = float(risk.get("max_drawdown_pct", 10.0))
        self._defense_recovery_ratio = float(risk.get("defense_recovery_ratio", 0.5))
        self._last_stop_day: dict[str, int] = {}
        self.cooldown_blocks = 0             # 冷却期拦截的再入场次数(symbol-日口径)

    # ------------------------------------------------------------ 辅助
    def _day_path(self, ind: pd.DataFrame, i: int) -> list[dict[str, float]]:
        """当日盘中路径(确定性 OHLC 插值, 段数 = 240 / 粒度)."""
        return build_intraday_path(
            _f(ind["open"].iloc[i]), _f(ind["high"].iloc[i]),
            _f(ind["low"].iloc[i]), _f(ind["close"].iloc[i]),
            minutes=self.intraday_minutes,
        )

    def _pos_info(self, p: Position) -> PositionInfo:
        return PositionInfo(symbol=p.symbol, cost=p.cost, qty=p.qty, peak_price=p.peak_price)

    def _cooldown_active(self, symbol: str, day_idx: int) -> bool:
        """止损冷却: 同票止损后 N 个交易日内不再触发 BUY_FIRST(防连跌中反复接刀)."""
        if self.stop_cooldown_days <= 0:
            return False
        last = self._last_stop_day.get(symbol)
        return last is not None and (day_idx - last) < self.stop_cooldown_days

    def _update_defense(self, eq: float) -> None:
        """回撤防守状态(按 defense_kind):
        - soft: 达阈值开启 -> 仓位减半仍可开仓; 修复至阈值 × defense_recovery_ratio 以下解除
                (对齐实盘 risk/manager, 回测用滞回近似)
        - hard: 达阈值开启 -> 禁开仓只减不加, 永不自动解除(旧实盘口径)
        - off:  关闭, 永不触发
        """
        if self.defense_kind == "off":
            return
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity <= 0:
            return
        dd = (self.peak_equity - eq) / self.peak_equity * 100
        if dd >= self._drawdown_limit:
            self.defense_mode = True
        elif (self.defense_kind == "soft" and self.defense_mode
              and dd < self._drawdown_limit * self._defense_recovery_ratio):
            self.defense_mode = False

    def _buy(self, date: str, symbol: str, name: str, price: float, qty: int,
             action: str, reason: str) -> None:
        if qty <= 0:
            return
        amount = price * qty
        fee = compute_trade_fee("buy", amount, self.fee_cfg)
        if self.cash < amount + fee:
            return
        self.cash -= amount + fee
        p = self.positions.get(symbol)
        if p is None:
            p = Position(symbol=symbol, name=name, peak_price=price)
            self.positions[symbol] = p
        # 加权平均成本(不含费, 与真实系统口径一致: 费用单独计)
        total_cost = p.cost * p.qty + price * qty
        p.qty += qty
        p.cost = total_cost / p.qty if p.qty else 0.0
        p.peak_price = max(p.peak_price, price)
        self.trades.append(TradeRecord(date, symbol, name, action, price, qty, fee, reason=reason))

    def _sell(self, date: str, symbol: str, name: str, price: float, qty: int,
              action: str, reason: str) -> None:
        p = self.positions.get(symbol)
        if p is None or qty <= 0:
            return
        qty = min(qty, p.qty)
        amount = price * qty
        fee = compute_trade_fee("sell", amount, self.fee_cfg)
        self.cash += amount - fee
        pnl = (price - p.cost) * qty - fee
        p.qty -= qty
        if p.qty == 0:
            self.positions.pop(symbol, None)
        self.trades.append(TradeRecord(date, symbol, name, action, price, qty, fee, pnl=pnl, reason=reason))
        # 连亏/连胜统计(仅止损与止盈减仓计入)
        if action in ("sell_stop", "sell_reduce"):
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

    # ------------------------------------------------------------ 主循环
    def run(self, symbols: list[str] | None = None,
            progress_cb: Callable[[int, int], None] | None = None,
            start: str = "", end: str = "") -> dict[str, Any]:
        """运行策略回测. symbols: 股票池(为空=自选+持仓). 返回报告.

        start/end: 回测区间(YYYY-MM-DD, 含端点; 空=全部本地数据).
        指标始终用全量数据计算(预热均线/ADX), 只裁剪交易循环区间——
        窗口起点前的历史自动成为指标预热段, 窗口首日即可正常出信号.
        """
        if symbols is None:
            symbols = self._default_pool()
        cfg = self.cfg
        risk = cfg["风控"]
        position_cfg = cfg["仓位"]
        t_cfg = cfg["做T"]
        plan_ratio = float(risk["single_position_pct"]) / 100.0
        max_total = float(risk["total_position_pct"]) / 100.0
        daily_limit = float(risk["daily_loss_limit_pct"])
        loss_limit = int(risk["consecutive_loss_limit"])
        pyramid = list(position_cfg.get("pyramid_ratios", [0.5, 0.3, 0.2]))

        # ---- 逐股预计算指标(一次性, 后续逐日判定零成本) + date->idx 映射
        pool: dict[str, tuple[pd.DataFrame, str, dict[str, int]]] = {}
        skipped: list[str] = []
        for sym in dict.fromkeys(symbols):
            rows = kline_store.load(sym, "daily")
            if not rows:
                skipped.append(sym)
                continue
            df = pd.DataFrame(rows)
            need = {"date", "open", "high", "low", "close", "volume", "amount"}
            if not need.issubset(df.columns) or len(df) < MIN_BARS + 2:
                skipped.append(sym)
                continue
            for c in ("open", "high", "low", "close", "volume", "amount"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if len(df) < MIN_BARS + 2:
                skipped.append(sym)
                continue
            ind = compute_all(
                df,
                ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
                macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
                rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
                volume_ma=cfg["量能"]["volume_ma"],
            )
            di = {str(d)[:10]: i for i, d in enumerate(ind["date"])}  # 归一日期(缓存存在 YYYY-MM-DD 与带 15:00 两种格式)
            pool[sym] = (ind, "", di)
        if not pool:
            return {"error": f"股票池全部无可用日线数据({len(symbols)} 只均跳过)"}

        # ---- 时间轴: 全部股票交易日并集(停牌/错位自动跳过), 逐日推进
        all_dates = sorted(set().union(*(di.keys() for _, _, di in pool.values())))
        # 时间范围裁剪(含端点; 指标已全量预热, 只裁交易循环)
        if start:
            all_dates = [d for d in all_dates if d >= start[:10]]
        if end:
            all_dates = [d for d in all_dates if d <= end[:10]]
        if not all_dates:
            return {"error": f"时间范围内无可用交易日({start or '不限'} ~ {end or '不限'})"}
        # 做T 未买回计数(防底仓漂移): symbol -> T_SELL 卖出未买回数量
        t_outstanding: dict[str, int] = {}
        n_days = len(all_dates)

        # 进度上报节流: 每 10 个交易日或每 2 秒一次(先到为准).
        # 旧版每 50 天一报, 慢变体(如裸奔无冷却交易爆炸)两个进度点间隔可达 20s+,
        # 服务器上叠加轮询抢 GIL 后更像"卡死"; 时间节流保证静默期 <= 2s.
        _last_cb = {"t": 0.0, "day": -10}

        def _report(day_idx: int) -> None:
            if progress_cb is None:
                return
            now = time.monotonic()
            if day_idx - _last_cb["day"] >= 10 or now - _last_cb["t"] >= 2.0 or day_idx == n_days - 1:
                _last_cb["day"], _last_cb["t"] = day_idx, now
                progress_cb(day_idx + 1, n_days)

        for day_idx, date in enumerate(all_dates):
            _report(day_idx)

            # ---- 风控闸门(基于昨日状态; 熔断/防守当日立即生效, 次日延续)
            gate_open = self.fuse_date is None or date > self.fuse_date
            # 回撤防守: soft=仓位上限砍半仍可开仓(对齐实盘) / hard=禁开仓只减不加 / off=无
            if self.defense_kind == "hard" and self.defense_mode:
                gate_open = False
            defense_ratio = 0.5 if (self.defense_kind == "soft" and self.defense_mode) else 1.0
            gate_add = gate_open
            reduced_ratio = 0.5 if self.consecutive_losses >= loss_limit else 1.0

            # ---- 逐票盘中路径推进: 全部信号盘中触发(与持仓回测同口径, 用户确认)
            # 每段: T-1 收盘指标 + 段内价格(open/high/low/close)调信号引擎, 命中即按段价成交
            for sym, (ind, name, di) in pool.items():
                i = di.get(date)
                if i is None or i < MIN_BARS + 1:
                    continue
                ti = i - 1  # 信号日(指标只算到 T-1 收盘, 无前视)
                if ti < MIN_BARS:
                    continue
                prev_close = _f(ind["close"].iloc[ti])
                limit = _limit_pct(sym)
                up_limit = prev_close * (1 + limit) if prev_close > 0 else 1e18
                down_limit = prev_close * (1 - limit) if prev_close > 0 else 0.0
                # 跳变/均线偏离防护(除权/脏数据日不参与)
                open_px = _f(ind["open"].iloc[i])
                high_px = _f(ind["high"].iloc[i])
                low_px = _f(ind["low"].iloc[i])
                if prev_close > 0 and any(
                    px > 0 and abs(px / prev_close - 1) > limit * 1.6
                    for px in (open_px, high_px, low_px)
                ):
                    continue
                ma20 = _f(ind["ma20"].iloc[i]) if "ma20" in ind.columns else 0.0
                if ma20 > 0 and any(
                    px > 0 and abs(px / ma20 - 1) > 0.40 for px in (open_px, high_px, low_px)
                ):
                    continue
                bought_today_qty = 0  # T+1: 当日买入数量(当日买入部分不可卖, 老仓可卖)
                day_acted: set[str] = set()  # 日线信号型动作每日最多一次(减仓/加仓); 做T 不限制
                # 模式决策每天一次(T-1 收盘, 无前视), 供盘中止损预检
                mode_dec = mode_for_ind(ind, self.cfg, end=ti)
                last_row = ind.iloc[ti]
                prev_row = ind.iloc[max(ti - 1, 0)]
                for seg in self._day_path(ind, i):
                    seg_c = seg["close"]
                    seg_h = seg["high"]
                    seg_l = seg["low"]
                    pos = self.positions.get(sym)
                    if pos is None:
                        # ---- 空仓: BUY_FIRST 盘中触发 -> 自动开新仓
                        if not gate_open or seg_c >= up_limit * 0.998:
                            continue
                        # 止损冷却期内禁止同票再入场(防连跌中反复接刀)
                        if self._cooldown_active(sym, day_idx):
                            if "COOLDOWN" not in day_acted:
                                day_acted.add("COOLDOWN")
                                self.cooldown_blocks += 1
                            continue
                        sig = self.engine.evaluate_with_ind(
                            symbol=sym, name=name, ind=ind, position=PositionInfo(symbol=sym),
                            quote_price=seg_c, quote_high=seg_h, quote_low=seg_l,
                            end=ti, skip_t=False,
                        )
                        if sig is None or sig.type != "BUY_FIRST":
                            continue
                        equity_now = self.cash + sum(
                            p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][2].get(date, 0), len(pool[s2][0]) - 1)])
                            for s2, p2 in self.positions.items()
                        )
                        used_pct = sum(
                            p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][2].get(date, 0), len(pool[s2][0]) - 1)])
                            for s2, p2 in self.positions.items()
                        ) / equity_now if equity_now else 1.0
                        if used_pct >= max_total:
                            continue
                        plan_amount = min(equity_now * plan_ratio * reduced_ratio * defense_ratio,
                                          self.cash * 0.99)
                        qty = _round_buy_qty(int(plan_amount / (seg_c * (1 + SLIPPAGE))), sym)
                        if qty <= 0:
                            continue
                        self._buy(date, sym, name, seg_c * (1 + SLIPPAGE), qty, "buy_first", sig.reason)
                        if sym in self.positions:
                            self.positions[sym].stage = 1
                        bought_today_qty += qty  # T+1: 当日买入部分锁定
                        continue
                    sellable = max(0, pos.qty - bought_today_qty)  # 老仓部分(当日买入 T+1 锁定)
                    # ---- 盘中止损预检: 段最低价触及止损线即离场(真实盘中语义)
                    stop_sig = self.engine._check_stop(
                        self.cfg, ind, last_row, prev_row, self._pos_info(pos),
                        price=seg_l, name=name, mode_decision=mode_dec,
                    )
                    if stop_sig is not None:
                        if sellable > 0 and seg_l > down_limit * 1.002:
                            self._sell(date, sym, name, seg_l * (1 - SLIPPAGE), sellable,
                                       "sell_stop", stop_sig.reason)
                            self._last_stop_day[sym] = day_idx  # 启动冷却计时
                        continue  # 盘中触及止损: 当日该票结束
                    # ---- 其余信号盘中判定(引擎按优先级返回最强信号)
                    sig = self.engine.evaluate_with_ind(
                        symbol=sym, name=name, ind=ind, position=self._pos_info(pos),
                        quote_price=seg_c, quote_high=seg_h, quote_low=seg_l,
                        end=ti, skip_t=False,
                    )
                    stype = sig.type if sig else ""
                    if stype == "SELL_STOP":
                        if sellable > 0 and seg_c > down_limit * 1.002:
                            self._sell(date, sym, name, seg_c * (1 - SLIPPAGE), sellable, "sell_stop", sig.reason)
                            self._last_stop_day[sym] = day_idx  # 启动冷却计时
                        continue
                    if stype == "SELL_REDUCE":
                        if "SELL_REDUCE" not in day_acted and sellable > 0 and seg_c > down_limit * 1.002:
                            qty = _sell_qty(max(1, int(sellable * REDUCE_RATIO)), sellable, sym)
                            if qty > 0:
                                self._sell(date, sym, name, seg_c * (1 - SLIPPAGE), qty, "sell_reduce", sig.reason)
                                day_acted.add("SELL_REDUCE")
                        continue
                    if stype == "T_SELL":
                        if sellable > 0 and seg_h > 0 and seg_c > down_limit * 1.002:
                            t_qty = max(1, int(sellable * float(t_cfg.get("t_position_ratio", 0.3))))
                            t_qty = min(t_qty, sellable)  # 不卖当日新买入部分
                            t_qty = _sell_qty(t_qty, sellable, sym)  # 申报取整(防科创板碎股)
                            if t_qty > 0:
                                self._sell(date, sym, name, seg_h * (1 - SLIPPAGE), t_qty, "t_sell", sig.reason)
                                t_outstanding[sym] = t_outstanding.get(sym, 0) + t_qty
                        continue
                    if stype == "BUY_ADD":
                        if "BUY_ADD" not in day_acted and gate_add and seg_c < up_limit * 0.998:
                            stage_idx = pos.stage
                            if stage_idx < len(pyramid):
                                equity_now = self.cash + sum(
                                    p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][2].get(date, 0), len(pool[s2][0]) - 1)])
                                    for s2, p2 in self.positions.items()
                                )
                                plan_amount = min(equity_now * plan_ratio * pyramid[stage_idx] * reduced_ratio
                                                  * defense_ratio,
                                                  self.cash * 0.99)
                                qty = _round_buy_qty(int(plan_amount / (seg_c * (1 + SLIPPAGE))), sym)
                                if qty > 0:
                                    self._buy(date, sym, name, seg_c * (1 + SLIPPAGE), qty, "buy_add", sig.reason)
                                    bought_today_qty += qty  # T+1: 当日买入部分锁定
                                    day_acted.add("BUY_ADD")
                                    if sym in self.positions:
                                        self.positions[sym].stage += 1
                        continue
                    if stype == "T_BUY":
                        want = t_outstanding.get(sym, 0)
                        if want <= 0:
                            want = max(1, int(pos.qty * float(t_cfg.get("t_position_ratio", 0.3))))
                        if want > 0 and seg_l > 0 and seg_l >= down_limit * 1.002 and seg_c < up_limit * 0.998:
                            t_qty = min(want, _round_buy_qty(int(self.cash / (seg_l * (1 + SLIPPAGE))), sym))
                            if t_qty > 0:
                                self._buy(date, sym, name, seg_l * (1 + SLIPPAGE), t_qty, "t_buy", sig.reason)
                                t_outstanding[sym] = max(0, t_outstanding.get(sym, 0) - t_qty)
                                bought_today_qty += t_qty  # T+1: 当日买入部分锁定
                        continue

            # ---- 第三步: 收盘结算净值 + 风控状态更新
            closes_final = {
                s: _f(pool[s][0]["close"].iloc[min(pool[s][2].get(date, 0), len(pool[s][0]) - 1)])
                for s in self.positions
            }
            eq = self.cash + sum(p2.qty * closes_final.get(s2, p2.cost) for s2, p2 in self.positions.items())
            self.equity_curve.append({"date": date, "equity": round(eq, 2)})
            # 更新持仓峰值(移动止损线随峰值上移)
            for s2, p2 in self.positions.items():
                p2.peak_price = max(p2.peak_price, closes_final.get(s2, p2.cost))
            prev_eq = self.equity_curve[-2]["equity"] if len(self.equity_curve) >= 2 else self.initial
            day_ret = (eq / prev_eq - 1) * 100 if prev_eq > 0 else 0.0
            if day_ret <= -daily_limit:
                self.fuse_date = date
            self._update_defense(eq)  # 软防守: 达阈值开启(仓位减半), 修复后自动解除

        return self._report(pool, skipped, n_days)

    # ------------------------------------------------------------ 股票池/统计辅助
    def _default_pool(self) -> list[str]:
        """自选 + 持仓(用户确认口径)."""
        from sqlmodel import select

        from app import db
        from app.models.models import Position, Watchlist

        with db.session_scope() as s:
            wl = [r.symbol for r in s.exec(select(Watchlist)).all()]
            pos = [r.symbol for r in s.exec(select(Position).where(Position.status == "holding")).all()]
        return list(dict.fromkeys(wl + pos))

    def _report(self, pool: dict, skipped: list[str], n_max: int) -> dict[str, Any]:
        eq = self.equity_curve
        final = eq[-1]["equity"] if eq else self.initial
        total_ret = (final / self.initial - 1) * 100 if self.initial else 0
        n_days = len(eq)
        annual = ((final / self.initial) ** (252 / n_days) - 1) * 100 if self.initial and n_days >= 2 else 0.0
        # 最大回撤
        peak = -1e18
        max_dd = 0.0
        for row in eq:
            peak = max(peak, row["equity"])
            dd = (peak - row["equity"]) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        # 日收益序列(夏普)
        rets = []
        for a, b in zip(eq[:-1], eq[1:], strict=False):
            if a["equity"] > 0:
                rets.append(b["equity"] / a["equity"] - 1)
        sharpe = (statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252)) if len(rets) > 2 and statistics.stdev(rets) > 0 else 0.0
        # 平仓交易统计(做T 高抛不视为平仓: 底仓未变, 单独统计贡献)
        closed = [t for t in self.trades if t.pnl != 0 and t.action not in ("t_sell", "t_buy")]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]
        gross_w = sum(t.pnl for t in wins)
        gross_l = abs(sum(t.pnl for t in losses))
        t_count = len(self.trades)
        turnover = sum(t.price * t.qty for t in self.trades) / ((self.initial + final) / 2) * 100 if (self.initial + final) else 0
        t_pnl = sum(t.pnl for t in self.trades if t.action in ("t_sell",))
        t_sell_n = len([t for t in self.trades if t.action == "t_sell"])
        return {
            "meta": {
                "pool": len(pool),
                "skipped": len(skipped),
                "initial_capital": self.initial,
                "final_equity": round(final, 2),
                "total_return_pct": round(total_ret, 2),
                "annual_return_pct": round(annual, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "sharpe": round(sharpe, 2),
                "days": n_days,
                "notes": "信号日T收盘判定->T+1开盘成交; 做T按当日最高/最低价近似(乐观口径); 已扣双边手续费; "
                         "风控三道闸门生效(回撤防守为软防守: 仓位减半, 修复后解除); 止损后同票冷却期生效",
            },
            "stats": {
                "trades": t_count,
                "closed": len(closed),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
                "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else (gross_w if closed else 0.0),
                "expectancy": round(sum(t.pnl for t in closed) / len(closed), 2) if closed else 0.0,
                "avg_win": round(gross_w / len(wins), 2) if wins else 0.0,
                "avg_loss": round(-gross_l / len(losses), 2) if losses else 0.0,
                "consecutive_losses_max": self.consecutive_losses,
                "turnover_pct": round(turnover, 1),
                "t_sell_count": t_sell_n,
                "t_contribution": round(t_pnl, 2),
                "fuse_triggered": self.fuse_date is not None,
                "defense_mode": self.defense_mode,
                "cooldown_blocks": self.cooldown_blocks,
                "stop_cooldown_days": self.stop_cooldown_days,
            },
            "equity_curve": eq,
            "trades": [
                {"date": t.date, "symbol": t.symbol, "name": t.name, "action": t.action,
                 "price": t.price, "qty": t.qty, "fee": round(t.fee, 2), "pnl": round(t.pnl, 2),
                 "reason": t.reason}
                for t in self.trades
            ],
        }


def run_strategy_backtest(symbols: list[str] | None = None,
                          initial_capital: float = 1_000_000.0,
                          progress_cb: Callable[[int, int], None] | None = None,
                          intraday_minutes: int = DEFAULT_MINUTES,
                          start: str = "", end: str = "") -> dict[str, Any]:
    """便捷入口. intraday_minutes: 盘中路径模拟粒度(5/10/15/30, 默认 10). start/end: 回测区间."""
    return StrategyBacktest(initial_capital=initial_capital,
                            intraday_minutes=intraday_minutes).run(
        symbols=symbols, progress_cb=progress_cb, start=start, end=end)
