"""测试: LLM 调用记账(LlmCall) - hash/usage 提取/落库/LLMClient 与两步链接入."""

from __future__ import annotations

import asyncio
import json

import pytest
from app import db
from app.core.ai_review import call_log
from app.core.ai_review.chain import ReviewOutput
from app.core.ai_review.llm import LLMClient, LLMError
from app.models.models import LlmCall
from sqlmodel import Session, select


# ---------------------------------------------------------------- 纯函数
def test_hash_prompt_stable_and_sensitive():
    h1 = call_log.hash_prompt("system-a", "user-a")
    assert h1 == call_log.hash_prompt("system-a", "user-a")  # 同内容稳定
    assert h1 != call_log.hash_prompt("system-a", "user-b")
    assert len(h1) == 16


def test_provider_of():
    assert call_log.provider_of("https://api.deepseek.com/v1") == "api.deepseek.com"
    assert call_log.provider_of("http://localhost:11434/v1") == "localhost"
    assert call_log.provider_of("") == "unknown"


def test_extract_usage_openai_json():
    data = {"usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}}
    assert call_log.extract_usage(data) == {"input_tokens": 100, "output_tokens": 25,
                                            "total_tokens": 125}


def test_extract_usage_aimessage_metadata():
    class FakeMsg:
        usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        response_metadata = {}

    assert call_log.extract_usage(FakeMsg())["total_tokens"] == 15


def test_extract_usage_aimessage_response_metadata():
    class FakeMsg:
        usage_metadata = {}
        response_metadata = {"token_usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                             "total_tokens": 10}}

    assert call_log.extract_usage(FakeMsg())["total_tokens"] == 10


def test_extract_usage_missing_safe():
    assert call_log.extract_usage({}) == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert call_log.extract_usage({"usage": {"prompt_tokens": "bad"}})["input_tokens"] == 0


def test_record_call_persists(tmp_engine):
    """落库字段完整."""
    call_log.record_call(feature="unit.test", model="m", provider="p",
                         prompt_hash="h", prompt_version="v1",
                         input_tokens=1, output_tokens=2, total_tokens=3,
                         latency_ms=42, degraded=True, ok=False, error="boom")
    with Session(db.engine) as s:
        row = s.exec(select(LlmCall)).first()
        assert row is not None
        assert row.feature == "unit.test" and row.model == "m" and row.provider == "p"
        assert row.prompt_hash == "h" and row.prompt_version == "v1"
        assert row.input_tokens == 1 and row.output_tokens == 2 and row.total_tokens == 3
        assert row.latency_ms == 42 and row.degraded is True and row.ok is False
        assert row.error == "boom"


# ---------------------------------------------------------------- LLMClient 接入
class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeAsyncClient:
    """假 httpx.AsyncClient: 返回预设响应或抛异常."""

    _resp: object = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _patch_httpx(monkeypatch, resp):
    monkeypatch.setattr("app.core.ai_review.llm.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient._resp = resp


def test_chat_records_usage(tmp_engine, monkeypatch):
    """成功调用: usage/token/延迟/feature/版本 全部落库."""
    _patch_httpx(monkeypatch, FakeResp({
        "choices": [{"message": {"content": "你好"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }))
    client = LLMClient(base_url="https://api.deepseek.com/v1", api_key="sk",
                       model="deepseek-chat")
    text = asyncio.run(client.chat(
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "usr"}],
        feature="ai_review.legacy", prompt_version="v1", degraded=True))
    assert text == "你好"
    with Session(db.engine) as s:
        row = s.exec(select(LlmCall)).first()
        assert row.feature == "ai_review.legacy"
        assert row.model == "deepseek-chat"
        assert row.provider == "api.deepseek.com"
        assert row.prompt_version == "v1"
        assert row.input_tokens == 10 and row.output_tokens == 4 and row.total_tokens == 14
        assert row.degraded is True and row.ok is True
        assert row.error == ""
        # prompt_hash 与同内容重放一致
        assert row.prompt_hash == call_log.hash_prompt("sys", "usr")


def test_chat_records_failure(tmp_engine, monkeypatch):
    """调用失败: ok=False + 错误原因落库, LLMError 照常抛给上层."""
    import httpx

    _patch_httpx(monkeypatch, httpx.TimeoutException("timeout"))
    client = LLMClient(base_url="https://api.deepseek.com/v1", api_key="sk")
    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "x"}],
                                feature="ai_review.legacy"))
    with Session(db.engine) as s:
        row = s.exec(select(LlmCall)).first()
        assert row is not None
        assert row.ok is False and "超时" in row.error


def test_chat_unconfigured_no_row(tmp_engine, monkeypatch):
    """未配置 Key: 抛错但不落库(不是真实调用)."""
    _patch_httpx(monkeypatch, FakeResp({}))
    client = LLMClient(base_url="https://x/v1", api_key="")
    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    with Session(db.engine) as s:
        assert s.exec(select(LlmCall)).first() is None


# ---------------------------------------------------------------- 两步链接入
def test_call_parsed_records_usage(tmp_engine, monkeypatch):
    """_call_parsed: langchain 响应 usage_metadata 记账; 解析失败也记账."""
    from app.core.ai_review import chain as chain_mod
    from langchain_core.messages import AIMessage
    from langchain_core.output_parsers import PydanticOutputParser

    class FakeLLM:
        model_name = "fake-model"
        base_url = "https://api.deepseek.com/v1"

        def __init__(self, content):
            self._content = content

        async def ainvoke(self, messages):
            return AIMessage(content=self._content, usage_metadata={
                "input_tokens": 20, "output_tokens": 8, "total_tokens": 28})

    parser = PydanticOutputParser(pydantic_object=ReviewOutput)
    ok_json = json.dumps({"analysis": "a", "suggestions": [{"text": "s"}],
                          "discipline_score": 60}, ensure_ascii=False)
    out = asyncio.run(chain_mod._call_parsed(
        "system", "user", FakeLLM(ok_json), parser,
        feature="ai_review.step2", prompt_version="v2"))
    assert out.analysis == "a"
    with Session(db.engine) as s:
        row = s.exec(select(LlmCall)).first()
        assert row.feature == "ai_review.step2" and row.prompt_version == "v2"
        assert row.model == "fake-model" and row.provider == "api.deepseek.com"
        assert row.input_tokens == 20 and row.output_tokens == 8 and row.total_tokens == 28
        assert row.ok is True

    # 解析失败(重试耗尽): 记一条 ok=False 的调用
    bad_llm = FakeLLM("不是 JSON")
    with pytest.raises(LLMError):
        asyncio.run(chain_mod._call_parsed(
            "system", "user", bad_llm, parser, retries=0, feature="ai_review.step1"))
    with Session(db.engine) as s:
        rows = list(s.exec(select(LlmCall).order_by(LlmCall.id)).all())
        assert len(rows) == 2
        assert rows[1].ok is False and "解析失败" in rows[1].error
