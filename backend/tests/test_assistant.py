"""测试: AI 助理 - LangGraph 流水线(节点流转/去重/降级/调度)."""

from __future__ import annotations

import asyncio

from app.core.assistant import nodes
from app.core.assistant.pipeline import build_graph, run_phase
from app.core.assistant.scheduler import setup_assistant_jobs, teardown_assistant_jobs
from app.models.models import Notification
from sqlmodel import Session, select


def _sig(symbol: str = "300139", type_: str = "SELL_REDUCE", strength: float = 60,
         reason: str = "触及止盈档") -> dict:
    return {"symbol": symbol, "name": "测试", "type": type_, "direction": "sell",
            "strength": strength, "reason": reason, "price": 52.5}


# ---------------------------------------------------------------- 指纹与去重
def test_fingerprint_format():
    assert nodes.fingerprint(_sig(), "2026-08-12") == "2026-08-12:300139:SELL_REDUCE"


def test_dedupe_filters_pushed(tmp_engine):
    """已推送指纹(同日同类型)被过滤, 新信号保留."""
    from app import db

    with Session(db.engine) as s:
        s.add(Notification(category="assistant", title="t", content="c",
                           fingerprint="2026-08-12:300139:SELL_REDUCE"))
        s.commit()
    state = {"date": "2026-08-12",
             "signals": [_sig("300139", "SELL_REDUCE"), _sig("600111", "BUY_FIRST", 80)]}
    out = asyncio.run(nodes.dedupe(state))
    assert len(out["fresh"]) == 1
    assert out["fresh"][0]["symbol"] == "600111"


# ---------------------------------------------------------------- 图流转
def test_graph_route_and_flow():
    """after_close 走 daily_report; premarket/intraday 走 collect 链."""
    from langgraph.checkpoint.memory import MemorySaver

    calls: list[str] = []

    def mk(name: str):
        async def node(state: dict) -> dict:
            calls.append(name)
            return {}
        return node

    overrides = {"collect": mk("collect"), "dedupe": mk("dedupe"),
                 "narrate": mk("narrate"), "notify": mk("notify"),
                 "daily_report": mk("daily_report")}
    graph = build_graph(MemorySaver(), overrides)

    asyncio.run(graph.ainvoke({"phase": "intraday", "date": "2026-08-12"},
                              {"configurable": {"thread_id": "t1"}}))
    assert calls == ["collect", "dedupe", "narrate", "notify"]

    calls.clear()
    asyncio.run(graph.ainvoke({"phase": "after_close", "date": "2026-08-12"},
                              {"configurable": {"thread_id": "t2"}}))
    assert calls == ["daily_report"]


def test_run_phase_validates():
    import pytest

    with pytest.raises(ValueError):
        asyncio.run(run_phase("bad_phase"))


# ---------------------------------------------------------------- 解说降级
def test_narrate_falls_back_to_template(monkeypatch, tmp_engine):
    """LLM 失败 -> 规则模板解说(PlanGenerator 文案), 不抛错."""
    async def boom(*a, **kw):
        raise RuntimeError("模拟 LLM 不可用")

    monkeypatch.setattr("app.core.ai_review.chain._call_parsed", boom)
    state = {"phase": "intraday", "date": "2026-08-12",
             "fresh": [_sig("300139", "SELL_REDUCE")], "market": {}}
    out = asyncio.run(nodes.narrate(state))
    assert out["insights"]
    assert out["insights"][0]["symbol"] == "300139"
    assert out["insights"][0]["text"]


def test_narrate_skips_when_no_fresh():
    out = asyncio.run(nodes.narrate({"fresh": []}))
    assert out["insights"] == []


# ---------------------------------------------------------------- 通知
def test_notify_writes_with_fingerprint(tmp_engine):
    """notify 落 Notification 并带指纹; 标题含代码/名称/信号类型."""
    from app import db

    state = {"phase": "intraday", "date": "2026-08-12",
             "fresh": [_sig("300139", "SELL_REDUCE"), _sig("600111", "BUY_FIRST", 80)],
             "insights": [{"symbol": "300139", "type": "SELL_REDUCE", "text": "减仓建议"},
                          {"symbol": "600111", "type": "BUY_FIRST", "text": "首仓观察"}]}
    out = asyncio.run(nodes.notify(state))
    assert len(out["notifications"]) == 2
    with Session(db.engine) as s:
        rows = s.exec(select(Notification).where(Notification.category == "assistant")).all()
        assert len(rows) == 2
        assert all(r.fingerprint for r in rows)
        titles = {r.title for r in rows}
        assert any("300139" in t and "减仓信号" in t for t in titles)
        assert any("600111" in t and "首仓信号" in t for t in titles)


# ---------------------------------------------------------------- 调度
def test_scheduler_jobs_register_and_teardown():
    from app.scheduler import scheduler

    teardown_assistant_jobs(scheduler)
    setup_assistant_jobs(scheduler)  # enabled 默认 False -> 不注册
    assert scheduler.get_job("assistant_premarket") is None

    # 开启后注册
    from app.core.config import config_manager

    cfg = config_manager.get()
    cfg["ai_assistant"]["enabled"] = True
    from app.core.config import ConfigManager

    orig_update = ConfigManager.update

    def fake_update(self, partial, persist=True):
        out = orig_update(self, partial, persist=False)
        return out

    try:
        config_manager._cfg = cfg  # 直接写内存(测试内)
        setup_assistant_jobs(scheduler)
        assert scheduler.get_job("assistant_premarket") is not None
        assert scheduler.get_job("assistant_intraday") is not None
        assert scheduler.get_job("assistant_after_close") is not None
    finally:
        teardown_assistant_jobs(scheduler)
        config_manager._cfg["ai_assistant"]["enabled"] = False
