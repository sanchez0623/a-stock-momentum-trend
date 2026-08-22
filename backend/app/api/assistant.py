"""AI 助理 API: 手动触发单阶段 / 查询状态."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_session
from app.core.assistant.pipeline import PHASES, run_phase
from app.core.assistant.scheduler import JOB_IDS
from app.core.config import config_manager
from app.core.report.notify import list_notifications

router = APIRouter(prefix="/api", tags=["assistant"])


@router.post("/assistant/run")
async def assistant_run(phase: str = Query("intraday", description="premarket/intraday/after_close")):
    """手动触发单阶段流水线(验证用, 不等定时任务)."""
    if phase not in PHASES:
        raise HTTPException(status_code=400, detail=f"phase 需为 {PHASES}")
    try:
        result = await run_phase(phase)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"code": 1, "msg": f"助理执行失败: {exc}"})
    return {"code": 0, "msg": "ok", "data": {
        "phase": phase,
        "signals": len(result.get("signals") or []),
        "fresh": len(result.get("fresh") or []),
        "insights": len(result.get("insights") or []),
        "notifications": result.get("notifications") or [],
        "market": (result.get("market") or {}).get("environment", ""),
    }}


@router.get("/assistant/status")
async def assistant_status(session=Depends(get_session)):
    """助理开关 + 定时任务注册状态 + 最近通知."""
    from app.scheduler import scheduler

    cfg = config_manager.get().get("ai_assistant", {}) or {}
    jobs = {jid: scheduler.get_job(jid) is not None for jid in JOB_IDS}
    recent = list_notifications(10, session=session)
    return {"code": 0, "msg": "ok", "data": {
        "enabled": bool(cfg.get("enabled")),
        "premarket": cfg.get("premarket", {}),
        "intraday": cfg.get("intraday", {}),
        "after_close": cfg.get("after_close", {}),
        "push_webhook": bool(cfg.get("push_webhook")),
        "jobs": jobs,
        "recent_notifications": [r.model_dump() for r in recent],
    }}
