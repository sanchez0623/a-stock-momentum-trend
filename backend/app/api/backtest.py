"""回测中心 API(方案C MVP: 阶段分桶因子回测)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.backtest.factor import backtest_factors

router = APIRouter(prefix="/api", tags=["backtest"])


class BacktestFactorBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 缓存全市场
    hold_days: list[int] = [5, 10, 20]
    min_bars: int = 60
    cost: bool = True                  # 是否扣除双边手续费


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
