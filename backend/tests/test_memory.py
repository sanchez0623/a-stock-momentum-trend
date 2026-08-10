"""测试: 复盘记忆 RAG - 记忆文本构造 + 索引 + 余弦检索 + 链注入."""

from __future__ import annotations

import asyncio
import json

from app.core.ai_review import memory as memory_mod
from app.core.ai_review.chain import ReviewOutput
from app.models.models import AiReview, ReviewMemory
from sqlmodel import select


def _mk_review(rule_result: dict, suggestions: list[dict], time: str = "2026-08-05 10:00:00",
               content: str = "复盘") -> AiReview:
    return AiReview(time=time, range="week", content=content,
                    suggestions_json=json.dumps(suggestions, ensure_ascii=False),
                    rule_result_json=json.dumps(rule_result, ensure_ascii=False))


def _mk_stats(win_rate: float, total_pnl: float, closed: int = 5) -> dict:
    return {"trades": 10, "closed": closed, "wins": 3, "win_rate": win_rate,
            "total_pnl": total_pnl, "buy_stages": {}}


# ---------------------------------------------------------------- 记忆文本构造
def test_build_memory_text_with_effect():
    review = _mk_review(
        {"issues": [{"level": "high", "title": "追高买入", "detail": "x"},
                    {"level": "high", "title": "追高买入", "detail": "y"},
                    {"level": "medium", "title": "止损未执行", "detail": "z"}],
         "stats": _mk_stats(40.0, -120.0)},
        [{"text": "下调RSI超买线", "patch": {"group": "动量", "key": "rsi_overbought",
                                           "from": 70, "to": 65}, "status": "accepted"},
         {"text": "严格执行止损", "status": "pending"}],
    )
    text = memory_mod.build_memory_text(review, {0: "active"}, _mk_stats(55.0, 800.0))
    assert "追高买入×2" in text
    assert "止损未执行×1" in text
    assert "动量.rsi_overbought 70→65" in text
    assert "已采纳生效" in text
    assert "胜率40.0%→55.0%" in text
    assert "盈亏-120→800" in text


def test_build_memory_text_reverted_suggestion():
    review = _mk_review({"issues": [], "stats": {}},
                        [{"text": "调整ADX", "patch": {"group": "趋势", "key": "adx_threshold",
                                                      "from": 25, "to": 30}, "status": "accepted"}])
    text = memory_mod.build_memory_text(review, {0: "reverted"})
    assert "已撤销" in text
    assert "采纳后表现" not in text  # 无后续复盘 stats


def test_build_memory_text_no_suggestions():
    review = _mk_review({"issues": [], "stats": {}}, [])
    text = memory_mod.build_memory_text(review, {})
    assert "建议: 无" in text


# ---------------------------------------------------------------- 索引与检索
def test_index_review_disabled_when_embedding_off(monkeypatch, tmp_engine):
    """embedding 未启用 -> 不落库返回 False."""
    monkeypatch.setattr(memory_mod, "_embedding_cfg", lambda: {"enabled": False, "api_key": ""})
    review = _mk_review({"issues": [], "stats": {}}, [])
    assert asyncio.run(memory_mod.index_review(review)) is False


def test_index_review_creates_memory(monkeypatch, tmp_engine):
    """embedding 启用 + mock 客户端 -> 记忆条目落库."""
    monkeypatch.setattr(memory_mod, "_embedding_cfg", lambda: {
        "enabled": True, "api_key": "sk-test", "base_url": "https://x/v1",
        "model": "BAAI/bge-m3", "timeout_sec": 10})
    monkeypatch.setattr(memory_mod, "build_embedding_client", lambda cfg: FakeEmb())

    from app import db
    from sqlmodel import Session

    with Session(db.engine) as s:
        r = _mk_review({"issues": [{"level": "high", "title": "追高买入", "detail": "x"}], "stats": {}},
                       [{"text": "下调RSI超买线", "status": "pending"}])
        s.add(r)
        s.commit()
        rid = r.id

    assert asyncio.run(memory_mod.index_review(r)) is True
    with Session(db.engine) as s:
        mem = s.exec(select(ReviewMemory).where(ReviewMemory.review_id == rid)).first()
        assert mem is not None
        assert "追高买入" in mem.text
        assert len(json.loads(mem.embedding_json)) == 3  # FakeEmb 返回 [1,0,0]


class FakeEmb:
    """假 embedding 客户端: 固定 3 维向量, 按文本内容区分方向."""

    async def embed_one(self, text: str) -> list[float]:
        if "止损" in text:
            return [0.0, 1.0, 0.0]
        return [1.0, 0.0, 0.0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_one(t) for t in texts]


def test_search_memories_returns_most_similar(monkeypatch, tmp_engine):
    """余弦检索: 与查询最相似(追高类)的记忆排最前."""
    monkeypatch.setattr(memory_mod, "_embedding_cfg", lambda: {
        "enabled": True, "api_key": "sk-test", "base_url": "https://x/v1",
        "model": "BAAI/bge-m3", "timeout_sec": 10})
    monkeypatch.setattr(memory_mod, "build_embedding_client", lambda cfg: FakeEmb())

    from app import db
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(ReviewMemory(review_id=1, time="2026-08-01 10:00:00",
                           text="追高买入", embedding_json=json.dumps([1.0, 0.0, 0.0])))
        s.add(ReviewMemory(review_id=2, time="2026-08-02 10:00:00",
                           text="止损执行", embedding_json=json.dumps([0.0, 1.0, 0.0])))
        s.commit()

    hits = asyncio.run(memory_mod.search_memories("追高问题", k=2))
    assert len(hits) == 2
    assert "追高买入" in hits[0]["text"]  # 相似度最高排最前
    assert hits[0]["score"] >= hits[1]["score"]


def test_memory_context_empty_when_disabled(monkeypatch, tmp_engine):
    monkeypatch.setattr(memory_mod, "_embedding_cfg", lambda: {"enabled": False, "api_key": ""})
    assert asyncio.run(memory_mod.memory_context("追高")) == ""


# ---------------------------------------------------------------- 链注入
def test_chain_injects_memory(monkeypatch):
    """两步链 Step2 prompt 注入历史记忆文本."""
    from app.core.ai_review import chain as chain_mod
    from langchain_core.messages import AIMessage

    captured: list[str] = []

    class FakeLLM:
        model_name = "fake-model"

        async def ainvoke(self, messages):
            user = messages[-1].content
            captured.append(user)
            step1 = json.dumps({"behavior_summary": "追高", "key_patterns": ["追高"],
                                "discipline_issues": []}, ensure_ascii=False)
            step2 = json.dumps({"analysis": "a", "suggestions": [{"text": "s"}],
                                "discipline_score": 60}, ensure_ascii=False)
            content = step1 if len(captured) == 1 else step2
            return AIMessage(content=content)

    monkeypatch.setattr(chain_mod, "build_chain_llm", lambda cfg: FakeLLM())
    memory_lines = "- [2026-08-01] 复盘: 追高买入×2 | 建议: 下调RSI超买线(已采纳生效) | 胜率40%→55%"
    out, _ = asyncio.run(chain_mod.run_review_chain([], [], [], {}, {"api_key": "x"},
                                                    memory_lines=memory_lines))
    assert isinstance(out, ReviewOutput)
    assert memory_lines in captured[1]  # Step2 包含记忆
    assert "历史复盘记忆" in captured[1]
    assert "历史复盘记忆" not in captured[0]  # Step1 不注入记忆


def test_chain_without_memory(monkeypatch):
    """不传记忆时 Step2 不出现记忆段落."""
    from app.core.ai_review import chain as chain_mod
    from langchain_core.messages import AIMessage

    captured: list[str] = []

    class FakeLLM:
        model_name = "fake-model"

        async def ainvoke(self, messages):
            captured.append(messages[-1].content)
            step1 = json.dumps({"behavior_summary": "b", "key_patterns": [],
                                "discipline_issues": []}, ensure_ascii=False)
            step2 = json.dumps({"analysis": "a", "suggestions": [{"text": "s"}],
                                "discipline_score": 60}, ensure_ascii=False)
            return AIMessage(content=step1 if len(captured) == 1 else step2)

    monkeypatch.setattr(chain_mod, "build_chain_llm", lambda cfg: FakeLLM())
    asyncio.run(chain_mod.run_review_chain([], [], [], {}, {"api_key": "x"}))
    assert "历史复盘记忆" not in captured[1]


# ---------------------------------------------------------------- 复盘服务集成
def test_review_run_indexes_memory(tmp_engine, monkeypatch):
    """复盘 run() 落库后自动调用记忆索引(未启用 embedding 时静默跳过)."""
    from app import db
    from app.core.ai_review import service as service_mod
    from app.models.models import Trade
    from sqlmodel import Session

    called: list[int] = []

    async def fake_index(review, session=None):
        called.append(review.id)
        return False

    monkeypatch.setattr(memory_mod, "index_review", fake_index)
    with Session(db.engine) as s:
        s.add(Trade(time="2026-08-06 10:00:00", symbol="300139", name="", action="buy",
                    price=10, qty=100, amount=1000, pnl=None, reason=""))
        s.commit()

    review = asyncio.run(service_mod.ReviewService().run("week"))
    assert review.id in called  # 复盘后尝试建索引
