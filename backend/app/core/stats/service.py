"""历史回顾统计 + 交易评分.

数据口径:
- 平仓盈亏 = trades 表中 action=sell 且 pnl != 0 的记录(已完成回合)
- 信号分布来自 signal_records 表
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlmodel import Session, select

from app import db
from app.models.models import SignalRecord, Trade


class StatsService:
    # ------------------------------------------------------------ 汇总统计
    def summary(self, session: Session | None = None) -> dict[str, Any]:
        with session or db.session_scope() as s:
            closed = [t for t in s.exec(select(Trade).where(Trade.action == "sell")).all() if t.pnl]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]
        n = len(closed)
        total_pnl = sum(t.pnl for t in closed)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        win_rate = len(wins) / n if n else 0.0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else (gross_win if n else 0.0)
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        expect = total_pnl / n if n else 0.0
        max_win = max((t.pnl for t in wins), default=0.0)
        max_loss = min((t.pnl for t in losses), default=0.0)
        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expect, 2),
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
            "max_consecutive_losses": self._max_consecutive_losses(closed),
        }

    @staticmethod
    def _max_consecutive_losses(closed: list[Trade]) -> int:
        streak = best = 0
        for t in closed:
            if t.pnl < 0:
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        return best

    # ------------------------------------------------------------ 盈亏曲线
    def equity_curve(self, session: Session | None = None) -> list[dict[str, Any]]:
        """按时间累计已实现盈亏(每个平仓点 + 起始点)."""
        with session or db.session_scope() as s:
            closed = [t for t in s.exec(select(Trade).where(Trade.action == "sell")).all() if t.pnl]
        closed.sort(key=lambda t: t.time)
        curve: list[dict[str, Any]] = [{"time": "start", "equity": 0.0, "pnl": 0.0}]
        acc = 0.0
        for t in closed:
            acc += t.pnl
            curve.append({"time": t.time, "equity": round(acc, 2), "pnl": round(t.pnl, 2),
                          "symbol": t.symbol, "name": t.name})
        return curve

    # ------------------------------------------------------------ 月度热力图
    def monthly_heatmap(self, session: Session | None = None) -> dict[str, Any]:
        """按月聚合: 每月盈亏 + 笔数 + 胜率."""
        with session or db.session_scope() as s:
            closed = [t for t in s.exec(select(Trade).where(Trade.action == "sell")).all() if t.pnl]
        months: dict[str, dict[str, float]] = {}
        for t in closed:
            key = t.time[:7]  # YYYY-MM
            m = months.setdefault(key, {"pnl": 0.0, "trades": 0, "wins": 0})
            m["pnl"] += t.pnl
            m["trades"] += 1
            if t.pnl > 0:
                m["wins"] += 1
        rows = []
        for key in sorted(months):
            m = months[key]
            rows.append({
                "month": key,
                "pnl": round(m["pnl"], 2),
                "trades": int(m["trades"]),
                "win_rate": round(m["wins"] / m["trades"] * 100, 0) if m["trades"] else 0,
            })
        return {"months": rows}

    # ------------------------------------------------------------ 信号分布
    def signal_distribution(self, session: Session | None = None) -> list[dict[str, Any]]:
        with session or db.session_scope() as s:
            rows = s.exec(select(SignalRecord)).all()
        counter = Counter(r.type for r in rows)
        return [{"type": t, "count": counter.get(t, 0)} for t in
                ("BUY_FIRST", "BUY_ADD", "SELL_REDUCE", "SELL_STOP", "T_BUY", "T_SELL")]

    # ------------------------------------------------------------ 交易评分
    def trade_scores(self, session: Session | None = None) -> dict[str, Any]:
        """单笔评分 + 健康度评分(方案 §4.9)."""
        with session or db.session_scope() as s:
            trades = list(s.exec(select(Trade).order_by(Trade.time.asc())).all())
            closed = [t for t in trades if t.action == "sell" and t.pnl]
        items = []
        for t in closed:
            pnl_pct = self._pnl_pct(t)
            pnl_score = 50 + pnl_pct * 8 if pnl_pct >= 0 else max(0.0, 50 + pnl_pct * 12)
            # 执行分: 亏损单若超过止损线(默认5%)则扣分
            execute_score = 100.0
            if t.pnl < 0 and pnl_pct < -5.0:
                execute_score = 60.0  # 超止损线, 纪律扣分
            elif t.pnl < 0:
                execute_score = 85.0  # 止损内亏损, 纪律合格
            score = round(0.6 * pnl_score + 0.4 * execute_score, 1)
            score = min(100.0, score)  # 上限 100
            items.append({
                "id": t.id, "time": t.time, "symbol": t.symbol, "name": t.name,
                "pnl": round(t.pnl, 2), "pnl_pct": round(pnl_pct, 2), "score": score,
                "comment": "盈利单" if t.pnl > 0 else ("止损执行良好" if pnl_pct >= -5 else "超止损线, 纪律差"),
            })
        health = self._health_score(closed)
        return {"items": items, "health": health}

    @staticmethod
    def _pnl_pct(t: Trade) -> float:
        if t.price <= 0 or t.qty <= 0 or not t.pnl:
            return 0.0
        return t.pnl / (t.price * t.qty) * 100

    @staticmethod
    def _health_score(closed: list[Trade]) -> int:
        """健康度 0-100: 胜率30 + 盈亏比30 + 连亏控制20 + 纪律20. 无平仓交易返回 0(暂无数据)."""
        if not closed:
            return 0
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]
        wr = len(wins) / len(closed)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = gross_win / gross_loss if gross_loss else (gross_win if closed else 0)

        s_win = min(30.0, wr * 30 / 0.7 * 0.7) if wr >= 0.5 else wr * 30 / 0.5
        s_pf = min(30.0, pf / 2.0 * 30)
        # 连亏控制: 1-2 连亏满分, 3 -> 15, 4+ -> 5
        max_streak = 0
        streak = 0
        for t in closed:
            streak = streak + 1 if t.pnl < 0 else 0
            max_streak = max(max_streak, streak)
        s_streak = 20 if max_streak <= 2 else (15 if max_streak == 3 else 5)
        # 纪律: 亏损单中超过止损线(5%)的比例
        over = sum(1 for t in losses if t.pnl / (t.price * t.qty) * 100 < -5) if losses else 0
        s_discipline = 20 if not losses else round(20 * (1 - over / len(losses)), 1)
        return int(round(s_win + s_pf + s_streak + s_discipline, 0))
