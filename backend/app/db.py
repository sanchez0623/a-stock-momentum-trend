"""SQLite 数据库引擎与会话."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

# 数据目录: env DATA_DIR > 默认 ./data
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trading.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


def init_db() -> None:
    """建表(幂等)."""
    from app.models import models  # noqa: F401  # 确保模型注册到 metadata

    SQLModel.metadata.create_all(engine)
    logger.info("SQLite 就绪: %s", DB_PATH)


@contextmanager
def session_scope() -> Iterator[Session]:
    """上下文管理器: with db.session_scope() as s: ..."""
    with Session(engine) as session:
        yield session


def get_session() -> Iterator[Session]:
    """FastAPI 依赖: 每请求一个 Session."""
    with Session(engine) as session:
        yield session
