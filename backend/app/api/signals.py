"""信号 API(方案 §6.4 部分)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.datasource import data_source_manager
from app.core.position import position_manager
from app.core.signals import SignalEngine
from app.core.signals.engine import PositionInfo, Signal
from app.models.models import SignalRecord, _now

router = APIRouter(prefix="/api", tags=["signals"])

engine = SignalEngine()


class EvaluateBatchBody(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=50)


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


def _store_signal(session: Session, symbol: str, name: str, signal: Signal | None) -> None:
    """评估产生信号时写入 SignalRecord; 同代码同类型当日已记录则跳过, 避免重复刷屏.

    仅落库真实信号(signal 非空); 无信号/行情缺失不写, 保证「最近信号」只反映有效信号。
    """
    if signal is None:
        return
    today = _now()[:10]
    latest = session.exec(
        select(SignalRecord).where(SignalRecord.symbol == symbol).order_by(SignalRecord.time.desc())
    ).first()
    # 同日同类型已存在 -> 视为重复评估, 跳过
    if latest is not None and latest.time[:10] == today and latest.type == signal.type:
        return
    session.add(SignalRecord(
        time=_now(),
        symbol=signal.symbol,
        name=signal.name or name or "",
        type=signal.type,
        direction=signal.direction,
        strength=round(float(signal.strength), 2),
        reason=signal.reason,
        indicators_json=json.dumps(signal.indicators_snapshot or {}, ensure_ascii=False),
    ))


async def _evaluate_one(symbol: str, session: Session) -> dict:
    """评估单只票(不落库). 返回 {symbol, name, price, signal|None, error?}."""
    try:
        df = await data_source_manager.get_kline(symbol, "daily", 120)
        if df is None or df.empty:
            return {"symbol": symbol, "name": "", "price": 0.0, "signal": None, "error": "无行情数据"}
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
        _store_signal(session, symbol, quote.name if quote else "", signal)
        return {
            "symbol": symbol,
            "name": quote.name if quote else "",
            "price": round(quote.price, 2) if quote else 0.0,
            "signal": signal.to_dict() if signal else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "name": "", "price": 0.0, "signal": None, "error": str(exc)[:80]}


@router.post("/signals/evaluate/{symbol}")
async def evaluate_symbol(symbol: str, session: Session = Depends(get_session)) -> dict:
    """手动评估一只票: 取K线+行情 -> 生成信号并落库(供仪表盘「最近信号」与信号记录)."""
    data = await _evaluate_one(symbol, session)
    session.commit()
    if data.get("error") == "无行情数据":
        return JSONResponse(status_code=404, content={"code": 1, "msg": "无行情数据", "data": None})
    return {"code": 0, "msg": "ok", "data": data}


@router.post("/signals/evaluate-batch")
async def evaluate_batch(body: EvaluateBatchBody, session: Session = Depends(get_session)) -> dict:
    """批量评估多只(持仓一键分析): 并发取数, 信号落库, 返回每只的结果列表."""
    results = await _evaluate_many(body.symbols, session)
    session.commit()
    return {"code": 0, "msg": "ok", "data": results}


async def _evaluate_many(symbols: list[str], session: Session) -> list[dict]:
    import asyncio

    sem = asyncio.Semaphore(5)  # 并发 5, 防止对数据源限流

    async def guarded(sym: str) -> dict:
        async with sem:
            return await _evaluate_one(sym, session)

    return list(await asyncio.gather(*(guarded(s) for s in symbols)))
