"""信号审计回测(方案 v2 §6): 真实成交 vs 纪律曲线, 量化「计划 vs 执行」偏差.

目的: 回答「我执行得怎么样」——同一持仓起点, 若严格按信号引擎当日判定执行,
收益会差多少? 逐笔标注 实际动作 vs 当时系统建议, 偏差分三类:
- 违背: 系统建议卖出/买入, 实际未执行(持有或不动)
- 滞后: 建议后 N 日才执行(记录 N 与价差)
- 提前: 未到信号即行动(抢跑/恐慌)

口径:
- 真实线: Trade 表实际成交(实际价格/费用)逐日回放, 每票独立账户(初始资金=首笔买入金额)
- 纪律线: 同一票同一起点, 信号引擎逐日判定(T-1 收盘) -> T+1 开盘成交,
  仅含 首仓/加仓/止损/减仓(skip_t=True); 做T 需盘中价, 审计口径保守不纳入(报告标注)
- 组合: 各票收益率等权平均(避免初始资金分配假设)
- 数据: backtest_data 前复权冻结快照(与持仓回测同一通道)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.core.backtest.data import backtest_data
from app.core.config import config_manager
from app.core.fees import compute_trade_fee
from app.core.indicators import compute_all
from app.core.lot_rules import round_buy_qty as _round_buy_qty
from app.core.lot_rules import sell_qty as _sell_qty
from app.core.signals.engine import PositionInfo, Signal, SignalEngine

logger = logging.getLogger(__name__)

MIN_BARS = 60
SLIPPAGE = 0.001
REDUCE_RATIO = 1 / 3

ADVICE_BUY = "BUY_FIRST"        # 建议买入(空仓)
ADVICE_ADD = "BUY_ADD"          # 建议加仓
ADVICE_REDUCE = "SELL_REDUCE"   # 建议减仓
ADVICE_STOP = "SELL_STOP"       # 建议止损清仓


@dataclass
class RealTrade:
    """真实成交(Trade 表)."""

    date: str
    symbol: str
    name: str
    action: str          # buy / sell
    price: float
    qty: int
    fee: float


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def _load_real_trades(symbols: list[str] | None = None) -> list[RealTrade]:
    """Trade 表真实成交(时间升序)."""
    from sqlmodel import select

    from app import db
    from app.models.models import Trade

    with db.session_scope() as s:
        stmt = select(Trade).order_by(Trade.time.asc(), Trade.id.asc())
        if symbols:
            stmt = stmt.where(Trade.symbol.in_(symbols))
        rows = s.exec(stmt).all()
    return [RealTrade(
        date=(t.time or "")[:10], symbol=t.symbol, name=t.name or "",
        action=t.action or "buy", price=float(t.price or 0.0),
        qty=int(t.qty or 0), fee=float(t.fee or 0.0),
    ) for t in rows if t.qty and t.price]


def run_audit(symbols: list[str] | None = None, start: str = "", end: str = "") -> dict[str, Any]:
    """运行信号审计. symbols: 空=全部有成交的票. 返回 {meta, curves, by_symbol, stats, audits}."""
    trades = _load_real_trades(symbols)
    if not trades:
        return {"error": "无真实成交记录可审计"}

    # ---- 数据准备: 冻结快照 + 指标(逐股一次)
    by_sym: dict[str, list[RealTrade]] = {}
    for t in trades:
        by_sym.setdefault(t.symbol, []).append(t)
    cfg = config_manager.get()

    if not end:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")
    pool: dict[str, tuple[pd.DataFrame, dict[str, int], list[RealTrade]]] = {}
    for sym, sym_trades in by_sym.items():
        first_date = sym_trades[0].date
        if not start:
            s0 = (pd.Timestamp(first_date) - pd.Timedelta(days=int(MIN_BARS * 1.6) + 40)).strftime("%Y-%m-%d")
        else:
            s0 = start
        r = backtest_data.ensure_range(sym, s0, end)
        if r.get("source") == "none" or r.get("row_count", 0) == 0:
            logger.info("审计: %s 无行情数据, 跳过", sym)
            continue
        df = pd.DataFrame(r.get("rows", []))
        for c in ("open", "high", "low", "close", "volume", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df) < 2:
            continue
        ind = compute_all(
            df,
            ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
            macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
            rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
            volume_ma=cfg["量能"]["volume_ma"],
        )
        di = {str(d)[:10]: i for i, d in enumerate(ind["date"])}
        pool[sym] = (ind, di, sym_trades)
    if not pool:
        return {"error": "全部成交票均无可用行情数据"}

    # ---- 时间轴: 全部票交易日并集
    all_dates = sorted(set().union(*(di.keys() for _, di, _ in pool.values())))

    # ---- 真实线 & 纪律线(每票独立账户)
    real_curves: dict[str, tuple[list[float], float]] = {}
    disc_curves: dict[str, tuple[list[float], float]] = {}
    audits: list[dict[str, Any]] = []
    fee_cfg = cfg.get("手续费", {})
    risk = cfg["风控"]
    position_cfg = cfg["仓位"]
    plan_ratio = float(risk["single_position_pct"]) / 100.0
    pyramid = list(position_cfg.get("pyramid_ratios", [0.5, 0.3, 0.2]))

    for sym, (ind, di, sym_trades) in pool.items():
        # ---- 真实线: 逐日回放实际成交(净资产口径: 现金 + 市值)
        r_cash = 0.0
        r_qty = 0
        r_invested = 0.0       # 累计买入金额(收益率基数)
        r_curve: list[float] = []
        real_actions: dict[str, str] = {}   # date -> 实际动作(buy/sell)
        t_by_date: dict[str, list[RealTrade]] = {}
        for t in sym_trades:
            t_by_date.setdefault(t.date, []).append(t)
        for date in all_dates:
            for t in t_by_date.get(date, []):
                if t.action == "buy":
                    r_cash -= t.price * t.qty + t.fee
                    r_qty += t.qty
                    r_invested += t.price * t.qty + t.fee
                    real_actions[date] = "buy"
                else:
                    r_cash += t.price * t.qty - t.fee
                    r_qty = max(0, r_qty - t.qty)
                    real_actions[date] = "sell"
            close = _f(ind["close"].iloc[di[date]]) if date in di else 0.0
            r_curve.append(r_cash + r_qty * close)
        real_curves[sym] = (r_curve, r_invested)

        # ---- 纪律线: 信号引擎逐日判定(T-1 收盘 -> T 开盘成交)
        d_cash = 0.0
        d_qty = 0
        d_cost = 0.0
        d_peak = 0.0
        d_stage = 0
        d_invested = 0.0      # 首仓投入(收益率基数, 与真实线首笔投入可比)
        d_curve: list[float] = []
        sym_advice: dict[str, str] = {}
        engine = SignalEngine()
        for _day_idx, date in enumerate(all_dates):
            i = di.get(date)
            advice = ""
            if i is not None and i >= MIN_BARS + 1:
                ti = i - 1
                if d_qty > 0:
                    p_info = PositionInfo(symbol=sym, cost=d_cost, qty=d_qty, peak_price=d_peak)
                    sig: Signal | None = engine.evaluate_with_ind(
                        sym, ind=ind, position=p_info,
                        quote_price=None, quote_high=None, quote_low=None, end=ti, skip_t=True,
                    )
                    stype = sig.type if sig else ""
                    open_px = _f(ind["open"].iloc[i])
                    # 卖出类: 止损/减仓
                    if stype == "SELL_STOP":
                        qty = d_qty
                        advice = ADVICE_STOP
                    elif stype == "SELL_REDUCE":
                        qty = _sell_qty(max(1, int(d_qty * REDUCE_RATIO)), d_qty, sym)
                        advice = ADVICE_REDUCE if qty > 0 else ""
                    else:
                        qty = 0
                    if advice and qty > 0 and open_px > 0:
                        d_cash += open_px * (1 - SLIPPAGE) * qty - compute_trade_fee("sell", open_px * qty, fee_cfg)
                        d_qty -= qty
                    # 加仓: 卖出类之外
                    if not advice and stype == "BUY_ADD" and d_stage < len(pyramid):
                        plan_amount = (d_cash + d_qty * _f(ind["close"].iloc[i])) * plan_ratio * pyramid[d_stage]
                        qty = _round_buy_qty(int(plan_amount / (open_px * (1 + SLIPPAGE))), sym)
                        if qty > 0 and d_cash >= open_px * qty * 1.01:
                            d_cash -= open_px * (1 + SLIPPAGE) * qty + compute_trade_fee("buy", open_px * qty, fee_cfg)
                            total = d_cost * d_qty + open_px * qty
                            d_qty += qty
                            d_cost = total / d_qty if d_qty else 0.0
                            d_stage += 1
                            advice = ADVICE_ADD
                else:
                    # 空仓: 首仓信号 -> 开盘买入(基准资金=真实首笔买入金额)
                    sig = engine.evaluate_with_ind(
                        sym, ind=ind, position=PositionInfo(symbol=sym),
                        quote_price=None, quote_high=None, quote_low=None, end=ti, skip_t=True,
                    )
                    if sig is not None and sig.type == "BUY_FIRST":
                        base = sym_trades[0].price * sym_trades[0].qty
                        open_px = _f(ind["open"].iloc[i])
                        qty = _round_buy_qty(int(base / (open_px * (1 + SLIPPAGE))), sym)
                        if qty > 0:
                            d_cash = -open_px * (1 + SLIPPAGE) * qty - compute_trade_fee("buy", open_px * qty, fee_cfg)
                            d_qty = qty
                            d_cost = open_px
                            d_peak = open_px
                            d_stage = 1
                            d_invested = base
                            advice = ADVICE_BUY
            # 收盘结算 + 峰值更新
            close = _f(ind["close"].iloc[di[date]]) if date in di else 0.0
            if close > 0 and d_qty > 0:
                d_peak = max(d_peak, close)
            d_curve.append(d_cash + d_qty * close)
            if advice:
                sym_advice[date] = advice
        disc_curves[sym] = (d_curve, d_invested)

        # ---- 逐笔审计: 真实动作 vs 纪律建议
        for date in all_dates:
            advice = sym_advice.get(date, "")
            real = real_actions.get(date, "")
            if not real and not advice:
                continue
            idx = all_dates.index(date)
            if real == "sell":
                # 实际卖出: 前 5 日纪律是否有卖出建议
                prev_adv = [sym_advice.get(all_dates[j], "") for j in range(max(0, idx - 5), idx)]
                if advice in (ADVICE_STOP, ADVICE_REDUCE):
                    audits.append(_audit_row(date, sym, "卖出", advice, "一致"))
                elif any(a in (ADVICE_STOP, ADVICE_REDUCE) for a in prev_adv):
                    audits.append(_audit_row(date, sym, "卖出", advice or "卖出(迟)", "滞后"))
                else:
                    audits.append(_audit_row(date, sym, "卖出", "无建议", "提前"))
            elif real == "buy":
                prev_adv = [sym_advice.get(all_dates[j], "") for j in range(max(0, idx - 5), idx)]
                if advice in (ADVICE_BUY, ADVICE_ADD):
                    audits.append(_audit_row(date, sym, "买入", advice, "一致"))
                elif any(a in (ADVICE_BUY, ADVICE_ADD) for a in prev_adv):
                    audits.append(_audit_row(date, sym, "买入", advice or "买入(迟)", "滞后"))
                else:
                    audits.append(_audit_row(date, sym, "买入", "无建议", "提前"))
            else:
                # 实际无动作但有建议 -> 违背
                audits.append(_audit_row(date, sym, "无动作", advice, "违背"))

    # ---- 组合: 各票收益率等权平均
    # 收益率口径: 净资产(现金+市值) / 累计投入 - 1(买入日净资产≈0 -> 收益率≈0)
    def _norm(curve: list[float], invested: float) -> list[float]:
        base = invested or 1.0
        return [v / base - 1 for v in curve]

    n = len(all_dates)
    real_avg = [0.0] * n
    disc_avg = [0.0] * n
    by_symbol = []
    for sym in pool:
        rn = _norm(*real_curves[sym])
        dn = _norm(*disc_curves[sym])
        for i in range(n):
            real_avg[i] += rn[i] / len(pool)
            disc_avg[i] += dn[i] / len(pool)
        by_symbol.append({
            "symbol": sym,
            "name": pool[sym][2][0].name,
            "real_return_pct": round(rn[-1] * 100, 2),
            "discipline_return_pct": round(dn[-1] * 100, 2),
            "gap_pct": round((rn[-1] - dn[-1]) * 100, 2),
        })

    dev_kinds = [a["deviation"] for a in audits]
    stats = {
        "gap_total_pct": round((real_avg[-1] - disc_avg[-1]) * 100, 2),
        "audit_count": len(audits),
        "agree": dev_kinds.count("一致"),
        "violate": dev_kinds.count("违背"),
        "lag": dev_kinds.count("滞后"),
        "early": dev_kinds.count("提前"),
    }
    return {
        "meta": {
            "symbols": len(pool),
            "days": n,
            "start": all_dates[0], "end": all_dates[-1],
            "notes": "纪律线: 信号引擎 T-1 收盘判定 -> T+1 开盘成交(首仓/加仓/止损/减仓); "
                     "做T 不纳入(需盘中价, 口径保守); 数据为前复权冻结快照; 组合=各票等权",
        },
        "curves": {
            "real": [{"date": d, "equity": round(v * 100, 2)} for d, v in zip(all_dates, real_avg, strict=False)],
            "discipline": [{"date": d, "equity": round(v * 100, 2)} for d, v in zip(all_dates, disc_avg, strict=False)],
        },
        "by_symbol": by_symbol,
        "stats": stats,
        "audits": audits,
    }


def _audit_row(date: str, symbol: str, real: str, advice: str, deviation: str) -> dict[str, Any]:
    return {"date": date, "symbol": symbol, "real_action": real, "advice": advice or "-",
            "deviation": deviation}


# ---------------------------------------------------------------- 便捷入口
def run_signal_audit(symbols: list[str] | None = None, start: str = "", end: str = "") -> dict[str, Any]:
    return run_audit(symbols=symbols, start=start, end=end)
