"""交易日志服务: 查询/导出(CSV)/统计基础.

三期将交易日志与 CSV 双写打通:
- 每次开仓/减仓/清仓在 DB trades 表留痕(二期已实现, position.manager 写入)
- 本服务提供查询筛选、CSV 导出
- CSV 落盘目录: data/exports/
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from sqlmodel import Session, select

from app import db
from app.models.models import Trade

# CSV 导出目录(方案 §4.8: data/exports)
EXPORT_DIR = Path("data") / "exports"

CSV_HEADERS = ["时间", "代码", "名称", "方向", "价格", "数量", "金额", "手续费", "盈亏(净)", "原因"]


class TradeLogService:
    def list_trades(
        self,
        symbol: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
        session: Session | None = None,
    ) -> list[Trade]:
        with session or db.session_scope() as s:
            stmt = select(Trade).order_by(Trade.time.desc(), Trade.id.desc()).limit(limit).offset(offset)
            if symbol:
                stmt = stmt.where(Trade.symbol == symbol)
            if action:
                stmt = stmt.where(Trade.action == action)
            return list(s.exec(stmt).all())

    def count(self, symbol: str | None = None, action: str | None = None, session: Session | None = None) -> int:
        with session or db.session_scope() as s:
            stmt = select(Trade)
            if symbol:
                stmt = stmt.where(Trade.symbol == symbol)
            if action:
                stmt = stmt.where(Trade.action == action)
            return len(list(s.exec(stmt).all()))

    def all_trades(self, session: Session | None = None) -> list[Trade]:
        with session or db.session_scope() as s:
            stmt = select(Trade).order_by(Trade.time.asc(), Trade.id.asc())
            return list(s.exec(stmt).all())

    def export_csv(self, session: Session | None = None) -> tuple[str, str]:
        """导出全部交易日志到 CSV. 返回 (csv内容, 文件名)."""
        trades = self.all_trades(session)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_HEADERS)
        for t in trades:
            writer.writerow([
                t.time, t.symbol, t.name, "买入" if t.action == "buy" else "卖出",
                t.price, t.qty, t.amount, t.fee, t.pnl if t.pnl is not None else "", t.reason,
            ])
        filename = f"trades_{dt.date.today().strftime('%Y%m%d_%H%M%S')}.csv"
        return buf.getvalue(), filename

    def save_csv_file(self, content: str, filename: str) -> Path:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / filename
        path.write_text(content, encoding="utf-8-sig")  # BOM 兼容 Excel
        return path


def export_trades_csv() -> tuple[str, str]:
    """便捷入口(供 API 直接下载)."""
    return TradeLogService().export_csv()
