"""AI 助理 - LangGraph 流水线(StateGraph).

路由: 盘前/盘中走 collect -> dedupe -> narrate -> notify;
      盘后走 daily_report(复用 report 模块, 其内部已推送)。
checkpointer: SqliteSaver 持久化, 崩溃恢复不重复提醒。
节点可注入 overrides(测试用)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.core.assistant import nodes
from app.core.assistant.state import AssistantState, build_checkpointer, thread_id

logger = logging.getLogger(__name__)

PHASES = ("premarket", "intraday", "after_close")


def _route(state: dict[str, Any]) -> str:
    """按 phase 分发: 盘后走日报节点, 其余走素材链路."""
    return "daily_report" if state.get("phase") == "after_close" else "collect"


def build_graph(saver: Any | None = None,
                overrides: dict[str, Callable] | None = None) -> Any:
    """编译状态图. overrides 可替换节点函数(测试注入 mock)."""
    overrides = overrides or {}
    g = StateGraph(AssistantState)
    g.add_node("collect", overrides.get("collect", nodes.collect))
    g.add_node("dedupe", overrides.get("dedupe", nodes.dedupe))
    g.add_node("narrate", overrides.get("narrate", nodes.narrate))
    g.add_node("notify", overrides.get("notify", nodes.notify))
    g.add_node("daily_report", overrides.get("daily_report", nodes.daily_report))
    g.add_conditional_edges(START, _route, {
        "collect": "collect", "daily_report": "daily_report"})
    g.add_edge("collect", "dedupe")
    g.add_edge("dedupe", "narrate")
    g.add_edge("narrate", "notify")
    g.add_edge("notify", END)
    g.add_edge("daily_report", END)
    return g.compile(checkpointer=saver)


async def run_phase(phase: str, date: str | None = None,
                    saver: Any | None = None) -> dict[str, Any]:
    """执行单个阶段流水线(手动触发/定时任务共用).

    同一天同阶段共享 checkpointer 线程(状态持久化, 重复执行不重复提醒)。
    """
    if phase not in PHASES:
        raise ValueError(f"phase 需为 {PHASES}")
    date = date or dt.date.today().isoformat()
    graph = build_graph(saver if saver is not None else await build_checkpointer())
    return await graph.ainvoke(
        {"phase": phase, "date": date},
        {"configurable": {"thread_id": thread_id(phase, date)}},
    )
