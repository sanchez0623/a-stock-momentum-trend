"""选股 API(方案 §6.3) + 自选股."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.screener import scan_tasks, screener
from app.models.models import Watchlist

router = APIRouter(prefix="/api", tags=["screener"])


class WatchlistBody(BaseModel):
    symbol: str
    name: str = ""


@router.post("/screener/run")
async def run_screener(
    market: str = Query("all", pattern="^(all|sh|sz|bj)$"),
    board: str | None = Query(None, pattern="^(main|chinext|star|bj)$", description="板块: main主板/chinext创业板/star科创板/bj北交所"),
    industry: str | None = Query(None, description="申万行业名(包含匹配, 需本地行业数据)"),
    top_n: int = Query(30, ge=5, le=200),
) -> dict:
    """触发扫描(异步, 返回 task_id). 支持 market + board + industry 组合缩小范围."""
    task_id = scan_tasks.create(market, top_n)
    scan_tasks.update(task_id, status="running")

    async def _run() -> None:
        try:
            result = await screener.scan(
                market=market,
                board=board,
                industry=industry,
                top_n=top_n,
                progress_cb=lambda done, total: scan_tasks.progress(task_id, done, total),
            )
            scan_tasks.update(task_id, status="done", progress=100, result=result)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "扫描已启动", "data": {"task_id": task_id}}


@router.get("/screener/result")
async def screener_result(task_id: str) -> dict:
    task = scan_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 0, "msg": "ok", "data": task}


@router.get("/screener/result/latest")
async def screener_latest() -> dict:
    task = scan_tasks.latest()
    if task is None:
        return {"code": 0, "msg": "ok", "data": None}
    return {"code": 0, "msg": "ok", "data": task}


# ---------------------------------------------------------------- 自选股
@router.get("/watchlist")
async def watchlist(session: Session = Depends(get_session)) -> dict:
    stmt = select(Watchlist).order_by(Watchlist.added_at.desc())
    rows = session.exec(stmt).all()
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


@router.post("/watchlist")
async def add_watch(body: WatchlistBody, session: Session = Depends(get_session)) -> dict:
    row = session.get(Watchlist, body.symbol)
    if row is None:
        row = Watchlist(symbol=body.symbol, name=body.name)
        session.add(row)
    else:
        row.name = body.name or row.name
    session.commit()
    return {"code": 0, "msg": "ok", "data": {"symbol": body.symbol}}


@router.delete("/watchlist/{symbol}")
async def remove_watch(symbol: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(Watchlist, symbol)
    if row:
        session.delete(row)
        session.commit()
    return {"code": 0, "msg": "ok", "data": {"symbol": symbol}}
