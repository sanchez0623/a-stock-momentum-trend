"""选股扫描任务持久化(断点续传).

任务元数据 + 结果批次落库: 服务重启后 running 任务标记 interrupted,
前端可「继续扫描」—— 恢复参数/扫描池快照/已产生结果, 跳过已完成票继续评分.
结果即进度: 已完成的票 = 各批次中的 symbol 集合, 无需单独维护 done 集合.
"""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from app import db
from app.models.models import ScreenerTask, ScreenerTaskBatch, _now

BATCH_SIZE = 20  # 每批结果条数(崩溃时最多丢一批, 有 K 线缓存重扫成本低)


def _task_to_dict(t: ScreenerTask) -> dict[str, Any]:
    return {
        "task_id": t.task_id, "status": t.status, "market": t.market,
        "board": t.board, "industry": t.industry, "top_n": t.top_n,
        "per_industry": t.per_industry, "industry_level": t.industry_level,
        "apply_gate": t.apply_gate, "universe": t.universe, "apply_factors": t.apply_factors,
        "total": t.total, "done": t.done, "error": t.error,
        "created_at": t.created_at, "updated_at": t.updated_at,
    }


def create_task(task_id: str, params: dict[str, Any], symbols: list[str]) -> None:
    with db.session_scope() as s:
        s.add(ScreenerTask(
            task_id=task_id,
            market=str(params.get("market", "all")),
            board=str(params.get("board", "") or ""),
            industry=str(params.get("industry", "") or ""),
            top_n=int(params.get("top_n", 30) or 30),
            per_industry=int(params.get("per_industry", 0) or 0),
            industry_level=str(params.get("industry_level", "sw_l1") or "sw_l1"),
            apply_gate=bool(params.get("apply_gate", True)),
            universe=str(params.get("universe", "") or ""),
            apply_factors=bool(params.get("apply_factors", True)),
            symbols_json=json.dumps(symbols, ensure_ascii=False),
            total=len(symbols),
        ))
        s.commit()


def update_task(task_id: str, *, done: int | None = None, status: str | None = None,
                error: str | None = None) -> None:
    with db.session_scope() as s:
        t = s.exec(select(ScreenerTask).where(ScreenerTask.task_id == task_id)).first()
        if t is None:
            return
        if done is not None:
            t.done = done
        if status is not None:
            t.status = status
        if error is not None:
            t.error = error
        t.updated_at = _now()
        s.add(t)
        s.commit()


def append_batch(task_id: str, seq: int, results: list[dict[str, Any]]) -> None:
    if not results:
        return
    with db.session_scope() as s:
        s.add(ScreenerTaskBatch(
            task_id=task_id, seq=seq,
            data_json=json.dumps(results, ensure_ascii=False),
        ))
        s.commit()


def load_task(task_id: str) -> dict[str, Any] | None:
    with db.session_scope() as s:
        t = s.exec(select(ScreenerTask).where(ScreenerTask.task_id == task_id)).first()
        if t is None:
            return None
        out = _task_to_dict(t)
        out["symbols"] = json.loads(t.symbols_json or "[]")
        return out


def load_batches(task_id: str) -> list[dict[str, Any]]:
    """按 seq 合并已落库的结果批次(即已完成票的结果)."""
    with db.session_scope() as s:
        rows = s.exec(
            select(ScreenerTaskBatch).where(ScreenerTaskBatch.task_id == task_id)
            .order_by(ScreenerTaskBatch.seq)  # type: ignore[arg-type]
        ).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            out.extend(json.loads(r.data_json or "[]"))
        except json.JSONDecodeError:
            continue
    return out


def list_interrupted(limit: int = 10) -> list[dict[str, Any]]:
    """可恢复的中断任务(按更新时间倒序)."""
    with db.session_scope() as s:
        rows = s.exec(
            select(ScreenerTask).where(ScreenerTask.status == "interrupted")
            .order_by(ScreenerTask.updated_at.desc()).limit(limit)
        ).all()
    return [_task_to_dict(t) for t in rows]


def mark_all_running_interrupted() -> int:
    """启动自愈: 遗留 running 任务标记 interrupted(进程被杀/重启). 返回标记数."""
    n = 0
    with db.session_scope() as s:
        rows = s.exec(select(ScreenerTask).where(ScreenerTask.status == "running")).all()
        for t in rows:
            t.status = "interrupted"
            s.add(t)
            n += 1
        if n:
            s.commit()
    return n


def delete_task(task_id: str) -> None:
    with db.session_scope() as s:
        for b in s.exec(select(ScreenerTaskBatch).where(ScreenerTaskBatch.task_id == task_id)).all():
            s.delete(b)
        t = s.exec(select(ScreenerTask).where(ScreenerTask.task_id == task_id)).first()
        if t is not None:
            s.delete(t)
        s.commit()


def cleanup_old_tasks(keep: int = 20) -> None:
    """清理过期任务(仅保留最近 keep 个, 含批次)."""
    with db.session_scope() as s:
        rows = s.exec(
            select(ScreenerTask).order_by(ScreenerTask.updated_at.desc()).offset(keep)
        ).all()
        for t in rows:
            for b in s.exec(select(ScreenerTaskBatch).where(ScreenerTaskBatch.task_id == t.task_id)).all():
                s.delete(b)
            s.delete(t)
        if rows:
            s.commit()


__all__ = [
    "create_task", "update_task", "append_batch", "load_task", "load_batches",
    "list_interrupted", "mark_all_running_interrupted", "delete_task",
    "cleanup_old_tasks", "BATCH_SIZE",
]
