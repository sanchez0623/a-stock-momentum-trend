"""回测中心 API(方案C: 阶段分桶因子回测 + 全流程策略回测)."""

from __future__ import annotations

import asyncio
import threading
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.backtest.data import backtest_data
from app.core.backtest.factor import backtest_factors
from app.core.backtest.strategy import run_strategy_backtest

router = APIRouter(prefix="/api", tags=["backtest"])


class BacktestDataWarmupBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 自选 + 持仓
    start: str = ""                    # 空 = end 前推 3 年(含指标预热回退)
    end: str = ""                      # 空 = 今天
    force: bool = False                # 显式重拉(常规路径不需要)


@router.post("/backtest/data/warmup")
async def warmup_backtest_data(body: BacktestDataWarmupBody) -> dict:
    """回测数据预热: 把目标股票池区间数据拉成前复权冻结快照(baostock).

    冻结语义: 已拉取日期不再覆盖; 同区间重复调用命中快照不重复拉取.
    """
    try:
        symbols = body.symbols or _default_backtest_pool()
        if not symbols:
            return {"code": 0, "msg": "ok(空股票池, 无需预热)", "data": {"meta": {"symbols": 0}, "results": {}}}
        report = await asyncio.to_thread(
            backtest_data.warmup,
            symbols=symbols,
            start=body.start,
            end=body.end,
            force=bool(body.force),
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"回测数据预热失败: {exc}", "data": None}


@router.get("/backtest/data/status")
async def backtest_data_status() -> dict:
    """回测冻结快照状态(诊断用)."""
    try:
        return {"code": 0, "msg": "ok", "data": backtest_data.status()}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"查询失败: {exc}", "data": None}


def _default_backtest_pool() -> list[str]:
    """默认预热池: 自选 + 持仓(与策略回测默认池口径一致)."""
    from sqlmodel import select

    from app import db
    from app.models.models import Position, Watchlist

    with db.session_scope() as s:
        wl = [r.symbol for r in s.exec(select(Watchlist)).all()]
        pos = [r.symbol for r in s.exec(select(Position).where(Position.status == "holding")).all()]
    return list(dict.fromkeys(wl + pos))


class BacktestFactorBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 缓存全市场
    hold_days: list[int] = [5, 10, 20]
    min_bars: int = 60
    cost: bool = True                  # 是否扣除双边手续费


class BacktestStrategyBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 自选 + 持仓
    initial_capital: float = 1_000_000.0


@router.post("/backtest/factor")
async def run_factor(body: BacktestFactorBody) -> dict:
    """阶段分桶因子回测(同步; 全市场缓存约 60s, 走线程池避免阻塞事件循环)."""
    try:
        hold = tuple(sorted({int(h) for h in body.hold_days}))
        report = await asyncio.to_thread(
            backtest_factors,
            symbols=body.symbols,
            hold_days=hold,
            min_bars=max(30, int(body.min_bars)),
            cost=bool(body.cost),
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"回测失败: {exc}", "data": None}


# ---------------------------------------------------------------- 策略回测(异步任务)
# 内存任务表(进程内有效, 重启丢失; MVP 够用). 结构: {task_id: {status, progress, result, error}}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


@router.post("/backtest/strategy")
async def run_strategy(body: BacktestStrategyBody) -> dict:
    """全流程策略回测(异步: 建仓/加仓/止盈/止损/做T + 风控三道闸门). 返回 task_id."""
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {"status": "running", "progress": 0, "result": None, "error": ""}

    def _run() -> None:
        try:
            def cb(done: int, total: int) -> None:
                with _tasks_lock:
                    _tasks[task_id]["progress"] = round(done / total * 100) if total else 0

            report = run_strategy_backtest(
                symbols=body.symbols,
                initial_capital=max(10_000.0, float(body.initial_capital)),
                progress_cb=cb,
            )
            with _tasks_lock:
                _tasks[task_id]["result"] = report
                _tasks[task_id]["status"] = "done"
                _tasks[task_id]["progress"] = 100
        except Exception as exc:  # noqa: BLE001
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = str(exc)

    threading.Thread(target=_run, daemon=True).start()
    return {"code": 0, "msg": "ok", "data": {"task_id": task_id}}


@router.get("/backtest/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    """查询策略回测任务进度/结果."""
    with _tasks_lock:
        t = _tasks.get(task_id)
    if t is None:
        return {"code": 1, "msg": "任务不存在(进程重启后任务丢失)", "data": None}
    return {"code": 0, "msg": "ok", "data": dict(t)}
