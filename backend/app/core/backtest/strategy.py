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
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from app.core.config import config_manager
from app.core.datasource import kline_store
from app.core.fees import compute_trade_fee
from app.core.indicators import compute_all
from app.core.lot_rules import (
    min_buy_unit as _min_unit,
    round_buy_qty as _round_buy_qty,
    sell_qty as _sell_qty,
)
from app.core.signals.engine import PositionInfo, Signal, SignalEngine

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

    def __init__(self, initial_capital: float = 1_000_000.0) -> None:
        self.engine = SignalEngine()
        self.cfg = config_manager.get()
        self.fee_cfg = self.cfg.get("手续费", {})
        self.initial = float(initial_capital)
        self.cash = self.initial
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict[str, Any]] = []
        # 风控状态(独立)
        self.consecutive_losses = 0
        self.peak_equity = self.initial
        self.fuse_date: str | None = None    # 日亏熔断生效日
        self.defense_mode = False            # 回撤防守

    # ------------------------------------------------------------ 辅助
    def _pos_info(self, p: Position) -> PositionInfo:
        return PositionInfo(symbol=p.symbol, cost=p.cost, qty=p.qty)

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
            p = Position(symbol=symbol, name=name)
            self.positions[symbol] = p
        # 加权平均成本(不含费, 与真实系统口径一致: 费用单独计)
        total_cost = p.cost * p.qty + price * qty
        p.qty += qty
        p.cost = total_cost / p.qty if p.qty else 0.0
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
            progress_cb: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        """运行策略回测. symbols: 股票池(为空=自选+持仓). 返回报告."""
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
        drawdown_limit = float(risk["max_drawdown_pct"])
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
        # 做T 未买回计数(防底仓漂移): symbol -> T_SELL 卖出未买回数量
        t_outstanding: dict[str, int] = {}
        n_days = len(all_dates)

        for day_idx, date in enumerate(all_dates):
            if progress_cb and (day_idx % 50 == 0 or day_idx == n_days - 1):
                progress_cb(day_idx + 1, n_days)

            # ---- 风控闸门(基于昨日状态; 熔断/防守当日立即生效, 次日延续)
            gate_open = self.fuse_date is None or date > self.fuse_date
            gate_add = gate_open and not self.defense_mode
            if self.defense_mode:
                gate_open = False
            reduced_ratio = 0.5 if self.consecutive_losses >= loss_limit else 1.0

            # ---- 第一步: 对池内每只股票, 用前一交易日(T-1)收盘判定信号
            day_signals: list[tuple[str, Signal, float, float, float, float]] = []  # (sym, sig, open, high, low)
            day_closes: dict[str, float] = {}
            for sym, (ind, name, di) in pool.items():
                i = di.get(date)  # 执行日在个股中的索引
                if i is None or i < MIN_BARS + 1:
                    continue
                ti = i - 1  # 信号日
                day_closes[sym] = _f(ind["close"].iloc[i])
                if ti < MIN_BARS:
                    continue
                pos = self.positions.get(sym)
                pos_info = self._pos_info(pos) if pos else PositionInfo(symbol=sym)
                sig = self.engine.evaluate_with_ind(
                    symbol=sym, name=name, ind=ind, position=pos_info,
                    quote_price=None, quote_high=None, quote_low=None, end=ti,
                )
                if sig is None:
                    continue
                day_signals.append((
                    sym, sig,
                    _f(ind["open"].iloc[i]), _f(ind["high"].iloc[i]), _f(ind["low"].iloc[i]),
                ))

            # ---- 第二步: 执行(先卖后买, 同日内多信号按类型排序)
            day_closes.setdefault("", 0.0)
            # 卖出类优先(释放资金), 买入类其次, 做T 最后
            sell_types = {"SELL_STOP", "SELL_REDUCE", "T_SELL"}
            buy_types = {"BUY_FIRST", "BUY_ADD", "T_BUY"}
            day_signals.sort(key=lambda x: (0 if x[1].type in sell_types else (1 if x[1].type in buy_types else 2),
                                            x[1].type))

            for sym, sig, open_px, high_px, low_px in day_signals:
                stype = sig.type
                name = pool[sym][1]
                pos = self.positions.get(sym)
                di_sym = pool[sym][2]
                idx_now = di_sym[date]  # 执行日索引(第一步已确认存在)
                prev_close = _f(pool[sym][0]["close"].iloc[idx_now - 1]) if idx_now >= 1 else 0.0
                limit = _limit_pct(sym)
                up_limit = prev_close * (1 + limit) if prev_close > 0 else 1e18
                down_limit = prev_close * (1 - limit) if prev_close > 0 else 0.0
                # 涨停不可买 / 跌停不可卖
                if stype in ("BUY_FIRST", "BUY_ADD", "T_BUY") and open_px >= up_limit * 0.998:
                    continue
                if stype in ("SELL_STOP", "SELL_REDUCE", "T_SELL") and open_px <= down_limit * 1.002:
                    continue
                # 价格跳变防护: 开盘/高低价相对昨收跳变远超涨跌停(未复权除权日/脏数据) -> 当日不成交
                if prev_close > 0 and any(
                    px > 0 and abs(px / prev_close - 1) > limit * 1.6
                    for px in (open_px, high_px, low_px)
                ):
                    continue
                # 均线偏离防护: 相对 MA20 偏离 > 40%(除权断层/数据错乱) -> 当日不成交
                ma20 = _f(pool[sym][0]["ma20"].iloc[idx_now]) if "ma20" in pool[sym][0].columns else 0.0
                if ma20 > 0 and any(
                    px > 0 and abs(px / ma20 - 1) > 0.40
                    for px in (open_px, high_px, low_px)
                ):
                    continue

                equity_now = self.cash + sum(p2.qty * day_closes.get(s2, p2.cost) for s2, p2 in self.positions.items())

                if stype == "SELL_STOP":
                    if pos:
                        self._sell(date, sym, name, open_px * (1 - SLIPPAGE), pos.qty, "sell_stop", sig.reason)
                elif stype == "SELL_REDUCE":
                    if pos:
                        qty = _sell_qty(max(1, int(pos.qty * REDUCE_RATIO)), pos.qty, sym)
                        if qty > 0:
                            self._sell(date, sym, name, open_px * (1 - SLIPPAGE), qty, "sell_reduce", sig.reason)
                elif stype == "BUY_FIRST":
                    if not gate_open:
                        continue
                    used_pct = sum(p2.qty * day_closes.get(s2, p2.cost) for s2, p2 in self.positions.items()) / equity_now if equity_now else 1.0
                    if used_pct >= max_total:
                        continue
                    plan_amount = min(equity_now * plan_ratio * reduced_ratio, self.cash * 0.99)
                    qty = _round_buy_qty(int(plan_amount / (open_px * (1 + SLIPPAGE))), sym)
                    if qty <= 0:
                        continue
                    self._buy(date, sym, name, open_px * (1 + SLIPPAGE), qty, "buy_first", sig.reason)
                    if sym in self.positions:
                        self.positions[sym].stage = 1
                elif stype == "BUY_ADD":
                    if not gate_add or pos is None:
                        continue
                    stage_idx = pos.stage
                    if stage_idx >= len(pyramid):
                        continue
                    plan_amount = min(equity_now * plan_ratio * pyramid[stage_idx] * reduced_ratio, self.cash * 0.99)
                    qty = _round_buy_qty(int(plan_amount / (open_px * (1 + SLIPPAGE))), sym)
                    if qty <= 0:
                        continue
                    self._buy(date, sym, name, open_px * (1 + SLIPPAGE), qty, "buy_add", sig.reason)
                    if sym in self.positions:
                        self.positions[sym].stage += 1

            # ---- 做T(独立于主信号): 前一日收盘布林轨判定(无前视), 当日盘中高/低价成交(用户确认口径)
            if t_cfg.get("enable", True):
                for sym in list(self.positions.keys()):
                    pos = self.positions.get(sym)
                    if pos is None or pos.qty <= 0:
                        continue
                    idx_now = pool[sym][2].get(date)
                    if idx_now is None or idx_now < 1:
                        continue
                    ind_sym = pool[sym][0]
                    ti = idx_now - 1  # 信号日(前一日收盘)
                    boll_u = _f(ind_sym["boll_upper20"].iloc[ti]) if "boll_upper20" in ind_sym.columns else 0.0
                    boll_l = _f(ind_sym["boll_lower20"].iloc[ti]) if "boll_lower20" in ind_sym.columns else 0.0
                    if boll_u <= 0 or boll_l <= 0:
                        continue
                    open_px = _f(ind_sym["open"].iloc[idx_now])
                    high_px = _f(ind_sym["high"].iloc[idx_now])
                    low_px = _f(ind_sym["low"].iloc[idx_now])
                    prev_close = _f(ind_sym["close"].iloc[ti])
                    swing = (high_px - low_px) / prev_close * 100 if prev_close > 0 else 0.0
                    if swing < float(t_cfg.get("min_swing_pct", 1.5)):
                        continue
                    limit = _limit_pct(sym)
                    up_limit = prev_close * (1 + limit) if prev_close > 0 else 1e18
                    down_limit = prev_close * (1 - limit) if prev_close > 0 else 0.0
                    ma20 = _f(ind_sym["ma20"].iloc[idx_now]) if "ma20" in ind_sym.columns else 0.0
                    if ma20 > 0 and any(
                        px > 0 and abs(px / ma20 - 1) > 0.40 for px in (open_px, high_px, low_px)
                    ):
                        continue  # 除权/脏数据日不参与做T
                    name = pool[sym][1]
                    t_ratio = float(t_cfg.get("t_position_ratio", 0.3))
                    min_qty = _min_unit(sym)
                    if high_px >= boll_u * 0.995:
                        if pos.qty <= min_qty:
                            continue  # 底仓不足最小单位, 不做T高抛
                        t_qty = max(1, int(pos.qty * t_ratio))
                        t_qty = min(t_qty, pos.qty - min_qty)  # 保留足额底仓(不产生碎股)
                        if t_qty <= 0:
                            continue
                        if open_px > down_limit and high_px > 0:
                            self._sell(date, sym, name, high_px * (1 - SLIPPAGE), t_qty, "t_sell", "日内冲布林上轨,做T高抛")
                            t_outstanding[sym] = t_outstanding.get(sym, 0) + t_qty
                    elif low_px <= boll_l * 1.005:
                        want = t_outstanding.get(sym, 0)
                        if want <= 0:
                            want = max(1, int(pos.qty * t_ratio))
                        t_qty = min(want, _round_buy_qty(int(self.cash / (low_px * (1 + SLIPPAGE))), sym))
                        if t_qty > 0 and open_px < up_limit:
                            self._buy(date, sym, name, low_px * (1 + SLIPPAGE), t_qty, "t_buy", "日内回踩布林下轨,做T低吸")
                            t_outstanding[sym] = max(0, t_outstanding.get(sym, 0) - t_qty)

            # ---- 第三步: 收盘结算净值 + 风控状态更新
            closes_final = {
                s: _f(pool[s][0]["close"].iloc[min(pool[s][2].get(date, 0), len(pool[s][0]) - 1)])
                for s in self.positions
            }
            eq = self.cash + sum(p2.qty * closes_final.get(s2, p2.cost) for s2, p2 in self.positions.items())
            self.equity_curve.append({"date": date, "equity": round(eq, 2)})
            prev_eq = self.equity_curve[-2]["equity"] if len(self.equity_curve) >= 2 else self.initial
            day_ret = (eq / prev_eq - 1) * 100 if prev_eq > 0 else 0.0
            if day_ret <= -daily_limit:
                self.fuse_date = date
            self.peak_equity = max(self.peak_equity, eq)
            if (self.peak_equity - eq) / self.peak_equity * 100 >= drawdown_limit:
                self.defense_mode = True

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
                "notes": "信号日T收盘判定->T+1开盘成交; 做T按当日最高/最低价近似(乐观口径); 已扣双边手续费; 风控三道闸门生效",
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
                          progress_cb: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """便捷入口."""
    return StrategyBacktest(initial_capital=initial_capital).run(symbols=symbols, progress_cb=progress_cb)
