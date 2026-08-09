"""历史 K 线补拉 API: 手动触发 + 状态查询.

- POST /api/kline/backfill          触发补拉(异步, 返回 task_id)
- GET  /api/kline/backfill/status   最近一次任务状态
- GET  /api/kline/backfill/status/{task_id}  指定任务状态
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from app.core.backfill import DEFAULT_CONCURRENCY, DEFAULT_TARGET, backfill_history, backfill_tasks

router = APIRouter(prefix="/api", tags=["backfill"])


@router.post("/kline/backfill")
async def start_backfill(
    target: int = Query(DEFAULT_TARGET, ge=100, le=800, description="目标 K 线根数(默认 260)"),
    concurrency: int = Query(DEFAULT_CONCURRENCY, ge=1, le=16, description="并发数(默认 4)"),
    symbols: str | None = Query(None, description="逗号分隔指定股票(默认全市场)"),
    force: bool = Query(False, description="强制重拉全部(忽略已达标, 测试用)"),
) -> dict:
    """启动历史 K 线补拉(异步, 全市场约 5000 只, 并发 4 预计 10~25 分钟)."""
    task_id = backfill_tasks.create("backfill", 0)
    backfill_tasks.update(task_id, status="running", target=target, concurrency=concurrency)
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None

    async def _run() -> None:
        try:
            stats = await backfill_history(
                target=target,
                concurrency=concurrency,
                symbols=sym_list,
                force=force,
                progress_cb=lambda done, total: backfill_tasks.progress(task_id, done, total),
            )
            backfill_tasks.update(task_id, status="done", progress=100, result=stats)
        except Exception as exc:  # noqa: BLE001
            backfill_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "补拉已启动", "data": {"task_id": task_id}}


@router.get("/kline/backfill/status")
async def backfill_status_latest() -> dict:
    """最近一次补拉任务状态."""
    task = backfill_tasks.latest()
    if task is None:
        return {"code": 0, "msg": "ok", "data": None}
    return {"code": 0, "msg": "ok", "data": task}


@router.get("/kline/backfill/status/{task_id}")
async def backfill_status(task_id: str) -> dict:
    task = backfill_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 0, "msg": "ok", "data": task}
