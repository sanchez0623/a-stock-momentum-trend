"""系统/配置/数据源 API(方案 §6.1)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import config_manager
from app.core.datasource import data_source_manager

router = APIRouter(prefix="/api", tags=["system"])


class UpdateConfigBody(BaseModel):
    """配置更新请求: 传分组字典, 如 {"风控": {"stop_loss_pct": 6.0}}."""

    config: dict[str, Any]


@router.get("/health")
async def health() -> dict:
    """健康检查(容器 HEALTHCHECK 用), 含数据源状态与时间自检."""
    sources = data_source_manager.status()
    now = dt.datetime.now()
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "status": "up",
            "time": now.isoformat(),
            "tz": now.astimezone().tzname() or "Asia/Shanghai",
            "date": now.strftime("%Y-%m-%d"),
            "data_sources": sources,
            "db": "ok",
        },
    }


@router.get("/config")
async def get_config() -> dict:
    """读取全局配置(api_key 脱敏)."""
    return {"code": 0, "msg": "ok", "data": config_manager.masked()}


@router.put("/config")
async def update_config(body: UpdateConfigBody) -> dict:
    """更新配置(热生效)."""
    config_manager.update(body.config)
    return {"code": 0, "msg": "配置已更新", "data": config_manager.masked()}


@router.get("/data-sources/status")
async def data_source_status() -> dict:
    return {"code": 0, "msg": "ok", "data": data_source_manager.status()}


@router.post("/data-sources/test/{name}")
async def test_data_source(name: str) -> dict:
    """手动测试某数据源: 探活 + 取一只票的 K线验证."""
    result = await data_source_manager.test_source(name)
    return {"code": 0 if result["ok"] else 1, "msg": "ok" if result["ok"] else result.get("error", "fail"), "data": result}
