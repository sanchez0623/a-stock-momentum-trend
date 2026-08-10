"""盘后日报 API: 手动触发 / 查询日报 / 站内通知."""

from __future__ import annotations

import contextlib
import json
from datetime import date as date_cls

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.api.deps import get_session
from app.core.report.notify import list_notifications, mark_read
from app.core.report.service import report_service

router = APIRouter(prefix="/api", tags=["report"])


def _report_dump(r) -> dict:
    content = {}
    with contextlib.suppress(json.JSONDecodeError):
        content = json.loads(r.content_json or "{}")
    return {
        "id": r.id,
        "date": r.date,
        "status": r.status,
        "model": r.model,
        "created_at": r.created_at,
        "content": content,
    }


@router.post("/report/daily/run")
async def run_daily_report(session: Session = Depends(get_session)) -> dict:
    """手动生成今日日报(验证用, 不等到 16:30 定时任务)."""
    try:
        report = await report_service.generate(date_cls.today().isoformat(), session)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"code": 1, "msg": f"日报生成失败: {exc}"})
    return {"code": 0, "msg": "日报已生成", "data": _report_dump(report)}


@router.get("/report/daily")
async def get_daily_report(date: str | None = Query(None, description="YYYY-MM-DD, 缺省今天"),
                           session: Session = Depends(get_session)) -> dict:
    target = date or date_cls.today().isoformat()
    row = report_service.get(target, session)
    if row is None:
        return {"code": 0, "msg": "当日暂无日报", "data": None}
    return {"code": 0, "msg": "ok", "data": _report_dump(row)}


@router.get("/notifications")
async def get_notifications(limit: int = Query(20, ge=1, le=100), unread_only: bool = False,
                            session: Session = Depends(get_session)) -> dict:
    rows = list_notifications(limit, unread_only, session)
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


@router.post("/notifications/{nid}/read")
async def mark_notification_read(nid: int, session: Session = Depends(get_session)) -> dict:
    row = mark_read(nid, session)
    if row is None:
        return JSONResponse(status_code=404, content={"code": 1, "msg": "通知不存在"})
    return {"code": 0, "msg": "已读", "data": row.model_dump()}
