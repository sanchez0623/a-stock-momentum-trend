"""得分追踪 API: 追踪增删/列表/采样点/手动采样."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.tracking import (
    archive_expired,
    delete_point,
    list_active,
    points,
    sample_all,
    stop,
    track,
)

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


class TrackBody(BaseModel):
    symbol: str
    name: str = ""
    score: float = 0.0
    stage: str = ""


@router.post("")
async def tracking_add(body: TrackBody) -> dict:
    """从选股结果添加追踪(已活跃追踪不重复)."""
    d = track(body.symbol, body.name, body.score, body.stage)
    return {"code": 0, "msg": "已加入得分追踪", "data": d}


@router.delete("/points/{point_id}")
async def tracking_point_delete(point_id: int) -> dict:
    """删除单条采样点(误采/异常数据清理). 注意: 必须先于 /{symbol} 注册, 否则被通配拦截."""
    ok = delete_point(point_id)
    return {"code": 0 if ok else 1, "msg": "已删除采样点" if ok else "采样点不存在", "data": {"ok": ok}}


@router.delete("/{symbol}")
async def tracking_remove(symbol: str) -> dict:
    """停止追踪(手动归档, 保留历史采样)."""
    ok = stop(symbol)
    return {"code": 0 if ok else 1, "msg": "已停止追踪" if ok else "未在追踪中", "data": {"ok": ok}}


@router.get("")
async def tracking_list() -> dict:
    """活跃追踪列表(附最近一次采样)."""
    return {"code": 0, "msg": "ok", "data": {"items": list_active()}}


@router.get("/points/{symbol}")
async def tracking_points(symbol: str) -> dict:
    """单票采样时间序列(图表数据, 升序)."""
    return {"code": 0, "msg": "ok", "data": {"items": points(symbol)}}


@router.post("/sample-now")
async def tracking_sample_now() -> dict:
    """手动立即采样(定时任务之外, 便于补采/测试)."""
    r = await sample_all(kind="manual")
    n = archive_expired()
    r["archived"] = n
    return {"code": 0, "msg": f"采样完成: {r['ok']}/{r['total']} 成功", "data": r}
