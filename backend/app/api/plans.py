"""交易计划 API(方案 §6.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import get_session
from app.core.account import account_manager
from app.core.config import config_manager
from app.core.datasource import data_source_manager
from app.core.modes import active_mode
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
    # force=True: 盘中信号评估必须用最新 K 线(日线缓存只记日期, 会把当天早盘 bar 视为新鲜)
    df = await data_source_manager.get_kline(body.symbol, "daily", 120, force=True)
    if df is None or df.empty:
        return {"code": 0, "msg": "无行情数据, 无法评估", "data": None}
    quotes = await data_source_manager.get_realtime_quote([body.symbol])
    quote = quotes[0] if quotes else None
    pos = position_manager.get_position(body.symbol, session)
    pos_info = (PositionInfo(symbol=body.symbol, cost=pos.cost, qty=pos.qty, peak_price=pos.peak_price)
                if pos else None)
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
        return {"code": 0, "msg": "当前无信号, 无需生成计划", "data": None}

    # 组合汇总(用于风控/仓位文案): 单票市值占总持仓市值比例 + 可用资金(Q4 资金感知)
    portfolio: dict = {"total_pct": 0.0}
    positions = position_manager.list_positions(session)
    ref_price = quote.price if quote else 0.0
    total_mv = sum((ref_price if p.symbol == body.symbol else p.cost) * p.qty for p in positions)
    if total_mv > 0 and pos:
        portfolio["total_pct"] = round((pos.cost * pos.qty) / total_mv * 100, 1)
    acc = account_manager.get(session)
    start_capital = float(acc.get("start_capital", 0.0) or 0.0)
    portfolio["start_capital"] = start_capital
    portfolio["market_value"] = round(total_mv, 2)
    portfolio["available_capital"] = round(start_capital - total_mv, 2)

    risk_status = risk_manager.status(session)
    # Q2: 规则化市况分类器选出当前交易模式, 注入计划(选型走显式规则, LLM 只解说)
    mode_decision = active_mode(body.symbol, df)
    # 动态仓位上限: 基础(80/25) × 防守/连亏砍半 × 大盘环境(择时闸门同源) × 凯利因子
    # 供计划展示动态上限, 加仓建议超限自动缩减
    limits = None
    try:
        from app.core.market_gate import compute_market_gate, fetch_gate_index_dfs

        gate_cfg = config_manager.get().get("择时闸门", {})
        market_env = None
        if gate_cfg.get("reference_indices"):
            idx_dfs = await fetch_gate_index_dfs(gate_cfg)
            if idx_dfs:
                market_env = compute_market_gate(idx_dfs, gate_cfg).get("environment")
        limits = risk_manager.dynamic_limits(session, market_env=market_env)
    except Exception:  # noqa: BLE001 - 动态上限失败不影响计划生成(回退静态 80%)
        limits = None
    plan_data = plan_generator.generate(
        body.symbol, body.name or (quote.name if quote else ""), signal, quote,
        portfolio, risk_status, mode=mode_decision, limits=limits,
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
