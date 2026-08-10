"""仓位管理核心实现.

- 金字塔加仓: 按 pyramid_ratios 分批, 加仓价必须高于上次(顺向加), 每次重算成本
- 分批止盈: 触及 take_profit_levels 各档各减一部分
- 凯利公式: f = p - (1-p)/b, 乘 kelly_fraction 折扣
- 状态机: 空仓 -> 首仓 -> 加仓 -> 减仓 -> 清仓, 每次操作落 trades 表
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Any

from sqlmodel import Session, select

from app import db
from app.core.config import config_manager
from app.core.logger import trade_logger
from app.core.lot_rules import round_buy_qty
from app.models.models import Position, Trade


class PositionManagerError(Exception):
    pass


def _now() -> str:
    # 强制东八区(与 models._now 一致, 不依赖进程时区)
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _session(session: Session | None):
    """传入 session 则复用(不关闭), 否则新建并关闭."""
    if session is not None:
        yield session
    else:
        with db.session_scope() as s:
            yield s


class PositionManager:
    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------ 查询
    def list_positions(self, session: Session | None = None) -> list[Position]:
        with _session(session) as s:
            stmt = select(Position).where(Position.status == "holding").order_by(Position.symbol)
            return list(s.exec(stmt).all())

    def get_position(self, symbol: str, session: Session | None = None) -> Position | None:
        with _session(session) as s:
            stmt = select(Position).where(Position.symbol == symbol, Position.status == "holding")
            return s.exec(stmt).first()

    def history(self, symbol: str, limit: int = 50, session: Session | None = None) -> list[Trade]:
        with _session(session) as s:
            stmt = select(Trade).where(Trade.symbol == symbol).order_by(Trade.time.desc()).limit(limit)
            return list(s.exec(stmt).all())

    # ------------------------------------------------------------ 交易操作
    def open_or_add(self, symbol: str, name: str, qty: int, price: float, reason: str = "", session: Session | None = None) -> Position:
        """首仓或加仓. 加仓必须顺向(price >= 当前成交均价), 否则抛错.

        成本口径(对齐券商 APP): pos.cost 为**含费摊薄成本**, 买入手续费直接摊进成本;
        pos.cost_raw 为纯成交均价, 仅用于顺向加仓判断(避免手续费把判断线抬高).
        """
        if qty <= 0 or price <= 0:
            raise PositionManagerError("数量与价格必须为正")
        # A 股买入申报数量合规(与交易计划/回测同一套规则): 科创板≥200, 北交所≥100, 主板100整数倍
        if round_buy_qty(qty, symbol) != qty:
            raise PositionManagerError(
                f"{symbol} 买入申报数量 {qty} 不合规: 科创板须≥200股(1股递增), "
                "北交所须≥100股(1股递增), 主板/创业板须为100的整数倍"
            )
        with _session(session) as s:
            pos = self.get_position(symbol, s)
            first_open = pos is None
            if pos is None:
                # 首仓: 记录持仓时间(加仓不刷新, 用于 T+1 判定); 档位置 0(仅首仓)
                pos = Position(symbol=symbol, name=name, qty=0, cost=0.0, cost_raw=0.0,
                               status="holding", opened_at=_now(), pyramid_stage=0)
                s.add(pos)
            else:
                # 顺向判断用纯均价: 含费成本天然高于成交价, 用它会误杀平价加仓
                ref = pos.cost_raw or pos.cost
                if price < ref:
                    raise PositionManagerError(f"加仓价 {price:.2f} 低于当前成本 {ref:.2f}, 拒绝顺向加仓")
                # 显式推进金字塔档位(取代旧「按成交笔数倒推」的脆弱逻辑)
                pos.pyramid_stage = (pos.pyramid_stage or 0) + 1
            # 先落成交, 以 trade.fee 为手续费唯一真相源(避免与日志层重复计算而口径漂移)
            trade = trade_logger.record(symbol, name, "buy", price, qty, reason, note="仓位管理", session=s)
            amount = trade.amount
            fee = trade.fee or 0.0

            new_qty = pos.qty + qty
            old_raw = pos.cost_raw or pos.cost or 0.0
            old_incl = pos.cost or old_raw
            # 含费成本: 历史含费市值 + 本次成交额 + 本次买入手续费
            pos.cost = round((old_incl * pos.qty + amount + fee) / new_qty, 4)
            pos.cost_raw = round((old_raw * pos.qty + amount) / new_qty, 4)
            pos.qty = new_qty
            pos.name = name or pos.name
            pos.updated_at = _now()
            s.commit()
            s.refresh(pos)
            return pos

    def reduce(self, symbol: str, qty: int, price: float, reason: str = "", session: Session | None = None) -> float:
        """减仓. 返回已实现盈亏(已扣**双边**手续费净额); 减到 0 自动清仓.

        pos.cost 已含买入手续费 -> (price - cost) * qty 天然扣掉了买入侧费用;
        record() 再扣卖出侧费用(含印花税), 最终 trade.pnl 即完整含费净额.
        减仓不改变每股成本(加权平均法), 剩余仓位继续沿用原含费成本.

        T+1 限制: 当日买入的持仓当日不可减仓/卖出(与 A 股 T+1 规则一致).
        """
        if qty <= 0 or price <= 0:
            raise PositionManagerError("数量与价格必须为正")
        with _session(session) as s:
            pos = self.get_position(symbol, s)
            if pos is None or pos.qty <= 0:
                raise PositionManagerError(f"{symbol} 无持仓")
            if self.is_t_plus_one_locked(symbol, s):
                raise PositionManagerError(
                    f"{symbol} 处于 T+1 锁定期(今日买入), 当日不可减仓/卖出, 下一交易日方可操作"
                )
            if qty > pos.qty:
                qty = pos.qty
            realized_pnl = round((price - pos.cost) * qty, 2)
            pos.qty -= qty
            if pos.qty == 0:
                pos.status = "closed"
                pos.pyramid_stage = 0  # 清仓后档位归零, 重开即首仓
            pos.updated_at = _now()
            # 交易日志双写(SQLite + CSV); record 内部扣卖出手续费, trade.pnl 为净额
            trade = trade_logger.record(symbol, pos.name, "sell", price, qty, reason,
                                        pnl=realized_pnl, note="仓位管理", session=s)
            s.commit()
            return trade.pnl if trade.pnl is not None else realized_pnl

    def is_t_plus_one_locked(self, symbol: str, session: Session | None = None) -> bool:
        """持仓是否处于 T+1 锁定期: 当日买入, 当日不可卖出/减仓.

        以持仓时间(opened_at)的日期与当前日期比较; 无持仓或时间为空视为非锁定.
        """
        with _session(session) as s:
            pos = self.get_position(symbol, s)
            if pos is None:
                return False
            opened = (pos.opened_at or "").strip()
            if not opened:
                return False
            try:
                opened_date = opened[:10]
                today = dt.datetime.now().strftime("%Y-%m-%d")
                return opened_date == today
            except Exception:  # noqa: BLE001
                return False

    def close(self, symbol: str, price: float, reason: str = "清仓", session: Session | None = None) -> float:
        """清仓, 返回已实现盈亏."""
        with _session(session) as s:
            pos = self.get_position(symbol, s)
            if pos is None or pos.qty <= 0:
                raise PositionManagerError(f"{symbol} 无持仓")
            return self.reduce(symbol, pos.qty, price, reason, s)

    # ------------------------------------------------------------ 仓位建议
    def pyramid_plan(self, symbol: str, session: Session | None = None,
                     kline_df: Any | None = None) -> dict[str, Any]:
        """金字塔加仓建议: 已用档位/剩余档位/建议比例.

        pyramid_stage = 已完成的分档数(0=仅首仓, 1=已加1档, 2=已加2档),
        由 open_or_add 显式维护, 不再从成交笔数倒推(避免同日多笔/减后回补误判).
        ratios[0] 即首仓本身, 因此「下一档加仓」对应索引 = pyramid_stage + 1.

        比率默认取全局「仓位.pyramid_ratios」; 当交易模式(Q2)启用且提供 K 线时,
        取当前模式(mode.pyramid_ratios) —— 不同市况下首仓/加仓比例自动切换.
        """
        cfg = config_manager.get()
        pos = self.get_position(symbol, session)
        stage = pos.pyramid_stage if pos is not None else 0
        # 选比率: 模式启用 + 有 K 线 -> 当前模式的金字塔比例
        ratios = cfg["仓位"]["pyramid_ratios"]
        mode_key = ""
        if cfg.get("交易模式", {}).get("enabled", True) and kline_df is not None:
            try:
                from app.core.modes import active_mode
                md = active_mode(symbol, kline_df, cfg)
                if md.mode.get("pyramid_ratios"):
                    ratios = md.mode["pyramid_ratios"]
                    mode_key = md.mode_key
            except Exception:  # noqa: BLE001
                pass
        next_idx = stage + 1  # 下一档加仓在 ratios 中的索引(首仓 ratios[0] 已占用)
        remaining = ratios[next_idx:] if next_idx < len(ratios) else []
        return {
            "strategy": "pyramid",
            "ratios": ratios,
            "mode": mode_key,
            "used_stage": stage,
            "next_stage_index": next_idx,
            "next_stage_exhausted": next_idx >= len(ratios),
            "remaining_ratios": remaining,
            "suggest_next_pct": remaining[0] * 100 if remaining else 0.0,
        }

    def take_profit_levels(self, cost: float, atr_pct: float | None = None,
                           session: Session | None = None,
                           mode: dict[str, Any] | None = None) -> list[dict[str, float]]:
        """分批止盈计划: 每档触发价与建议减仓比例.

        atr_pct 给定 -> ATR 动态档(成本 × (1+倍数×ATR), 带下限保护);
        atr_pct 为空 -> 使用配置中的 fixed 档位(向后兼容).
        mode 给定(当前交易模式) -> 优先用模式的止盈配置, 否则用全局「仓位」配置.
        """
        cfg = config_manager.get()
        pc = mode or cfg["仓位"]
        ratios = pc.get("take_profit_ratios", [0.2, 0.3, 0.5])
        mode = pc.get("take_profit_mode", "atr")
        if mode == "atr" and atr_pct is not None:
            min_pct = pc.get("min_tp_pct", 3.0) / 100.0
            levels = [1 + max(m * atr_pct, min_pct) for m in pc.get("atr_multipliers", [1.5, 3.0, 5.0])]
        else:
            levels = list(pc["take_profit_levels"])
        out = []
        for i, lv in enumerate(levels):
            out.append({
                "level": i + 1,
                "target_price": round(cost * lv, 2),
                "target_pct": round((lv - 1) * 100, 1),
                "suggest_reduce_ratio": ratios[i] if i < len(ratios) else 0.3,
            })
        return out

    def kelly(self, win_rate: float, avg_win: float, avg_loss: float) -> float:
        """凯利公式(折扣后): f = p - (1-p)/b, b = avg_win/avg_loss."""
        cfg = config_manager.get()
        if avg_loss <= 0 or avg_win <= 0:
            return 0.0
        p = max(0.01, min(0.99, win_rate))
        b = avg_win / avg_loss
        f = p - (1 - p) / b
        f = max(0.0, f) * cfg["仓位"]["kelly_fraction"]
        return round(f, 4)

    # ------------------------------------------------------------ 持仓汇总
    def portfolio(self, prices: dict[str, float], session: Session | None = None) -> dict[str, Any]:
        """组合汇总: 市值/浮盈/浮盈率(用于风控与计划).

        cost 为含费摊薄成本, 因此 unrealized_pnl 已扣掉买入手续费(与券商 APP 一致);
        另返回 fee_cost = 摊在当前持仓上的买入费用, 便于前端说明口径.

        realized_pnl 为历史卖出净额合计(trade.pnl, 已扣双边手续费),
        供前端算总权益 = 启动资金 + 已实现盈亏 + 浮动盈亏.
        """
        positions = self.list_positions(session)
        market_value = 0.0
        cost_value = 0.0
        cost_raw_value = 0.0
        fee_cost_total = 0.0
        unrealized = 0.0
        items = []
        for p in positions:
            price = prices.get(p.symbol, p.cost)
            cost_raw = p.cost_raw or p.cost
            mv = price * p.qty
            cv = p.cost * p.qty
            crv = cost_raw * p.qty
            fee_cost = cv - crv  # 已摊入当前持仓的买入手续费
            pnl = mv - cv
            market_value += mv
            cost_value += cv
            cost_raw_value += crv
            fee_cost_total += fee_cost
            unrealized += pnl
            items.append({
                "symbol": p.symbol, "name": p.name, "qty": p.qty, "cost": p.cost,
                "cost_raw": round(cost_raw, 4), "fee_cost": round(fee_cost, 2),
                "opened_at": p.opened_at or "",
                "price": round(price, 3), "market_value": round(mv, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pct": round(pnl / cv * 100, 2) if cv else 0.0,
            })
        # 已实现盈亏: 历史卖出净额合计(trade.pnl 已扣双边手续费), 供前端总权益计算
        realized = 0.0
        with _session(session) as s:
            for t in s.exec(select(Trade).where(Trade.action == "sell")).all():
                realized += t.pnl or 0.0
        return {
            "positions": items,
            "market_value": round(market_value, 2),
            "cost_value": round(cost_value, 2),
            "cost_raw_value": round(cost_raw_value, 2),
            "fee_cost": round(fee_cost_total, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pct": round(unrealized / cost_value * 100, 2) if cost_value else 0.0,
        }
