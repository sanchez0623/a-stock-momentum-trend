"""选股扫描历史落库(内存任务重启即清, 此表持久保存供前端回看)."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import func, select

from app import db
from app.models.models import ScreenerHistory

_LIST_FIELDS = (
    "id", "time", "market", "board", "industry", "top_n", "per_industry",
    "industry_level", "apply_gate", "universe", "apply_factors",
    "total", "result_count", "status",
)


def _row_to_item(row: ScreenerHistory, with_result: bool = False) -> dict[str, Any]:
    item = {k: getattr(row, k) for k in _LIST_FIELDS}
    if with_result:
        item["error"] = row.error
        item["result"] = json.loads(row.result_json or "[]")
    return item


def save_scan_history(task: dict[str, Any], params: dict[str, Any]) -> int:
    """扫描任务完成(done)后持久化. 返回记录 id."""
    result = task.get("result") or []
    with db.session_scope() as s:
        row = ScreenerHistory(
            market=str(params.get("market", "all") or "all"),
            board=str(params.get("board", "") or ""),
            industry=str(params.get("industry", "") or ""),
            top_n=int(params.get("top_n", 30) or 30),
            per_industry=int(params.get("per_industry", 0) or 0),
            industry_level=str(params.get("industry_level", "sw_l1") or "sw_l1"),
            apply_gate=bool(params.get("apply_gate", True)),
            universe=str(params.get("universe", "") or ""),
            apply_factors=bool(params.get("apply_factors", True)),
            total=int(task.get("total", 0) or 0),
            result_count=len(result),
            status=str(task.get("status", "done") or "done"),
            result_json=json.dumps(result, ensure_ascii=False),
            error=str(task.get("error", "") or ""),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id or 0


def list_scan_history(page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    """历史列表分页(不含结果 JSON, 避免一次性拉取大字段). 返回 (items, total)."""
    with db.session_scope() as s:
        total = s.exec(select(func.count()).select_from(ScreenerHistory)).one()
        rows = s.exec(
            select(ScreenerHistory).order_by(ScreenerHistory.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
    return [_row_to_item(r) for r in rows], int(total)


def get_scan_history(history_id: int) -> dict[str, Any] | None:
    """历史详情(含完整结果列表)."""
    with db.session_scope() as s:
        row = s.get(ScreenerHistory, history_id)
        return _row_to_item(row, with_result=True) if row else None


def delete_scan_history(history_id: int) -> bool:
    with db.session_scope() as s:
        row = s.get(ScreenerHistory, history_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True
