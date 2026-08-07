"""交易日志核心实现: SQLite + CSV 双写.

- CSV 字段(方案 §4.8): time, symbol, name, action, price, qty, amount,
  reason, signal_strength, plan_id, pnl, score, note
- CSV 追加写, UTF-8-BOM(Excel 直接打开不乱码)
- 手动回填: buy -> 顺向建/加仓, sell -> 减/清仓(自动算 pnl), 并同步持仓
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import db
from app.models.models import Trade

logger = logging.getLogger(__name__)

CSV_HEADER = ["time", "symbol", "name", "action", "price", "qty", "amount",
              "reason", "signal_strength", "plan_id", "pnl", "score", "note"]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TradeLogger:
    def __init__(self, csv_path: str | Path | None = None) -> None:
        if csv_path is None:
            data_dir = Path(os.environ.get("DATA_DIR", "data"))
            csv_path = data_dir / "trades.csv"
        self.csv_path = Path(csv_path)
        self._pos = None

    # 懒加载, 避免与 position.manager 循环 import
    def _position(self):
        if self._pos is None:
            from app.core.position.manager import PositionManager

            self._pos = PositionManager()
        return self._pos

    # ------------------------------------------------------------ CSV
    def _ensure_csv(self) -> None:
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(CSV_HEADER)

    def append_csv(self, row: dict[str, Any]) -> None:
        self._ensure_csv()
        with open(self.csv_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore")
            writer.writerow(row)

    def export_csv(self) -> str:
        """返回完整 CSV 文本(含表头), 供下载/复制."""
        if not self.csv_path.exists():
            self._ensure_csv()
        return self.csv_path.read_text(encoding="utf-8-sig")

    # ------------------------------------------------------------ 记录(双写)
    def record(
        self,
        symbol: str,
        name: str,
        action: str,
        price: float,
        qty: int,
        reason: str = "",
        signal_strength: float = 0.0,
        plan_id: int | None = None,
        pnl: float | None = None,
        score: float | None = None,
        note: str = "",
        time: str | None = None,
        session: Session | None = None,
    ) -> Trade:
        """写入 SQLite trades 表 + 追加 CSV."""
        row_time = time or _now()
        trade = Trade(time=row_time, symbol=symbol, name=name, action=action,
                      price=round(price, 4), qty=qty, amount=round(price * qty, 2),
                      reason=reason, signal_strength=signal_strength, plan_id=plan_id,
                      pnl=round(pnl, 2) if pnl is not None else None, score=score, note=note)
        if session is not None:
            session.add(trade)
            session.commit()
            session.refresh(trade)
        else:
            with db.session_scope() as s:
                s.add(trade)
                s.commit()
                s.refresh(trade)
        self.append_csv({
            "time": trade.time, "symbol": trade.symbol, "name": trade.name,
            "action": trade.action, "price": trade.price, "qty": trade.qty,
            "amount": trade.amount, "reason": trade.reason,
            "signal_strength": trade.signal_strength, "plan_id": trade.plan_id,
            "pnl": trade.pnl if trade.pnl is not None else "",
            "score": trade.score if trade.score is not None else "",
            "note": trade.note,
        })
        return trade

    # ------------------------------------------------------------ 查询
    def query(
        self,
        start: str | None = None,
        end: str | None = None,
        symbol: str | None = None,
        action: str | None = None,
        limit: int = 500,
        session: Session | None = None,
    ) -> list[Trade]:
        with session or db.session_scope() as s:
            stmt = select(Trade)
            if symbol:
                stmt = stmt.where(Trade.symbol == symbol)
            if action:
                stmt = stmt.where(Trade.action == action)
            if start:
                stmt = stmt.where(Trade.time >= start)
            if end:
                stmt = stmt.where(Trade.time <= end + " 23:59:59")
            stmt = stmt.order_by(Trade.time.desc()).limit(limit)
            return list(s.exec(stmt).all())

    def all(self, session: Session | None = None) -> list[Trade]:
        return self.query(limit=10_000, session=session)

    # ------------------------------------------------------------ 手动回填
    def manual_entry(
        self,
        symbol: str,
        name: str,
        action: str,
        price: float,
        qty: int,
        reason: str = "",
        signal_strength: float = 0.0,
        plan_id: int | None = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """手动回填成交: 同步持仓并写日志(持仓操作内部已双写, 此处不重复写). 返回 {trade, realized_pnl}."""
        from app.core.position.manager import PositionManagerError

        pos_mgr = self._position()
        if action == "buy":
            with session or db.session_scope() as s:
                pos_mgr.open_or_add(symbol, name, qty, price, reason, s)
                trade = self.query(symbol=symbol, action="buy", limit=1, session=s)[0]
            return {"trade": trade, "realized_pnl": 0.0}
        # sell: 减仓/清仓
        with session or db.session_scope() as s:
            pos = pos_mgr.get_position(symbol, s)
            if pos is None or pos.qty <= 0:
                raise PositionManagerError(f"{symbol} 无持仓, 无法回填卖出")
            qty = min(qty, pos.qty)
            realized = pos_mgr.reduce(symbol, qty, price, reason, s)
            trade = self.query(symbol=symbol, action="sell", limit=1, session=s)[0]
        return {"trade": trade, "realized_pnl": realized}

    def import_rows(self, rows: list[dict[str, Any]], session: Session | None = None) -> int:
        """批量导入历史成交(JSON 数组, 字段与手动回填一致, 支持 time). 持仓操作内部已双写."""
        pos_mgr = self._position()
        count = 0
        for row in rows:
            action = row.get("action", "buy")
            symbol = str(row.get("symbol", "")).strip()
            price = float(row.get("price", 0))
            qty = int(row.get("qty", 0))
            if not symbol or price <= 0 or qty <= 0:
                continue
            if action == "buy":
                with session or db.session_scope() as s:
                    pos_mgr.open_or_add(symbol, row.get("name", ""), qty, price,
                                        row.get("reason", ""), s)
            else:
                with session or db.session_scope() as s:
                    pos = pos_mgr.get_position(symbol, s)
                    if pos is None or pos.qty <= 0:
                        logger.warning("导入卖出失败(无持仓): %s", symbol)
                        continue
                    qty = min(qty, pos.qty)
                    pos_mgr.reduce(symbol, qty, price, row.get("reason", ""), s)
            count += 1
        return count
