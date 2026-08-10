"""四期测试: AI 复盘 - 规则诊断 + 复盘服务."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from app.core.ai_review import service as service_mod
from app.core.ai_review.chain import BehaviorProfile, LangChainUnavailable, ReviewOutput, Suggestion
from app.core.ai_review.llm import LLMError
from app.core.ai_review.rules import diagnose
from app.core.ai_review.service import ReviewService
from app.models.models import AiReview, SignalRecord, Trade


def _mk_trade(symbol, action, price, qty, time, pnl=None, reason=""):
    return Trade(time=time, symbol=symbol, name="", action=action, price=price,
                 qty=qty, amount=price * qty, pnl=pnl, reason=reason)


def _mk_signal(symbol, type_, time, strength=80):
    return SignalRecord(time=time, symbol=symbol, type=type_, direction="", strength=strength)


# ---------------------------------------------------------------- 规则诊断
def test_rule_stop_loss_ignored():
    trades = [_mk_trade("300139", "buy", 10, 100, "2026-08-01 10:00:00")]
    signals = [_mk_signal("300139", "SELL_STOP", "2026-08-03 14:00:00", 90)]
    issues = diagnose(trades, signals)
    codes = {i["code"] for i in issues}
    assert "stop_loss_ignored" in codes
    assert issues[0]["level"] == "high"


def test_rule_stop_loss_executed_ok():
    trades = [
        _mk_trade("300139", "buy", 10, 100, "2026-08-01 10:00:00"),
        _mk_trade("300139", "sell", 9.5, 100, "2026-08-04 10:00:00", pnl=-50),
    ]
    signals = [_mk_signal("300139", "SELL_STOP", "2026-08-03 14:00:00", 90)]
    issues = diagnose(trades, signals)
    assert all(i["code"] != "stop_loss_ignored" for i in issues)


def test_rule_over_trading():
    trades = [
        _mk_trade("300139", "buy", 10, 100, "2026-08-03 09:35:00"),
        _mk_trade("300139", "sell", 10.1, 100, "2026-08-03 10:00:00", pnl=10),
        _mk_trade("300139", "buy", 10.2, 100, "2026-08-03 11:00:00"),
        _mk_trade("300139", "sell", 10.0, 100, "2026-08-03 14:00:00", pnl=-20),
    ]
    issues = diagnose(trades, [])
    assert any(i["code"] == "over_trading" for i in issues)


def test_rule_chase_high_and_counter_trend():
    import pandas as pd

    df = pd.DataFrame({
        "date": ["2026-08-03", "2026-08-04", "2026-08-05"],
        "open": [9.0, 9.1, 9.2],
        "high": [9.5, 9.6, 9.7],
        "low": [8.8, 8.9, 9.0],
        "close": [9.2, 9.3, 9.4],
        "volume": [1000, 1000, 1000],
        "amount": [9000, 9000, 9000],
        "ma5": [8.5, 8.6, 8.7],
        "ma10": [9.0, 9.1, 9.2],
        "ma20": [9.5, 9.6, 9.7],
    })
    trades = [_mk_trade("300139", "buy", 9.6, 100, "2026-08-05 10:00:00", reason="")]  # 高于收盘9.4, MA空头
    issues = diagnose(trades, [], klines={"300139": df})
    codes = {i["code"] for i in issues}
    assert "chase_high" in codes
    assert "counter_trend" in codes


# ---------------------------------------------------------------- 趋势阶段诊断(方案B)
def _kline_df(close_list):
    import pandas as pd

    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = np.full(len(close), 5_000_000.0)
    amount = volume * close
    dates = pd.bdate_range("2026-03-02", periods=len(close))
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount,
    })


def _last_date(df) -> str:
    return str(df["date"].iloc[-1])


def test_rule_buy_on_overheat_stage():
    """买入日趋势处于过热期 -> 检出 buy_overheat."""
    base = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)]
    df = _kline_df(base + [150, 155, 160, 165, 170, 175, 180, 185, 190, 195, 200, 205])
    day = _last_date(df)
    close = float(df["close"].iloc[-1])
    trades = [_mk_trade("300139", "buy", close, 100, f"{day} 10:00:00")]
    issues = diagnose(trades, [], klines={"300139": df})
    assert any(i["code"] == "buy_overheat" for i in issues)


def test_rule_buy_on_exhaust_stage():
    """买入日趋势处于衰竭期 -> 检出 buy_exhaust."""
    base = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)]
    df = _kline_df(base + [150, 157, 164, 172, 180, 189, 198, 208, 215, 213])
    day = _last_date(df)
    close = float(df["close"].iloc[-1])
    trades = [_mk_trade("300139", "buy", close, 100, f"{day} 10:00:00")]
    issues = diagnose(trades, [], klines={"300139": df})
    assert any(i["code"] == "buy_exhaust" for i in issues)


def test_stage_stats_links_closed_pnl():
    """stage_stats: 阶段分布统计 + 平仓盈亏按最近买入关联."""
    from app.core.ai_review.rules import stage_stats

    base = [100 + i * 0.25 + np.sin(i / 6) * 2 for i in range(170)]
    over_df = _kline_df(base + [150, 155, 160, 165, 170, 175, 180, 185, 190, 195, 200, 205])
    over_day, over_close = _last_date(over_df), float(over_df["close"].iloc[-1])
    # 启动行情: 震荡后温和突破
    flat = [100 + np.sin(i / 3) * 4 + np.sin(i / 11) * 2 for i in range(150)]
    launch_df = _kline_df(flat + [101, 102.2, 103.6, 105.2])
    launch_day, launch_close = _last_date(launch_df), float(launch_df["close"].iloc[-1])

    trades = [
        _mk_trade("600001", "buy", launch_close, 100, f"{launch_day} 10:00:00"),   # 启动期买入
        _mk_trade("600001", "sell", launch_close * 1.1, 100, f"{launch_day} 10:00:01",
                  pnl=launch_close * 0.1 * 100),                                   # 盈利平仓 -> 归 launch
        _mk_trade("300139", "buy", over_close, 100, f"{over_day} 10:00:00"),       # 过热期买入
    ]
    stats = stage_stats(trades, klines={"600001": launch_df, "300139": over_df})
    assert stats.get("launch", {}).get("n") == 1
    assert stats.get("overheat", {}).get("n") == 1
    assert stats["launch"]["closed"] == 1 and stats["launch"]["wins"] == 1
    assert stats["launch"]["win_rate"] == 100.0
    assert stats["launch"]["pnl"] > 0


def test_parse_llm_output_json():
    text = '```json\n{"analysis": "追高明显", "suggestions": [{"text": "买入前看当日均价"}], "discipline_score": 60}\n```'
    content, suggestions = ReviewService._parse_llm_output(text)
    assert "追高" in content
    assert len(suggestions) == 1
    assert suggestions[0]["status"] == "pending"


def test_parse_llm_output_plain_text():
    content, suggestions = ReviewService._parse_llm_output("第一条建议\n第二条建议")
    assert suggestions  # 拆行兜底


def test_review_day_scope_only_today(tmp_engine):
    """day 范围只统计今天的交易, 历史交易不计入."""
    import datetime as dt

    from app import db
    from app.core.ai_review.service import ReviewService
    from sqlmodel import Session

    today = dt.date.today().isoformat()
    with Session(db.engine) as s:
        s.add(_mk_trade("300139", "buy", 10, 100, f"{today} 10:00:00"))
        s.add(_mk_trade("600000", "buy", 10, 100, "2026-01-01 10:00:00"))  # 旧交易, 不应计入
        s.commit()

    review = asyncio.run(ReviewService().run("day"))
    assert review.range == "day"
    result = json.loads(review.rule_result_json)
    assert result["stats"]["trades"] == 1  # 仅今日一笔


def test_review_scope_range_mapping():
    """范围解析: day 精确到今日; week/month/all 各自正确边界."""
    import datetime as dt

    today = dt.date.today()
    start, end = ReviewService._scope_range("day")
    assert start == end == today.isoformat()
    start, end = ReviewService._scope_range("week")
    assert start == (today - dt.timedelta(days=today.weekday())).isoformat()
    assert end == today.isoformat()
    start, end = ReviewService._scope_range("month")
    assert start == today.replace(day=1).isoformat()
    assert ReviewService._scope_range("all") == ("", "")
    assert ReviewService._scope_range("2026-08-01..2026-08-07") == ("2026-08-01", "2026-08-07")


# ---------------------------------------------------------------- 复盘服务
def test_review_run_without_llm(tmp_engine):
    import asyncio

    from app import db
    from app.core.ai_review.service import ReviewService
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(_mk_trade("300139", "buy", 10, 100, "2026-08-06 10:00:00"))
        s.commit()

    review = asyncio.run(ReviewService().run("week"))
    assert isinstance(review, AiReview)
    assert "规则诊断" in review.content or review.rule_result_json
    result = json.loads(review.rule_result_json)
    assert "issues" in result
    assert "stats" in result


def test_review_history_and_suggestion(tmp_engine):
    from app import db
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(AiReview(range="week", content="测试复盘", suggestions_json='[{"text":"a","status":"pending"}]', rule_result_json="{}"))
        s.commit()
    service = ReviewService()
    rows = service.history()
    assert len(rows) == 1
    updated, info = service.update_suggestion(rows[0].id, 0, "accepted")
    items = json.loads(updated.suggestions_json)
    assert items[0]["status"] == "accepted"
    # 纯文字建议(无 patch): 只打标记, 不应改动任何配置
    assert info["applied"] is False


# ---------------------------------------------------------------- LangChain 两步链
def test_chain_review_output_parse():
    """PydanticOutputParser 解析 ReviewOutput, discipline_score 越界收敛到 0-100."""
    from langchain_core.output_parsers import PydanticOutputParser

    parser = PydanticOutputParser(pydantic_object=ReviewOutput)
    text = json.dumps({
        "analysis": "追高明显, 止损执行及时",
        "suggestions": [
            {"text": "买入前看当日均价",
             "patch": {"group": "动量", "key": "rsi_overbought", "to": 65}},
            {"text": "严格执行止损"},
        ],
        "discipline_score": 150,
    }, ensure_ascii=False)
    out = parser.parse(text)
    assert out.analysis
    assert len(out.suggestions) == 2
    assert out.suggestions[0].patch.key == "rsi_overbought"
    assert out.suggestions[1].patch is None
    assert out.discipline_score == 100  # 越界收敛


def test_chain_behavior_profile_parse():
    """Step1 输出模型: 行为特征归纳结构."""
    profile = BehaviorProfile.model_validate({
        "behavior_summary": "追高频繁, 止损果断",
        "key_patterns": ["追高买入", "止损执行及时"],
        "discipline_issues": [],
    })
    assert len(profile.key_patterns) == 2


def test_llm_review_chain_conversion(monkeypatch):
    """两步链输出 -> suggestions 结构; 白名单外 patch(风控分组)降级为纯文字."""
    async def fake_run_chain(trades, signals, issues, stats, llm_cfg, memory_lines=""):
        return ReviewOutput(
            analysis="测试分析",
            suggestions=[
                Suggestion(text="调整ADX门槛",
                           patch={"group": "趋势", "key": "adx_threshold", "to": 30}),
                Suggestion(text="风控参数建议",
                           patch={"group": "风控", "key": "stop_loss_pct", "to": 3.0}),
                Suggestion(text="心态建议"),
            ],
            discipline_score=70,
        ), "deepseek-chat"

    monkeypatch.setattr("app.core.ai_review.chain.run_review_chain", fake_run_chain)
    content, suggestions, model = asyncio.run(
        service_mod.ReviewService._llm_review([], [], [], {}, {"api_key": "x"}))
    assert content == "测试分析"
    assert model == "deepseek-chat"
    by_text = {s["text"]: s for s in suggestions}
    # 白名单内 patch 保留
    assert by_text["调整ADX门槛"]["patch"]["group"] == "趋势"
    assert by_text["调整ADX门槛"]["patch"]["key"] == "adx_threshold"
    # 风控分组(禁止) -> 降级纯文字并注明原因
    assert by_text["风控参数建议"]["guard"] == "not_whitelisted"
    assert "patch" not in by_text["风控参数建议"]
    # 纯文字建议正常
    assert by_text["心态建议"]["status"] == "pending"
    assert "patch" not in by_text["心态建议"]


def test_llm_review_fallback_legacy(monkeypatch):
    """langchain 不可用(抛 LangChainUnavailable) -> 降级旧单步 prompt 路径."""
    async def fake_run_chain(*args, **kwargs):
        raise LangChainUnavailable("langchain 未安装")

    class FakeClient:
        model = "deepseek-chat"

        async def chat(self, messages):
            return json.dumps({
                "analysis": "降级分析",
                "suggestions": [{"text": "降级建议"}],
                "discipline_score": 50,
            }, ensure_ascii=False)

    monkeypatch.setattr("app.core.ai_review.chain.run_review_chain", fake_run_chain)
    monkeypatch.setattr(service_mod, "build_client_from_config", lambda cfg: FakeClient())
    stats = {"closed": 0, "win_rate": 0.0, "total_pnl": 0.0}
    content, suggestions, model = asyncio.run(
        service_mod.ReviewService._llm_review([], [], [], stats, {"api_key": "x"}))
    assert "降级分析" in content
    assert suggestions and suggestions[0]["text"] == "降级建议"
    assert model == "deepseek-chat"


def test_llm_review_llm_error_propagates(monkeypatch):
    """两步链 LLM 请求失败 -> 抛 LLMError, 由 run() 统一降级为纯规则诊断."""
    async def boom(*args, **kwargs):
        raise LLMError("模拟 LLM 不可用")

    monkeypatch.setattr("app.core.ai_review.chain.run_review_chain", boom)
    with pytest.raises(LLMError):
        asyncio.run(service_mod.ReviewService._llm_review([], [], [], {}, {"api_key": "x"}))
