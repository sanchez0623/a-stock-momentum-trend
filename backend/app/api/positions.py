"""持仓 API(方案 §6.5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import get_session
from app.core.datasource import data_source_manager
from app.core.position import position_manager

router = APIRouter(prefix="/api", tags=["positions"])


class PositionBody(BaseModel):
    symbol: str
    name: str = ""
    qty: int = Field(ge=1)
    price: float = Field(gt=0)
    reason: str = ""
    action: str = Field("buy", pattern="^(buy|sell)$")  # buy=首仓/加仓, sell=减仓


@router.get("/positions")
async def list_positions(session: Session = Depends(get_session)) -> dict:
    """当前持仓(含实时浮盈)."""
    positions = position_manager.list_positions(session)
    symbols = [p.symbol for p in positions]
    prices: dict[str, float] = {}
    if symbols:
        quotes = await data_source_manager.get_realtime_quote(symbols)
        prices = {q.symbol: q.price for q in quotes if q.is_valid}
    portfolio = position_manager.portfolio(prices, session)
    return {"code": 0, "msg": "ok", "data": portfolio}


@router.post("/positions")
async def upsert_position(body: PositionBody, session: Session = Depends(get_session)) -> dict:
    """手动录入/调整持仓(虚拟, 不接实盘)."""
    try:
        if body.action == "buy":
            pos = position_manager.open_or_add(body.symbol, body.name, body.qty, body.price, body.reason, session)
            return {"code": 0, "msg": "ok", "data": {"position": pos.model_dump(), "action": "buy"}}
        pnl = position_manager.reduce(body.symbol, body.qty, body.price, body.reason, session)
        return {"code": 0, "msg": "ok", "data": {"action": "sell", "realized_pnl": pnl}}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/positions/{symbol}")
async def position_detail(symbol: str, session: Session = Depends(get_session)) -> dict:
    pos = position_manager.get_position(symbol, session)
    if pos is None:
        raise HTTPException(status_code=404, detail="无持仓")
    history = position_manager.history(symbol, 50, session)
    # ATR 波动率(供动态止盈档计算; 失败则回退 fixed 档)
    atr_pct = None
    try:
        from app.core.datasource import data_source_manager
        from app.core.indicators import compute_all

        df = await data_source_manager.get_kline(symbol, "daily", 60)
        if df is not None and not df.empty:
            ind = compute_all(df)
            last = ind.iloc[-1]
            close = float(last["close"])
            atr14 = float(last.get("atr14", 0) or 0)
            if close > 0 and atr14 > 0:
                atr_pct = atr14 / close
    except Exception:  # noqa: BLE001
        atr_pct = None
    return {"code": 0, "msg": "ok", "data": {
        "position": pos.model_dump(),
        "pyramid": position_manager.pyramid_plan(symbol, session),
        "take_profit": position_manager.take_profit_levels(pos.cost, atr_pct, session),
        "atr_pct": round(atr_pct, 4) if atr_pct else None,
        "history": [t.model_dump() for t in history],
    }}
