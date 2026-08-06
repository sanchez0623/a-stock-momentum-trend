"""信号 API(方案 §6.4 部分)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.datasource import data_source_manager
from app.core.position import position_manager
from app.core.signals import SignalEngine
from app.core.signals.engine import PositionInfo
from app.models.models import SignalRecord

router = APIRouter(prefix="/api", tags=["signals"])

engine = SignalEngine()


@router.get("/signals")
async def list_signals(
    symbol: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(SignalRecord).order_by(SignalRecord.time.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(SignalRecord.symbol == symbol)
    rows = session.exec(stmt).all()
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


@router.get("/signals/{symbol}")
async def latest_signal(symbol: str, session: Session = Depends(get_session)) -> dict:
    stmt = select(SignalRecord).where(SignalRecord.symbol == symbol).order_by(SignalRecord.time.desc())
    row = session.exec(stmt).first()
    if row is None:
        return JSONResponse(status_code=404, content={"code": 1, "msg": "无信号", "data": None})
    return {"code": 0, "msg": "ok", "data": row.model_dump()}


@router.post("/signals/evaluate/{symbol}")
async def evaluate_symbol(symbol: str, session: Session = Depends(get_session)) -> dict:
    """手动评估一只票: 取K线+行情 -> 生成信号(不落库, 供计划生成用)."""
    df = await data_source_manager.get_kline(symbol, "daily", 120)
    if df is None or df.empty:
        return JSONResponse(status_code=404, content={"code": 1, "msg": "无行情数据", "data": None})
    quote = None
    quotes = await data_source_manager.get_realtime_quote([symbol])
    if quotes:
        quote = quotes[0]
    pos = position_manager.get_position(symbol, session)
    pos_info = PositionInfo(symbol=symbol, cost=pos.cost, qty=pos.qty) if pos else None
    signal = engine.evaluate(
        symbol,
        name=quote.name if quote else "",
        kline_df=df,
        position=pos_info,
        quote_price=quote.price if quote else None,
        quote_high=quote.high if quote else None,
        quote_low=quote.low if quote else None,
    )
    if signal is None:
        return {"code": 0, "msg": "ok", "data": {"symbol": symbol, "signal": None}}
    return {"code": 0, "msg": "ok", "data": {"symbol": symbol, "signal": signal.to_dict()}}
