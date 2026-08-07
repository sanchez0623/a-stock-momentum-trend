"""AI 复盘 API(方案 §6.9): 触发/结果/历史/配置."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_session
from app.core.ai_review import review_service
from app.core.config import config_manager

router = APIRouter(prefix="/api/ai-review", tags=["ai-review"])

_tasks: dict[str, dict[str, Any]] = {}


class RunBody(BaseModel):
    scope: str = "week"  # week | month | all | YYYY-MM-DD..YYYY-MM-DD


class SuggestionBody(BaseModel):
    review_id: int
    index: int
    status: str  # accepted / rejected


class ConfigBody(BaseModel):
    base_url: str = ""
    api_key: str = ""  # 空表示不修改(脱敏不回传)
    model: str = ""
    enabled: bool | None = None


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "****" + key[-4:]


def _review_dump(r) -> dict:
    return {
        "id": r.id, "time": r.time, "range": r.range, "content": r.content,
        "suggestions": json.loads(r.suggestions_json or "[]"),
        "model": r.model,
        "rule_result": json.loads(r.rule_result_json or "{}"),
    }


@router.post("/run")
async def run_review(body: RunBody, session: Session = Depends(get_session)) -> dict:
    """触发复盘(异步, 返回 task_id)."""
    task_id = f"rev_{asyncio.get_running_loop().time():.0f}"
    _tasks[task_id] = {"status": "running", "progress": 0}

    async def _run() -> None:
        try:
            review = await review_service.run(body.scope, session)
            _tasks[task_id] = {"status": "done", "progress": 100, "review": _review_dump(review)}
        except Exception as exc:  # noqa: BLE001
            _tasks[task_id] = {"status": "failed", "error": str(exc)}

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "复盘已启动", "data": {"task_id": task_id}}


@router.get("/result")
async def review_result(task_id: str) -> dict:
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 0, "msg": "ok", "data": task}


@router.get("/history")
async def review_history(limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_session)) -> dict:
    rows = review_service.history(limit, session)
    return {"code": 0, "msg": "ok", "data": [_review_dump(r) for r in rows]}


@router.post("/suggestion")
async def mark_suggestion(body: SuggestionBody, session: Session = Depends(get_session)) -> dict:
    try:
        review = review_service.update_suggestion(body.review_id, body.index, body.status, session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return {"code": 0, "msg": "ok", "data": {"id": review.id, "suggestions": json.loads(review.suggestions_json or "[]")}}


@router.get("/config")
async def get_llm_config() -> dict:
    """LLM 配置(key 脱敏)."""
    llm = config_manager.get().get("llm", {})
    return {"code": 0, "msg": "ok", "data": {
        "provider": llm.get("provider", "openai_compatible"),
        "base_url": llm.get("base_url", ""),
        "api_key": _mask_key(llm.get("api_key", "")),
        "has_key": bool(llm.get("api_key")),
        "model": llm.get("model", ""),
        "enabled": bool(llm.get("enabled")),
    }}


@router.put("/config")
async def update_llm_config(body: ConfigBody) -> dict:
    """更新 LLM 配置(api_key 留空则不覆盖已保存的 key)."""
    llm = dict(config_manager.get().get("llm", {}))
    if body.base_url:
        llm["base_url"] = body.base_url
    if body.api_key:
        llm["api_key"] = body.api_key.strip()
    if body.model:
        llm["model"] = body.model
    if body.enabled is not None:
        llm["enabled"] = body.enabled
    config_manager.update({"llm": llm})
    return {"code": 0, "msg": "配置已保存", "data": {
        "base_url": llm.get("base_url", ""), "model": llm.get("model", ""),
        "has_key": bool(llm.get("api_key")), "enabled": bool(llm.get("enabled")),
    }}
