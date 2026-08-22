"""持仓回测(方案 v2 §5): 以真实持仓/指定建仓组合为起点, 三线对照 + 差异归因.

模式 A(real):   Position 表当前持仓(opened_at/cost/qty/pyramid_stage 天然齐全),
                从各自建仓日起回放.
模式 B(import): 显式建仓腿列表 {symbol, entry_date, cost, qty}, 支持不同时间建仓、
                同票分批多腿; 也可从 Trade 表真实买入成交导入.

三条对照线(同一初始状态, 只差后续管理策略):
- hold(躺平): 买入持有至末日, 无任何交易 —— 实际已发生的基准
- stop(纪律): 仅执行 SELL_STOP 止损(T-1 收盘判定, T+1 开盘成交)
- signal(系统): 信号全开(止损/减仓/加仓/做T + 风控三道闸门)

复用原则(用户要求, 回测与实盘同一套操作逻辑):
- 主信号 100% 走 SignalEngine.evaluate_with_ind(skip_t=True, end=ti) —— T-1 收盘口径,
  内部优先级: 止损 > 减仓 > 加仓 > 首仓(做T 分支由回测侧单独处理, 见下);
- 做T 走 SignalEngine._check_t_trade(want="sell"/"buy") + T 日盘中高低价(乐观口径,
  用户确认) —— 不再内联任何判定逻辑;
- 循环优先级对齐新版引擎: 止损/减仓 > T_SELL(高抛) > 加仓/首仓 > T_BUY(低吸);
- 费用 compute_trade_fee / 申报 lot_rules / 涨跌停 与实盘一致.

数据: backtest_data.ensure_range(P0 前复权冻结快照通道), 指标逐股一次 compute_all.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from app.core.backtest.data import backtest_data
from app.core.backtest.path_sim import DEFAULT_MINUTES, build_intraday_path
from app.core.config import config_manager
from app.core.fees import compute_trade_fee
from app.core.indicators import compute_all
from app.core.lot_rules import (
    round_buy_qty as _round_buy_qty,
)
from app.core.lot_rules import (
    sell_qty as _sell_qty,
)
from app.core.modes import mode_for_ind
from app.core.signals.engine import PositionInfo, Signal, SignalEngine

logger = logging.getLogger(__name__)

MIN_BARS = 60          # 指标预热(不足不参与信号判定, 但持仓市值照常结算)
SLIPPAGE = 0.001       # 滑点(买卖各 0.1%, 做T 除外: 已按高低价近似)
REDUCE_RATIO = 1 / 3   # SELL_REDUCE 减仓比例

MANAGE_HOLD = "hold"        # 躺平: 买入持有
MANAGE_STOP = "stop"        # 纪律: 仅止损
MANAGE_SIGNAL = "signal"    # 系统: 信号全开

MANAGE_LABELS = {MANAGE_HOLD: "躺平(买入持有)", MANAGE_STOP: "纪律(仅止损)", MANAGE_SIGNAL: "系统(信号全开)"}

BENCHMARK_SECID = "0.000300"  # 沪深300(与配置闸门口径一致)

# 板块涨跌停限制(按代码前缀, 与 strategy.py 一致)
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


@dataclass
class Leg:
    """建仓腿(模式 A: 来自 Position 表; 模式 B: 显式传入/成交导入)."""

    symbol: str
    name: str = ""
    entry_date: str = ""      # YYYY-MM-DD
    cost: float = 0.0         # 含费摊薄成本(与实盘 Position.cost 口径一致)
    qty: int = 0
    pyramid_stage: int = 0    # 已用加仓档位(0=仅首仓)


@dataclass
class SimPosition:
    """回测持仓(单腿)."""

    symbol: str
    name: str = ""
    qty: int = 0
    cost: float = 0.0        # 加权平均成本(含费, 加仓摊薄)
    stage: int = 0           # 已用加仓档位
    entry_date: str = ""
    peak_price: float = 0.0  # 持仓期间最高收盘价(移动止损用)


@dataclass
class SimTrade:
    date: str
    symbol: str
    name: str
    action: str          # buy_entry/buy_add/sell_reduce/sell_stop/t_sell/t_buy
    price: float
    qty: int
    fee: float
    pnl: float = 0.0
    reason: str = ""


class PortfolioBacktest:
    """持仓回测引擎: 同一腿集分别跑 躺平/纪律/系统 三条线, 输出对照+归因."""

    def __init__(self, manage: str = MANAGE_SIGNAL, initial_capital: float = 0.0,
                 progress_cb: Callable[[int, int], None] | None = None,
                 intraday_minutes: int = DEFAULT_MINUTES) -> None:
        self.manage = manage if manage in (MANAGE_HOLD, MANAGE_STOP, MANAGE_SIGNAL) else MANAGE_SIGNAL
        self.initial = float(initial_capital)
        self.engine = SignalEngine()
        self.cfg = config_manager.get()
        self.fee_cfg = self.cfg.get("手续费", {})
        self.progress_cb = progress_cb
        # 盘中路径模拟粒度(分钟): 全部信号在构造的日内路径上逐段触发(用户确认口径)
        self.intraday_minutes = intraday_minutes if intraday_minutes in (5, 10, 15, 30) else DEFAULT_MINUTES

    def _day_path(self, ind: pd.DataFrame, i: int) -> list[dict[str, float]]:
        """当日盘中路径(确定性 OHLC 插值, 段数 = 240 / 粒度)."""
        return build_intraday_path(
            _f(ind["open"].iloc[i]), _f(ind["high"].iloc[i]),
            _f(ind["low"].iloc[i]), _f(ind["close"].iloc[i]),
            minutes=self.intraday_minutes,
        )

    # ------------------------------------------------------------ 主入口
    def run(self, legs: list[Leg], start: str = "", end: str = "") -> dict[str, Any]:
        """运行持仓回测. legs: 建仓腿(至少 1 条). 返回三线对照报告."""
        legs = [leg for leg in legs if leg.symbol and leg.qty > 0 and leg.cost > 0]
        if not legs:
            return {"error": "无有效建仓腿(需 symbol/qty/cost > 0)"}

        # ---- 数据准备: P0 冻结快照通道, 区间 = [最早建仓日回退预热, 末日]
        start, end = self._resolve_dates(legs, start, end)
        pool: dict[str, tuple[pd.DataFrame, dict[str, int]]] = {}
        skipped: list[str] = []
        # 初始资金兜底: 至少覆盖全部期初投入(入场成本含费口径), 三条线统一起点
        need = sum(leg.qty * leg.cost for leg in legs)
        if self.initial <= 0 or self.initial < need:
            self.initial = need * 1.05
        for leg in legs:
            if leg.symbol in pool:
                continue
            r = backtest_data.ensure_range(leg.symbol, start, end)
            if r.get("source") == "none" or r.get("row_count", 0) == 0:
                skipped.append(leg.symbol)
                continue
            df = pd.DataFrame(r.get("rows", []))
            if "close" not in df.columns or len(df) < 2:
                skipped.append(leg.symbol)
                continue
            for c in ("open", "high", "low", "close", "volume", "amount"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if len(df) < 2:
                skipped.append(leg.symbol)
                continue
            ind = compute_all(
                df,
                ma_short=self.cfg["趋势"]["ma_short"], ma_mid=self.cfg["趋势"]["ma_mid"], ma_long=self.cfg["趋势"]["ma_long"],
                macd_fast=self.cfg["动量"]["macd_fast"], macd_slow=self.cfg["动量"]["macd_slow"], macd_signal=self.cfg["动量"]["macd_signal"],
                rsi_period=self.cfg["动量"]["rsi_period"], roc_period=self.cfg["动量"]["roc_period"],
                volume_ma=self.cfg["量能"]["volume_ma"],
            )
            di = {str(d)[:10]: i for i, d in enumerate(ind["date"])}
            pool[leg.symbol] = (ind, di)
        if not pool:
            return {"error": f"全部建仓腿无可用日线数据({len(skipped)} 只跳过)"}

        active_legs = [leg for leg in legs if leg.symbol in pool]
        if not active_legs:
            return {"error": "无有效建仓腿"}
        if skipped:
            logger.info("持仓回测: %d 只无数据跳过: %s", len(skipped), skipped)

        # ---- 时间轴: 全部股票交易日并集(含腿入场前的日期, 组合从最早入场日开始)
        all_dates = sorted(set().union(*(di.keys() for _, di in pool.values())))
        n_days = len(all_dates)

        # ---- 三条线(共享数据, 独立状态)
        hold = self._simulate(active_legs, pool, all_dates, MANAGE_HOLD)
        stop = self._simulate(active_legs, pool, all_dates, MANAGE_STOP)
        signal = self._simulate(active_legs, pool, all_dates, MANAGE_SIGNAL)
        if self.progress_cb:
            self.progress_cb(n_days, n_days)

        # ---- 基准(沪深300, 归一化到组合起点)
        bench = backtest_data.load_index(BENCHMARK_SECID, start, end)
        bench_curve = self._normalize_benchmark(bench.get("rows", []), all_dates)

        return self._report(active_legs, all_dates, hold, stop, signal, bench_curve, skipped)

    # ------------------------------------------------------------ 数据/日期
    def _resolve_dates(self, legs: list[Leg], start: str, end: str) -> tuple[str, str]:
        """区间: 显式传入优先; 否则 end=今天, start=最早建仓日回退预热根数."""
        if not end:
            end = pd.Timestamp.today().strftime("%Y-%m-%d")
        if not start:
            entries = [leg.entry_date[:10] for leg in legs if leg.entry_date]
            if entries:
                # 最早建仓日再回退预热(指标需要历史), 但不超过 5 年前
                base = min(entries)
                start = (pd.Timestamp(base) - pd.Timedelta(days=int(MIN_BARS * 1.6) + 40)).strftime("%Y-%m-%d")
            else:
                start = (pd.Timestamp(end) - pd.Timedelta(days=int(MIN_BARS * 1.6) + 40)).strftime("%Y-%m-%d")
        return start[:10], end[:10]

    def _normalize_benchmark(self, rows: list[dict], all_dates: list[str]) -> list[dict]:
        """基准序列: 对齐组合交易日并集, 以组合首个有数据的日期为 1.0."""
        if not rows:
            return []
        by_date = {str(r.get("date", ""))[:10]: _f(r.get("close")) for r in rows}
        series = [(d, by_date[d]) for d in all_dates if d in by_date and by_date[d] > 0]
        if len(series) < 2:
            return []
        base = series[0][1]
        return [{"date": d, "equity": round(v / base, 4)} for d, v in series]

    # ------------------------------------------------------------ 模拟(单条线)
    def _simulate(self, legs: list[Leg], pool: dict, all_dates: list[str],
                  manage: str) -> dict[str, Any]:
        """跑一条对照线. 返回 {curve, trades, cash, pos_end, leg_flows, fuse, defense}."""
        cash = self.initial
        positions: dict[str, SimPosition] = {}
        trades: list[SimTrade] = []
        curve: list[dict[str, Any]] = []
        t_outstanding: dict[str, int] = {}          # 做T 未买回计数(防底仓漂移)
        consecutive_losses = 0
        peak_equity = max(self.initial, 1.0)
        fuse_date: str | None = None
        defense_mode = False
        cooldown_blocks = 0                     # 冷却期拦截的再入场次数(symbol-日口径)
        last_stop_day: dict[str, int] = {}      # 止损冷却: symbol -> 最近一次止损的交易日序号
        # 每腿现金流: symbol -> {"out": 买入流出, "in": 卖出流入(含期末市值), "pnl_by_action": {...}}
        leg_flows: dict[str, dict[str, Any]] = {
            leg.symbol: {"out": 0.0, "in": 0.0, "pnl_by_action": {}}
            for leg in legs
        }
        leg_done: list[bool] = [False] * len(legs)  # 每条腿独立入场标记(同 symbol 多腿都入场)
        ever_entered: dict[str, bool] = {}          # 该票是否已入场过(清仓/止损后可再买入)
        leg_names: dict[str, str] = {leg.symbol: leg.name for leg in legs}  # 再入场时取名称

        risk = self.cfg["风控"]
        position_cfg = self.cfg["仓位"]
        t_cfg = self.cfg["做T"]
        plan_ratio = float(risk["single_position_pct"]) / 100.0
        max_total = float(risk["total_position_pct"]) / 100.0
        daily_limit = float(risk["daily_loss_limit_pct"])
        loss_limit = int(risk["consecutive_loss_limit"])
        drawdown_limit = float(risk["max_drawdown_pct"])
        # 止损冷却 + 防守解除线(与策略回测同口径, 对齐实盘)
        cooldown_days = int(risk.get("stop_cooldown_days", 10))
        defense_recovery = float(risk.get("defense_recovery_ratio", 0.5))
        pyramid = list(position_cfg.get("pyramid_ratios", [0.5, 0.3, 0.2]))

        def pos_info(p: SimPosition | None) -> PositionInfo:
            return (PositionInfo(symbol=p.symbol, cost=p.cost, qty=p.qty, peak_price=p.peak_price)
                    if p else PositionInfo(symbol=""))

        def buy(date: str, symbol: str, name: str, price: float, qty: int,
                action: str, reason: str) -> None:
            nonlocal cash
            if qty <= 0:
                return
            amount = price * qty
            fee = compute_trade_fee("buy", amount, self.fee_cfg)
            if cash < amount + fee:
                return
            cash -= amount + fee
            p = positions.get(symbol)
            if p is None:
                p = SimPosition(symbol=symbol, name=name, entry_date=date, peak_price=price)
                positions[symbol] = p
            total_cost = p.cost * p.qty + price * qty
            p.qty += qty
            p.cost = total_cost / p.qty if p.qty else 0.0
            p.peak_price = max(p.peak_price, price)
            leg_flows[symbol]["out"] += amount + fee
            trades.append(SimTrade(date, symbol, name, action, price, qty, fee, reason=reason))

        def sell(date: str, symbol: str, name: str, price: float, qty: int,
                 action: str, reason: str) -> None:
            nonlocal cash, consecutive_losses
            p = positions.get(symbol)
            if p is None or qty <= 0:
                return
            qty = min(qty, p.qty)
            amount = price * qty
            fee = compute_trade_fee("sell", amount, self.fee_cfg)
            cash += amount - fee
            pnl = (price - p.cost) * qty - fee
            p.qty -= qty
            leg_flows[symbol]["in"] += amount - fee
            leg_flows[symbol]["pnl_by_action"][action] = \
                leg_flows[symbol]["pnl_by_action"].get(action, 0.0) + pnl
            if p.qty == 0:
                positions.pop(symbol, None)
            trades.append(SimTrade(date, symbol, name, action, price, qty, fee, pnl=pnl, reason=reason))
            if action in ("sell_stop", "sell_reduce"):
                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0

        def trade_guard(sym: str, ind: pd.DataFrame, idx_now: int, open_px: float,
                        high_px: float, low_px: float) -> tuple[bool, float, float]:
            """涨跌停/价格跳变防护(除权日/脏数据不成交).

            返回 (可交易, 涨停价, 跌停价). 方向判定(涨停不可买/跌停不可卖)由调用方执行.
            """
            prev_close = _f(ind["close"].iloc[idx_now - 1]) if idx_now >= 1 else 0.0
            limit = _limit_pct(sym)
            up = prev_close * (1 + limit) if prev_close > 0 else 1e18
            down = prev_close * (1 - limit) if prev_close > 0 else 0.0
            if prev_close > 0 and any(px > 0 and abs(px / prev_close - 1) > limit * 1.6
                                      for px in (open_px, high_px, low_px)):
                return False, up, down
            ma20 = _f(ind["ma20"].iloc[idx_now]) if "ma20" in ind.columns else 0.0
            if ma20 > 0 and any(px > 0 and abs(px / ma20 - 1) > 0.40
                                for px in (open_px, high_px, low_px)):
                return False, up, down
            return True, up, down

        for day_idx, date in enumerate(all_dates):
            if self.progress_cb and manage == MANAGE_SIGNAL and (day_idx % 100 == 0 or day_idx == len(all_dates) - 1):
                self.progress_cb(day_idx + 1, len(all_dates))

            # ---- 腿入场(历史事实: 按真实含费成本成交, 非信号; 同一日全部入场)
            # 同 symbol 多腿独立入场: 分批建仓(不同日期)各自生效, 持仓按加权成本合并
            for idx, leg in enumerate(legs):
                if leg_done[idx]:
                    continue
                if leg.entry_date[:10] <= date:
                    leg_done[idx] = True
                    ever_entered[leg.symbol] = True
                    cash -= leg.qty * leg.cost
                    p = positions.get(leg.symbol)
                    if p is None:
                        p = SimPosition(symbol=leg.symbol, name=leg.name, cost=leg.cost, qty=leg.qty,
                                        stage=leg.pyramid_stage, entry_date=leg.entry_date,
                                        peak_price=leg.cost)
                        positions[leg.symbol] = p
                    else:
                        total = p.cost * p.qty + leg.cost * leg.qty
                        p.qty += leg.qty
                        p.cost = total / p.qty  # 加权平均成本(与 buy 口径一致)
                        p.peak_price = max(p.peak_price, leg.cost)
                    leg_flows[leg.symbol]["out"] += leg.qty * leg.cost
                    trades.append(SimTrade(date, leg.symbol, leg.name, "buy_entry", leg.cost, leg.qty, 0.0,
                                           reason=f"建仓腿(成本{leg.cost:.2f})"))
                    if p.stage < 1:
                        p.stage = max(1, p.stage)

            if manage == MANAGE_HOLD:
                pass  # 躺平: 无任何管理
            else:
                # ---- 风控闸门(基于昨日状态; 熔断/防守当日立即生效)
                gate_open = fuse_date is None or date > fuse_date
                # 回撤防守=软防守(对齐实盘 risk/manager: 仓位上限砍半, 不禁开仓; 修复后自动解除)
                defense_ratio = 0.5 if defense_mode else 1.0
                gate_add = gate_open
                reduced_ratio = 0.5 if consecutive_losses >= loss_limit else 1.0

                # ---- 逐票管理: 盘中路径逐段推进(全部信号盘中触发, 用户确认口径)
                # 每段: 「T-1 收盘指标 + 段内价格(open/high/low/close)」调信号引擎(与实盘
                # 盘中评估同语义), 命中即按段价成交; 当日买入段后 T+1 保护(后续段不可卖).
                for sym, (ind, di) in pool.items():
                    pos = positions.get(sym)
                    if pos is None:
                        # 腿未入场过: 不参与管理(建仓由腿决定)
                        if not ever_entered.get(sym, False):
                            continue
                        # ---- 再入场: 止损/清仓后, BUY_FIRST 盘中触发时重新买入
                        if manage == MANAGE_STOP or not gate_open:
                            continue  # 纪律线保持纯止损口径: 清仓后不再买入
                        # 止损冷却期内禁止同票再入场(防连跌中反复接刀)
                        last_stop = last_stop_day.get(sym)
                        if cooldown_days > 0 and last_stop is not None \
                                and (day_idx - last_stop) < cooldown_days:
                            cooldown_blocks += 1
                            continue
                        i = di.get(date)
                        if i is None or i < MIN_BARS + 1:
                            continue
                        ti = i - 1
                        ok_trade, up_limit, _down = trade_guard(sym, ind, i,
                                                                 _f(ind["open"].iloc[i]),
                                                                 _f(ind["high"].iloc[i]),
                                                                 _f(ind["low"].iloc[i]))
                        if not ok_trade:
                            continue
                        equity_now = cash + sum(
                            p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][1].get(date, 0), len(pool[s2][0]) - 1)])
                            for s2, p2 in positions.items()
                        )
                        used_pct = sum(
                            p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][1].get(date, 0), len(pool[s2][0]) - 1)])
                            for s2, p2 in positions.items()
                        ) / equity_now if equity_now else 1.0
                        if used_pct >= max_total:
                            continue
                        for seg in self._day_path(ind, i):
                            seg_c = seg["close"]
                            if seg_c >= up_limit * 0.998:
                                continue
                            re_sig = self.engine.evaluate_with_ind(
                                symbol=sym, name=leg_names.get(sym, ""), ind=ind,
                                position=PositionInfo(symbol=sym),
                                quote_price=seg_c, quote_high=seg["high"], quote_low=seg["low"],
                                end=ti, skip_t=False,
                            )
                            if re_sig is None or re_sig.type != "BUY_FIRST":
                                continue
                            plan_amount = min(equity_now * plan_ratio * reduced_ratio * defense_ratio,
                                              cash * 0.99)
                            qty = _round_buy_qty(int(plan_amount / (seg_c * (1 + SLIPPAGE))), sym)
                            if qty <= 0:
                                continue
                            buy(date, sym, leg_names.get(sym, ""), seg_c * (1 + SLIPPAGE), qty,
                                "buy_first", re_sig.reason)
                            if sym in positions:
                                positions[sym].stage = 1  # 首仓已用, 后续可加仓
                            break  # 已再入场, 当日该票结束
                        continue
                    if pos.entry_date and date <= pos.entry_date[:10]:
                        continue  # 入场日只建仓, 次日开始管理(首个信号日=入场日收盘)
                    i = di.get(date)
                    if i is None or i < MIN_BARS + 1:
                        continue
                    ti = i - 1  # 信号日(指标只算到 T-1 收盘, 无前视)
                    ok_trade, up_limit, down_limit = trade_guard(sym, ind, i,
                                                                 _f(ind["open"].iloc[i]),
                                                                 _f(ind["high"].iloc[i]),
                                                                 _f(ind["low"].iloc[i]))
                    if not ok_trade:
                        continue
                    bought_today_qty = 0  # T+1: 当日买入数量(当日买入部分不可卖, 老仓可卖)
                    day_acted: set[str] = set()  # 日线信号型动作每日最多一次(减仓/加仓); 做T 不限制
                    # 模式决策每天一次(T-1 收盘, 无前视), 供盘中止损预检
                    mode_dec = mode_for_ind(ind, self.cfg, end=ti)
                    last_row = ind.iloc[ti]
                    prev_row = ind.iloc[max(ti - 1, 0)]
                    for seg in self._day_path(ind, i):
                        pos = positions.get(sym)
                        if pos is None:
                            break  # 已止损清仓, 当日结束(再入场次日判定)
                        seg_c = seg["close"]
                        seg_h = seg["high"]
                        seg_l = seg["low"]
                        p_info = pos_info(pos)
                        sellable = max(0, pos.qty - bought_today_qty)  # 老仓部分(当日买入 T+1 锁定)
                        # ---- 盘中止损预检: 段最低价触及止损线即离场(真实盘中语义, 不等段收盘)
                        stop_sig = self.engine._check_stop(
                            self.cfg, ind, last_row, prev_row, p_info,
                            price=seg_l, name=pos.name, mode_decision=mode_dec,
                        )
                        if stop_sig is not None:
                            if sellable > 0 and seg_l > down_limit * 1.002:
                                sell(date, sym, pos.name, seg_l * (1 - SLIPPAGE), sellable,
                                     "sell_stop", stop_sig.reason)
                                last_stop_day[sym] = day_idx  # 启动冷却计时
                            continue  # 盘中触及止损: 当日该票结束
                        if manage == MANAGE_STOP:
                            continue  # 纪律线: 只认止损(预检已覆盖), 其余忽略
                        # ---- 其余信号盘中触发: 引擎按优先级返回最强信号(止损>减仓>T_SELL>加仓>首仓>T_BUY)
                        sig: Signal | None = self.engine.evaluate_with_ind(
                            symbol=sym, name=pos.name, ind=ind, position=p_info,
                            quote_price=seg_c, quote_high=seg_h, quote_low=seg_l,
                            end=ti, skip_t=False,
                        )
                        stype = sig.type if sig else ""
                        # ---- 系统线: 段内命中即执行(每段最多一个动作, 路径推进)
                        if stype == "SELL_STOP":
                            if sellable > 0 and seg_c > down_limit * 1.002:
                                self._sell_stop(sell, date, sym, pos.name,
                                                seg_c * (1 - SLIPPAGE), sellable, sig)
                                last_stop_day[sym] = day_idx  # 启动冷却计时
                            continue
                        if stype == "SELL_REDUCE":
                            if "SELL_REDUCE" not in day_acted and sellable > 0 and seg_c > down_limit * 1.002:
                                qty = _sell_qty(max(1, int(sellable * REDUCE_RATIO)), sellable, sym)
                                if qty > 0:
                                    sell(date, sym, pos.name, seg_c * (1 - SLIPPAGE), qty,
                                         "sell_reduce", sig.reason if sig else "减仓")
                                    day_acted.add("SELL_REDUCE")
                            continue
                        if stype == "T_SELL":
                            if sellable > 0 and seg_h > 0:
                                t_qty = max(1, int(sellable * float(t_cfg.get("t_position_ratio", 0.3))))
                                t_qty = min(t_qty, sellable)  # 不卖当日新买入部分
                                t_qty = _sell_qty(t_qty, sellable, sym)  # 申报取整(防科创板碎股)
                                if t_qty > 0 and seg_c > down_limit * 1.002:
                                    sell(date, sym, pos.name, seg_h * (1 - SLIPPAGE), t_qty,
                                         "t_sell", sig.reason if sig else "做T高抛")
                                    t_outstanding[sym] = t_outstanding.get(sym, 0) + t_qty
                            continue
                        if stype == "BUY_ADD":
                            if "BUY_ADD" not in day_acted and gate_add and seg_c < up_limit * 0.998:
                                stage_idx = pos.stage
                                if stage_idx < len(pyramid):
                                    equity_now = cash + sum(
                                        p2.qty * _f(pool[s2][0]["close"].iloc[min(pool[s2][1].get(date, 0), len(pool[s2][0]) - 1)])
                                        for s2, p2 in positions.items()
                                    )
                                    plan_amount = min(equity_now * plan_ratio * pyramid[stage_idx] * reduced_ratio
                                                      * defense_ratio,
                                                      cash * 0.99)
                                    qty = _round_buy_qty(int(plan_amount / (seg_c * (1 + SLIPPAGE))), sym)
                                    if qty > 0:
                                        buy(date, sym, pos.name, seg_c * (1 + SLIPPAGE), qty,
                                            "buy_add", sig.reason if sig else "加仓")
                                        bought_today_qty += qty  # T+1: 当日买入部分锁定
                                        day_acted.add("BUY_ADD")
                                        if sym in positions:
                                            positions[sym].stage += 1
                            continue
                        if stype == "T_BUY":
                            want = t_outstanding.get(sym, 0)
                            if want <= 0:
                                want = max(1, int(pos.qty * float(t_cfg.get("t_position_ratio", 0.3))))
                            if want > 0 and seg_l > 0 and seg_c < up_limit * 0.998 and seg_l >= down_limit * 1.002:
                                t_qty = min(want, _round_buy_qty(int(cash / (seg_l * (1 + SLIPPAGE))), sym))
                                if t_qty > 0:
                                    buy(date, sym, pos.name, seg_l * (1 + SLIPPAGE), t_qty,
                                        "t_buy", sig.reason if sig else "做T低吸")
                                    t_outstanding[sym] = max(0, t_outstanding.get(sym, 0) - t_qty)
                                    bought_today_qty += t_qty  # T+1: 当日买入部分锁定
                            continue

            # ---- 收盘结算: 组合净值 + 每腿市值 + 风控状态
            closes = {
                s: _f(pool[s][0]["close"].iloc[min(pool[s][1].get(date, 0), len(pool[s][0]) - 1)])
                for s in positions
            }
            eq = cash + sum(p2.qty * closes.get(s2, p2.cost) for s2, p2 in positions.items())
            curve.append({"date": date, "equity": round(eq, 2)})
            # 更新持仓峰值(移动止损线随峰值上移)
            for s2, p2 in positions.items():
                p2.peak_price = max(p2.peak_price, closes.get(s2, p2.cost))
            if manage != MANAGE_HOLD:
                prev_eq = curve[-2]["equity"] if len(curve) >= 2 else self.initial
                day_ret = (eq / prev_eq - 1) * 100 if prev_eq > 0 else 0.0
                if day_ret <= -daily_limit:
                    fuse_date = date
                peak_equity = max(peak_equity, eq)
                # 软防守(对齐实盘): 达阈值开启(仓位减半), 修复至阈值×defense_recovery 以下解除
                dd = (peak_equity - eq) / peak_equity * 100 if peak_equity > 0 else 0.0
                if dd >= drawdown_limit:
                    defense_mode = True
                elif defense_mode and dd < drawdown_limit * defense_recovery:
                    defense_mode = False

        # ---- 收尾: 每腿期末市值并入 in(归因口径: 期末市值 + 已实现 - 投入)
        for sym, p in positions.items():
            closes = {s: _f(pool[s][0]["close"].iloc[-1]) for s in pool}
            end_close = closes.get(sym, p.cost)
            leg_flows[sym]["in"] += p.qty * end_close

        return {
            "curve": curve,
            "trades": trades,
            "cash": cash,
            "leg_flows": leg_flows,
            "fuse": fuse_date is not None,
            "defense": defense_mode,
            "cooldown_blocks": cooldown_blocks,
        }

    def _sell_stop(self, sell, date: str, sym: str, name: str, open_px: float,
                   qty: int, sig: Signal | None) -> None:
        sell(date, sym, name, open_px * (1 - SLIPPAGE), qty, "sell_stop",
             sig.reason if sig else "止损")

    # ------------------------------------------------------------ 报告
    def _report(self, legs: list[Leg], all_dates: list[str],
                hold: dict, stop: dict, signal: dict,
                bench_curve: list[dict], skipped: list[str]) -> dict[str, Any]:
        def line_ret(curve: list[dict]) -> float:
            if not curve:
                return 0.0
            return (curve[-1]["equity"] / self.initial - 1) * 100 if self.initial else 0.0

        def max_dd(curve: list[dict]) -> float:
            peak = -1e18
            dd = 0.0
            for row in curve:
                peak = max(peak, row["equity"])
                dd = max(dd, (peak - row["equity"]) / peak * 100 if peak > 0 else 0.0)
            return round(dd, 2)

        def sharpe(curve: list[dict]) -> float:
            rets = []
            for a, b in zip(curve[:-1], curve[1:], strict=False):
                if a["equity"] > 0:
                    rets.append(b["equity"] / a["equity"] - 1)
            if len(rets) > 2 and statistics.stdev(rets) > 0:
                return round(statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252), 2)
            return 0.0

        # ---- 每腿归因: 所选管理线回报 - 躺平线回报(期末市值差 + 已实现)
        # 同 symbol 多腿(分批建仓)聚合为一行: 持仓本就合并, 成本/数量按合并口径展示
        managed = {"hold": hold, "stop": stop, "signal": signal}[self.manage]
        leg_rows: list[dict[str, Any]] = []
        seen_syms: set[str] = set()
        for leg in legs:
            sym = leg.symbol
            if sym in seen_syms:
                continue
            seen_syms.add(sym)
            sym_legs = [leg for leg in legs if leg.symbol == sym]
            total_qty = sum(leg.qty for leg in sym_legs)
            avg_cost = sum(leg.qty * leg.cost for leg in sym_legs) / total_qty if total_qty else 0.0
            entry_date = min((leg.entry_date or "9999")[:10] for leg in sym_legs)
            hf = hold["leg_flows"].get(sym, {"out": 0.0, "in": 0.0})
            mf = managed["leg_flows"].get(sym, {"out": 0.0, "in": 0.0})
            hold_ret = (hf["in"] - hf["out"]) / hf["out"] * 100 if hf["out"] else 0.0
            mgd_ret = (mf["in"] - mf["out"]) / mf["out"] * 100 if mf["out"] else 0.0
            attribution = {k: round(v, 2) for k, v in mf.get("pnl_by_action", {}).items()}
            leg_rows.append({
                "symbol": sym, "name": sym_legs[0].name, "entry_date": entry_date,
                "cost": round(avg_cost, 3), "qty": total_qty, "legs": len(sym_legs),
                "hold_return_pct": round(hold_ret, 2),
                "managed_return_pct": round(mgd_ret, 2),
                "excess_pct": round(mgd_ret - hold_ret, 2),
                "attribution": attribution,
            })

        # ---- 组合曲线(以最早入场日为起点对齐: 三条线首日相同)
        s_trades = managed["trades"]
        closed = [t for t in s_trades if t.pnl != 0 and t.action not in ("t_buy", "buy_add", "buy_entry")]
        wins = [t for t in closed if t.pnl > 0]
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        t_pnl = sum(t.pnl for t in s_trades if t.action == "t_sell")

        meta = {
            "manage": self.manage, "manage_label": MANAGE_LABELS[self.manage],
            "legs": len(legs), "skipped": len(skipped),
            "initial_capital": round(self.initial, 2),
            "days": len(all_dates),
            "benchmark": "沪深300",
            "notes": "主信号复用 SignalEngine(T-1 收盘判定, T+1 开盘成交); 做T 复用 _check_t_trade(当日高低价近似, 乐观口径); "
                     "优先级 止损>减仓>T_SELL>加仓>首仓>T_BUY; 已扣双边手续费; 数据为前复权冻结快照",
        }
        return {
            "meta": meta,
            "curves": {
                "hold": hold["curve"],
                "stop": stop["curve"],
                "signal": signal["curve"],
                "benchmark": bench_curve,
            },
            "stats": {
                "hold_return_pct": round(line_ret(hold["curve"]), 2),
                "stop_return_pct": round(line_ret(stop["curve"]), 2),
                "signal_return_pct": round(line_ret(signal["curve"]), 2),
                "signal_annual_pct": round(
                    self._annual(line_ret(signal["curve"]), len(signal["curve"])), 2),
                "managed_max_drawdown_pct": max_dd(managed["curve"]),
                "managed_sharpe": sharpe(managed["curve"]),
                "excess_vs_hold_pct": round(line_ret(managed["curve"]) - line_ret(hold["curve"]), 2),
                "trades": len(s_trades),
                "closed": len(closed),
                "win_rate": win_rate,
                "t_sell_count": len([t for t in s_trades if t.action == "t_sell"]),
                "t_contribution": round(t_pnl, 2),
                "fuse_triggered": signal["fuse"],
                "defense_mode": signal["defense"],
            },
            "legs": leg_rows,
            "trades": [
                {"date": t.date, "symbol": t.symbol, "name": t.name, "action": t.action,
                 "price": round(t.price, 3), "qty": t.qty, "fee": round(t.fee, 2),
                 "pnl": round(t.pnl, 2), "reason": t.reason}
                for t in s_trades
            ],
        }

    @staticmethod
    def _annual(total_ret_pct: float, days: int) -> float:
        """年化收益率(%, 按 252 交易日折算). days<2 时返回 0."""
        if days < 2 or total_ret_pct <= -100:
            return 0.0
        factor = (1 + total_ret_pct / 100) ** (252 / days) - 1
        return factor * 100

    # ------------------------------------------------------------ 模式 A 便捷入口
    @staticmethod
    def load_position_legs(session=None) -> list[Leg]:
        """当前持仓 -> 建仓腿(opened_at/cost/qty/pyramid_stage 天然齐全)."""
        from sqlmodel import select

        from app import db
        from app.models.models import Position

        with session or db.session_scope() as s:
            rows = s.exec(select(Position).where(Position.status == "holding")).all()
        legs = []
        for p in rows:
            if not p.qty or not p.cost:
                continue
            legs.append(Leg(
                symbol=p.symbol, name=p.name,
                entry_date=(p.opened_at or "")[:10], cost=p.cost, qty=p.qty,
                pyramid_stage=p.pyramid_stage,
            ))
        return legs

    @staticmethod
    def legs_from_trades(session=None, symbols: list[str] | None = None) -> list[Leg]:
        """模式 B 导入: 从 Trade 表真实买入成交生成建仓腿(不同时间建仓)."""
        from sqlmodel import select

        from app import db
        from app.models.models import Trade

        with session or db.session_scope() as s:
            stmt = select(Trade).where(Trade.action == "buy").order_by(Trade.time.asc())
            if symbols:
                stmt = stmt.where(Trade.symbol.in_(symbols))
            rows = s.exec(stmt).all()
        return [Leg(
            symbol=t.symbol, name=t.name, entry_date=(t.time or "")[:10],
            cost=t.price, qty=t.qty,
        ) for t in rows if t.qty and t.price]


def run_portfolio_backtest(legs: list[Leg], manage: str = MANAGE_SIGNAL,
                           initial_capital: float = 0.0, start: str = "", end: str = "",
                           progress_cb: Callable[[int, int], None] | None = None,
                           intraday_minutes: int = DEFAULT_MINUTES) -> dict[str, Any]:
    """便捷入口. intraday_minutes: 盘中路径模拟粒度(5/10/15/30, 默认 10)."""
    return PortfolioBacktest(manage=manage, initial_capital=initial_capital,
                             progress_cb=progress_cb,
                             intraday_minutes=intraday_minutes).run(legs, start=start, end=end)
