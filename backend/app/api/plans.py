"""交易计划 API(方案 §6.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.datasource import data_source_manager
from app.core.plan import plan_generator
from app.core.position import position_manager
from app.core.risk import risk_manager
from app.core.signals import SignalEngine
from app.core.signals.engine import PositionInfo
from app.models.models import Plan

router = APIRouter(prefix="/api", tags=["plans"])

engine = SignalEngine()


class GenerateBody(BaseModel):
    symbol: str
    name: str = ""


class PlanStatusBody(BaseModel):
    status: str = Field(pattern="^(done|ignored)$")


@router.post("/plan/generate")
async def generate_plan(body: GenerateBody, session: Session = Depends(get_session)) -> dict:
    """为指定票生成交易计划(评估信号 -> 组装人话指引 -> 落库 pending)."""
    df = await data_source_manager.get_kline(body.symbol, "daily", 120)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="无行情数据")
    quotes = await data_source_manager.get_realtime_quote([body.symbol])
    quote = quotes[0] if quotes else None
    pos = position_manager.get_position(body.symbol, session)
    pos_info = PositionInfo(symbol=body.symbol, cost=pos.cost, qty=pos.qty) if pos else None
    signal = engine.evaluate(
        body.symbol,
        name=body.name or (quote.name if quote else ""),
        kline_df=df,
        position=pos_info,
        quote_price=quote.price if quote else None,
        quote_high=quote.high if quote else None,
        quote_low=quote.low if quote else None,
    )
    if signal is None:
        raise HTTPException(status_code=404, detail="当前无信号, 无需生成计划")

    # 组合汇总(用于风控/仓位文案): 单票市值占总持仓市值比例
    portfolio: dict = {"total_pct": 0.0}
    positions = position_manager.list_positions(session)
    total_mv = sum((quote.price if p.symbol == body.symbol else p.cost) * p.qty for p in positions)
    if total_mv > 0 and pos:
        portfolio["total_pct"] = round((pos.cost * pos.qty) / total_mv * 100, 1)

    risk_status = risk_manager.status(session)
    plan_data = plan_generator.generate(
        body.symbol, body.name or (quote.name if quote else ""), signal, quote, portfolio, risk_status
    )
    plan = Plan(
        symbol=body.symbol,
        name=body.name or (quote.name if quote else ""),
        action=plan_data["action"],
        content=plan_data["content"],
        status="pending",
    )
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return {"code": 0, "msg": "ok", "data": plan.model_dump()}


@router.get("/plan/current")
async def current_plans(session: Session = Depends(get_session)) -> dict:
    stmt = select(Plan).where(Plan.status == "pending").order_by(Plan.time.desc())
    rows = session.exec(stmt).all()
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


@router.get("/plan/{plan_id}")
async def plan_detail(plan_id: int, session: Session = Depends(get_session)) -> dict:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    return {"code": 0, "msg": "ok", "data": plan.model_dump()}


@router.put("/plan/{plan_id}/status")
async def update_plan_status(plan_id: int, body: PlanStatusBody, session: Session = Depends(get_session)) -> dict:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    plan.status = body.status
    session.add(plan)
    session.commit()
    return {"code": 0, "msg": "ok", "data": plan.model_dump()}
