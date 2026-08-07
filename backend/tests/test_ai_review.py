"""四期测试: AI 复盘 - 规则诊断 + 复盘服务."""

from __future__ import annotations

import json

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


def test_parse_llm_output_json():
    text = '```json\n{"analysis": "追高明显", "suggestions": [{"text": "买入前看当日均价"}], "discipline_score": 60}\n```'
    content, suggestions = ReviewService._parse_llm_output(text)
    assert "追高" in content
    assert len(suggestions) == 1
    assert suggestions[0]["status"] == "pending"


def test_parse_llm_output_plain_text():
    content, suggestions = ReviewService._parse_llm_output("第一条建议\n第二条建议")
    assert suggestions  # 拆行兜底


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
