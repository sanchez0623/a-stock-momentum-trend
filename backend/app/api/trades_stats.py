"""三期 API: 交易日志 / CSV 导出 / 持仓减仓清仓 / 历史统计与评分.

补充(2026-08-07 整合): 手动回填 POST /api/trades + 批量导入 POST /api/trades/import,
成交走 TradeLogger 双写(SQLite + data/trades.csv 实时追加).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import get_session
from app.core.logger import trade_logger
from app.core.position import position_manager
from app.core.stats import stats
from app.core.tradelog import trade_log

router = APIRouter(prefix="/api", tags=["trades-stats"])


class ReduceBody(BaseModel):
    qty: int = Field(ge=1)
    price: float = Field(gt=0)
    reason: str = ""


class TradeBody(BaseModel):
    symbol: str
    name: str = ""
    action: str = Field(pattern="^(buy|sell)$")
    price: float = Field(gt=0)
    qty: int = Field(ge=1)
    reason: str = ""
    signal_strength: float = 0.0
    plan_id: int | None = None


class ImportBody(BaseModel):
    rows: list[dict]


# ---------------------------------------------------------------- 交易日志
@router.get("/trades")
async def list_trades(
    symbol: str | None = None,
    action: str | None = Query(None, pattern="^(buy|sell)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    rows = trade_log.list_trades(symbol, action, limit, offset, session)
    total = trade_log.count(symbol, action, session)
    return {"code": 0, "msg": "ok", "data": {
        "total": total, "items": [t.model_dump() for t in rows],
    }}


@router.get("/trades/export")
async def export_trades(session: Session = Depends(get_session)) -> Response:
    content, filename = trade_log.export_csv(session)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/trades")
async def manual_entry(body: TradeBody, session: Session = Depends(get_session)) -> dict:
    """手动回填成交(同步更新持仓, 双写日志)."""
    try:
        result = trade_logger.manual_entry(
            body.symbol, body.name, body.action, body.price, body.qty,
            body.reason, body.signal_strength, body.plan_id, session,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "msg": "ok", "data": {
        "trade": result["trade"].model_dump(),
        "realized_pnl": result["realized_pnl"],
    }}


@router.post("/trades/import")
async def import_trades(body: ImportBody, session: Session = Depends(get_session)) -> dict:
    """批量导入历史成交(JSON 数组: symbol/action/price/qty/name/reason/time...)."""
    try:
        count = trade_logger.import_rows(body.rows, session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "msg": "ok", "data": {"imported": count}}


# ---------------------------------------------------------------- 持仓操作(减仓/清仓)
@router.post("/positions/{symbol}/reduce")
async def reduce_position(symbol: str, body: ReduceBody, session: Session = Depends(get_session)) -> dict:
    try:
        pnl = position_manager.reduce(symbol, body.qty, body.price, body.reason, session)
        return {"code": 0, "msg": "减仓成功", "data": {"realized_pnl": pnl}}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/positions/{symbol}/close")
async def close_position(symbol: str, body: ReduceBody, session: Session = Depends(get_session)) -> dict:
    try:
        pnl = position_manager.close(symbol, body.price, body.reason, session)
        return {"code": 0, "msg": "清仓成功", "data": {"realized_pnl": pnl}}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------- 历史回顾统计
@router.get("/stats/summary")
async def stats_summary(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": stats.summary(session)}


@router.get("/stats/equity-curve")
async def stats_equity_curve(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": {"curve": stats.equity_curve(session)}}


@router.get("/stats/monthly-heatmap")
async def stats_monthly(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": stats.monthly_heatmap(session)}


@router.get("/stats/signal-distribution")
async def stats_signals(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": {"items": stats.signal_distribution(session)}}


@router.get("/stats/scores")
async def stats_scores(session: Session = Depends(get_session)) -> dict:
    return {"code": 0, "msg": "ok", "data": stats.trade_scores(session)}
