"""选股条件组合预设(指数池 + 板块 + 行业, 一键复用)."""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from app import db
from app.models.models import ScreenerPreset

_FIELDS = ("id", "name", "universe", "board", "industry", "created_at")


def list_presets(limit: int = 50) -> list[dict[str, Any]]:
    """预设列表(按创建时间倒序)."""
    with db.session_scope() as s:
        rows = s.exec(
            select(ScreenerPreset).order_by(ScreenerPreset.created_at.desc()).limit(limit)
        ).all()
    return [{k: getattr(r, k) for k in _FIELDS} for r in rows]


def save_preset(name: str, universe: str = "", board: str = "", industry: str = "") -> int:
    """保存预设. 重名覆盖(同名视为更新). 返回 id."""
    name = (name or "").strip()
    with db.session_scope() as s:
        row = s.exec(
            select(ScreenerPreset).where(ScreenerPreset.name == name)
        ).first()
        if row is None:
            row = ScreenerPreset(name=name)
            s.add(row)
        row.universe = (universe or "").strip()
        row.board = (board or "").strip()
        row.industry = (industry or "").strip()
        s.commit()
        s.refresh(row)
        return row.id or 0


def delete_preset(preset_id: int) -> bool:
    with db.session_scope() as s:
        row = s.get(ScreenerPreset, preset_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True
