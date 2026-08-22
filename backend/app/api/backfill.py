"""历史 K 线补拉 API: 手动触发 + 状态查询 + 缓存新鲜度统计.

- POST /api/kline/backfill          触发补拉(异步, 返回 task_id)
- GET  /api/kline/backfill/status   最近一次任务状态
- GET  /api/kline/backfill/status/{task_id}  指定任务状态
- GET  /api/kline-cache/stats       日线缓存新鲜度统计(数据管理页; 避开 /kline/{symbol} 通配)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, select

from app import db
from app.core.backfill import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TARGET,
    _load_cache_status,
    backfill_history,
    backfill_tasks,
)
from app.models.models import KlineCache

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


@router.get("/kline-cache/stats")
async def kline_stats(target: int = Query(DEFAULT_TARGET, ge=100, le=800)) -> dict:
    """日线缓存新鲜度统计(数据管理页): ok/stale/missing 分布 + 数据日期范围."""
    try:
        status = await asyncio.to_thread(_load_cache_status, target)
        ok = sum(1 for v in status.values() if v == "ok")
        stale = sum(1 for v in status.values() if v == "stale")
        missing = sum(1 for v in status.values() if v == "missing")

        def _job() -> dict:
            first, last, n = "", "", 0
            with Session(db.engine) as s:
                rows = s.exec(
                    select(KlineCache.symbol, KlineCache.ohlcv_json)
                    .where(KlineCache.period == "daily")
                ).all()
            for _, blob in rows:
                try:
                    bars = json.loads(blob or "[]")
                except (ValueError, TypeError):
                    continue
                if not bars:
                    continue
                n += 1
                f, l = str(bars[0].get("date", ""))[:10], str(bars[-1].get("date", ""))[:10]
                if not first or (f and f < first):
                    first = f
                if l > last:
                    last = l
            return {"symbols": n, "date_from": first, "date_to": last}

        rng = await asyncio.to_thread(_job)
        today = dt.date.today()
        days_behind = (today - dt.date.fromisoformat(rng["date_to"])).days if rng["date_to"] else None
        return {"code": 0, "msg": "ok", "data": {
            "ok": ok, "stale": stale, "missing": missing, "cached": len(status),
            "days_behind": days_behind,
            **rng,
            "target": target,
            "note": "ok=达标或已尽力(次新) / stale=陈旧或不足待补 / missing=无缓存; 回测与实盘共用此缓存",
        }}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"统计失败: {exc}", "data": None}
