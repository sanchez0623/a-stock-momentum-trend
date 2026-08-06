"""风控模块核心实现.

- 日亏损熔断: 当日浮动+实现亏损 >= daily_loss_limit_pct -> 停止买入(仅允许减仓/止损)
- 连续亏损降仓: 连亏 >= consecutive_loss_limit -> 新仓位上限降至 50%
- 回撤防守: 组合回撤 >= max_drawdown_pct -> 防守模式, 仓位上限砍半
- 单票/总仓位硬性上限
- 状态持久化 risk_state 表, 重启不丢, 需手动 reset
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Any

from sqlmodel import Session

from app import db
from app.core.config import config_manager
from app.models.models import RiskState


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _session(session: Session | None):
    if session is not None:
        yield session
    else:
        with db.session_scope() as s:
            yield s


class RiskManager:
    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------ 状态读写
    def _load(self, session: Session) -> RiskState:
        row = session.get(RiskState, 1)
        if row is None:
            row = RiskState(id=1)
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def status(self, session: Session | None = None) -> dict[str, Any]:
        cfg = config_manager.get()
        risk = cfg["风控"]
        with _session(session) as s:
            st = self._load(s)
            return {
                "day_loss_tripped": st.day_loss_tripped,
                "defense_mode": st.defense_mode,
                "consecutive_losses": st.consecutive_losses,
                "day_pnl": st.day_pnl,
                "last_trade_pnl": st.last_trade_pnl,
                "updated_at": st.updated_at,
                "config": {
                    "daily_loss_limit_pct": risk["daily_loss_limit_pct"],
                    "consecutive_loss_limit": risk["consecutive_loss_limit"],
                    "max_drawdown_pct": risk["max_drawdown_pct"],
                    "single_position_pct": risk["single_position_pct"],
                    "total_position_pct": risk["total_position_pct"],
                },
                "position_multiplier": 0.5 if (st.defense_mode or st.consecutive_losses >= risk["consecutive_loss_limit"]) else 1.0,
            }

    def reset(self, session: Session | None = None) -> dict[str, Any]:
        """重置熔断/防守/连亏(需人工确认)."""
        with _session(session) as s:
            st = self._load(s)
            st.day_loss_tripped = False
            st.defense_mode = False
            st.consecutive_losses = 0
            st.day_pnl = 0.0
            st.updated_at = _now()
            s.add(st)
            s.commit()
        return self.status()

    # ------------------------------------------------------------ 闸门检查
    def check_entry(
        self,
        symbol: str,
        suggest_pct: float,
        portfolio: dict[str, Any],
        session: Session | None = None,
    ) -> tuple[bool, list[str], float]:
        """买入闸门. 返回 (是否放行, 拦截原因列表, 实际允许仓位%).."""
        cfg = config_manager.get()
        risk = cfg["风控"]
        reasons: list[str] = []
        with _session(session) as s:
            st = self._load(s)
            if st.day_loss_tripped:
                reasons.append("⚠ 日亏损熔断中: 禁止买入, 仅允许减仓/止损")
            if st.defense_mode:
                reasons.append("⚠ 回撤防守模式: 仓位上限砍半")
            if st.consecutive_losses >= risk["consecutive_loss_limit"]:
                reasons.append(f"⚠ 连续亏损 {st.consecutive_losses} 笔: 新仓位上限降至 50%")

            total_pct = risk["total_position_pct"]
            single_pct = risk["single_position_pct"]
            if st.defense_mode or st.consecutive_losses >= risk["consecutive_loss_limit"]:
                total_pct *= 0.5
                single_pct *= 0.5

            # 单票限制
            if suggest_pct > single_pct:
                reasons.append(f"建议仓位 {suggest_pct:.0f}% 超过单票上限 {single_pct:.0f}%")
                suggest_pct = single_pct
            # 总仓位限制
            current_total = portfolio.get("total_pct", 0.0)
            if current_total + suggest_pct > total_pct:
                suggest_pct = max(0.0, total_pct - current_total)
                reasons.append(f"总仓位将超限({current_total:.0f}%+{suggest_pct:.0f}% > {total_pct:.0f}%)")
                if suggest_pct <= 0:
                    reasons.append("总仓位已满, 拒绝买入")

        allowed = st.day_loss_tripped is False and suggest_pct > 0
        return allowed, reasons, round(suggest_pct, 2)

    # ------------------------------------------------------------ 状态更新
    def record_trade_result(self, pnl: float, session: Session | None = None) -> None:
        """一笔交易平仓后记录盈亏, 更新连亏计数与当日盈亏."""
        with _session(session) as s:
            st = self._load(s)
            st.last_trade_pnl = round(pnl, 2)
            if pnl < 0:
                st.consecutive_losses += 1
            else:
                st.consecutive_losses = 0
            st.day_pnl = round(st.day_pnl + pnl, 2)
            st.updated_at = _now()
            s.add(st)
            s.commit()

    def update_day_pnl(
        self,
        realized_pnl: float,
        unrealized_pnl: float,
        base_cost: float = 0.0,
        session: Session | None = None,
    ) -> bool:
        """更新当日盈亏并检查日亏损熔断. 返回是否触发熔断."""
        cfg = config_manager.get()
        limit = cfg["风控"]["daily_loss_limit_pct"]
        day_pnl = realized_pnl + unrealized_pnl
        with _session(session) as s:
            st = self._load(s)
            st.day_pnl = round(day_pnl, 2)
            st.updated_at = _now()
            if base_cost > 0 and day_pnl < 0 and -day_pnl / base_cost * 100 >= limit:
                st.day_loss_tripped = True
            s.add(st)
            s.commit()
            return st.day_loss_tripped

    def update_drawdown(self, peak_equity: float, equity: float, session: Session | None = None) -> bool:
        """回撤检查: 触发防守模式返回 True."""
        cfg = config_manager.get()
        limit = cfg["风控"]["max_drawdown_pct"]
        if peak_equity <= 0:
            return False
        dd = (peak_equity - equity) / peak_equity * 100
        if dd >= limit:
            with _session(session) as s:
                st = self._load(s)
                st.defense_mode = True
                st.updated_at = _now()
                s.add(st)
                s.commit()
            return True
        return False
