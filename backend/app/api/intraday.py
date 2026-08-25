"""盘中监控 API: 状态查询 / 手动触发 / 预警历史."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.config import config_manager
from app.core.intraday import run_intraday_monitor
from app.models.models import Notification, _now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["intraday"])


@router.get("/intraday/status")
async def intraday_status() -> dict:
    """盘中监控状态: 开关/配置/今日预警数."""
    cfg = config_manager.get().get("盘中监控", {}) or {}
    today = _now()[:10]
    today_alerts = 0
    try:
        from app import db
        with db.session_scope() as s:
            today_alerts = len(s.exec(
                select(Notification).where(
                    Notification.category == "intraday_alert",
                    Notification.time.startswith(today),
                )
            ).all())
    except Exception:
        pass

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "enabled": cfg.get("enabled", False),
            "interval_sec": cfg.get("interval_sec", 30),
            "scope": cfg.get("scope", "positions_watchlist"),
            "cooldown_sec": cfg.get("cooldown_sec", 300),
            "alert_rules": cfg.get("alert_rules", {}),
            "today_alerts": today_alerts,
        },
    }


@router.post("/intraday/run")
async def intraday_run() -> dict:
    """手动触发一次盘中监控轮询(验证用)."""
    result = await run_intraday_monitor()
    return {"code": 0, "msg": "ok", "data": result}


@router.get("/intraday/alerts")
async def intraday_alerts(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    """查询盘中预警历史通知."""
    rows = session.exec(
        select(Notification)
        .where(Notification.category == "intraday_alert")
        .order_by(Notification.time.desc())
        .limit(limit)
    ).all()
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


# ---------------------------------------------------------------- 做T波幅建议(P1)
@router.get("/intraday/t-swing")
async def t_swing_list() -> dict:
    """查询今日 LLM 做T波幅建议."""
    from app.core.assistant.t_swing import list_today_advices

    items = list_today_advices()
    return {"code": 0, "msg": "ok", "data": items}


@router.post("/intraday/t-swing/run")
async def t_swing_run() -> dict:
    """手动生成今日做T波幅建议(验证用, 不等盘前定时任务)."""
    from app.core.assistant.t_swing import generate_premarket_swing

    result = await generate_premarket_swing()
    if result.get("skipped"):
        return {"code": 1, "msg": str(result["skipped"]), "data": result}
    return {"code": 0, "msg": "ok", "data": result}
