"""AI 助理 - LangGraph 状态定义与 checkpointer.

状态经 AsyncSqliteSaver 持久化(独立库文件, 不占用 trading.db):
崩溃/重启后恢复时知道当天已推送过哪些提醒, 不会重复打扰。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


# 状态字段全部可选(节点按需写入)
class AssistantState(TypedDict, total=False):
    phase: str                  # premarket / intraday / after_close
    date: str                   # YYYY-MM-DD
    market: dict[str, Any]      # 市况闸门结果
    symbols: list[str]          # 观察范围(持仓+自选)
    signals: list[dict[str, Any]]  # 规则引擎评估结果(未去重)
    fresh: list[dict[str, Any]]    # 去重后待解说的新信号
    insights: list[dict[str, Any]] # AI 解说/规则模板降级结果
    pushed: list[str]           # 本次已推送指纹
    notifications: list[dict[str, Any]]  # 本次通知记录
    error: str


def _default_db_path() -> Path:
    """backend/data/assistant_checkpoints.db(与 trading.db 同目录, 独立文件)."""
    return Path(__file__).resolve().parents[3] / "data" / "assistant_checkpoints.db"


async def build_checkpointer(path: str | Path | None = None) -> AsyncSqliteSaver:
    """AsyncSqliteSaver: 状态持久化(表 checkpoints / checkpoint_writes).

    graph.ainvoke 为异步调用, 必须用 AsyncSqliteSaver(依赖 aiosqlite)。
    """
    import aiosqlite

    db_path = Path(path or _default_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    return AsyncSqliteSaver(conn)


def thread_id(phase: str, date: str) -> str:
    """按 阶段:日期 的线程标识(同一天同阶段共享状态)."""
    return f"{phase}:{date}"
