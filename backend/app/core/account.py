"""资金账户核心: 启动资金(可编辑).

- 默认启动资金 50w(500000), 可在前端修改.
- 可用资金 / 总权益为前端派生值, 不在后端落库:
    可用资金 = 启动资金 + 已实现盈亏 - 持仓成本(含费)
    总权益   = 可用资金 + 持仓市值 = 启动资金 + 已实现盈亏 + 浮动盈亏
  持仓市值/浮盈来自组合接口(list_positions), 已实现盈亏为其 realized_pnl 字段
  (历史卖出净额, 已扣手续费), 故前端用 start_capital + portfolio 即可完整派生,
  与每笔买卖自然同步(买入消耗资金, 卖出落袋利润).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlmodel import Session

from app import db
from app.models.models import Account

DEFAULT_START_CAPITAL = 500000.0


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AccountManager:
    """资金账户单例."""

    def _get_or_create(self, session: Session) -> Account:
        acc = session.get(Account, 1)
        if acc is None:
            acc = Account(id=1, start_capital=DEFAULT_START_CAPITAL)
            session.add(acc)
            session.commit()
            session.refresh(acc)
        return acc

    def get(self, session: Session | None = None) -> dict[str, Any]:
        with session or db.session_scope() as s:
            acc = self._get_or_create(s)
            return {
                "start_capital": acc.start_capital,
                "updated_at": acc.updated_at,
            }

    def set_start(self, start_capital: float, session: Session | None = None) -> dict[str, Any]:
        """修改启动资金(可用资金/总权益由前端按持仓市值派生, 无需后端重算)."""
        if start_capital < 0:
            raise ValueError("启动资金不能为负")
        with session or db.session_scope() as s:
            acc = self._get_or_create(s)
            acc.start_capital = round(float(start_capital), 2)
            acc.updated_at = _now()
            s.add(acc)
            s.commit()
            s.refresh(acc)
            return {
                "start_capital": acc.start_capital,
                "updated_at": acc.updated_at,
            }


# 便捷单例
account_manager = AccountManager()
