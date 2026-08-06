"""依赖注入."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session

from app.db import engine


def get_session() -> Iterator[Session]:
    """FastAPI 依赖: 每请求一个 Session."""
    with Session(engine) as session:
        yield session
