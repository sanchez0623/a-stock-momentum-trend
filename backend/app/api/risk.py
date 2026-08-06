"""风控 API(方案 §6.6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.deps import get_session
from app.core.risk import risk_manager

router = APIRouter(prefix="/api", tags=["risk"])


@router.get("/risk/status")
async def risk_status(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": risk_manager.status(session)}


@router.post("/risk/reset")
async def risk_reset(session: Session = Depends(get_session)) -> dict:
    """重置熔断/防守/连亏(需确认, 本接口即确认)."""
    return {"code": 0, "msg": "已重置", "data": risk_manager.reset(session)}
