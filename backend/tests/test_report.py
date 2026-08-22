"""测试: 盘后 AI 日报 - LLM 链 + 素材组装 + 降级模板 + 落库推送 + 定时任务."""

from __future__ import annotations

import asyncio
import json

from app.core.report.chain import DailyReportOutput
from app.core.report.service import DailyReportService, report_service
from app.models.models import DailyReport, Notification, SignalRecord, Trade
from sqlmodel import Session, select


def _mk_output(**over: dict) -> dict:
    base = {
        "market_summary": "沪深300 多头排列, 闸门看多",
        "trade_summary": "买入 300139 首仓 50%",
        "holdings_review": ["300139 浮盈+2.3%, 量能正常"],
        "signals_today": ["600111 加仓信号(强度75)"],
        "tomorrow_watch": ["300139 止损线 36.58, 跌破需处理"],
        "risk_notes": ["日亏损熔断未触发"],
        "discipline_score": 78,
    }
    base.update(over or {})
    return base


# ---------------------------------------------------------------- LLM 链
def test_report_output_parse_clamp():
    """Pydantic 解析 + discipline_score 越界收敛到 0-100."""
    from langchain_core.output_parsers import PydanticOutputParser

    parser = PydanticOutputParser(pydantic_object=DailyReportOutput)
    out = parser.parse(json.dumps(_mk_output(discipline_score=150), ensure_ascii=False))
    assert out.discipline_score == 100
    assert len(out.tomorrow_watch) == 1


def test_chain_run_with_memory(monkeypatch):
    """日报链: 素材与记忆注入 prompt, 结构化输出解析成功."""
    from app.core.report import chain as chain_mod
    from langchain_core.messages import AIMessage

    captured: list[str] = []

    class FakeLLM:
        model_name = "fake-model"

        async def ainvoke(self, messages):
            captured.append(messages[-1].content)
            return AIMessage(content=json.dumps(_mk_output(), ensure_ascii=False))

    monkeypatch.setattr("app.core.ai_review.chain.build_chain_llm", lambda cfg: FakeLLM())
    out, model = asyncio.run(chain_mod.run_report_chain(
        {"text": "素材文本"}, {"api_key": "x"}, memory_lines="历史记忆行"))
    assert out.discipline_score == 78
    assert model == "fake-model"
    assert "素材文本" in captured[0]
    assert "历史记忆行" in captured[0]


def test_chain_requires_api_key(monkeypatch):
    """未配置 Key -> 抛 LLMError(由 service 降级模板)."""
    import pytest
    from app.core.ai_review.llm import LLMError
    from app.core.report import chain as chain_mod

    with pytest.raises(LLMError):
        asyncio.run(chain_mod.run_report_chain({"text": "x"}, {"api_key": ""}))


# ---------------------------------------------------------------- 素材组装(纯函数)
def test_trades_text():
    trades = [Trade(time="2026-08-10 10:02:00", symbol="300139", name="测试", action="buy",
                    price=38.5, qty=100, amount=3850)]
    text = DailyReportService._trades_text(trades)
    assert "买入" in text and "300139" in text and "38.50" in text


def test_signals_text():
    sigs = [SignalRecord(time="2026-08-10 10:00:00", symbol="300139", name="测试",
                         type="BUY_FIRST", direction="", strength=82, reason="放量突破")]
    text = DailyReportService._signals_text(sigs)
    assert "首仓信号" in text and "强度82" in text


def test_holdings_text():
    items = [{"symbol": "300139", "name": "测试", "qty": 100, "cost": 38.5, "price": 39.4,
              "pnl_pct": 2.34, "stop_line": 36.58, "dist_to_stop_pct": 7.7,
              "tp_targets": [39.8, 40.9]}]
    text = DailyReportService._holdings_text(items)
    assert "止损线36.58" in text
    assert "止盈档: 39.80 / 40.90" in text


def test_build_report_text():
    text = DailyReportService.build_report_text("看多", "- 无", "- 无持仓", "- 无",
                                                "熔断未触发", "- 无纪律问题")
    assert "【市况】看多" in text
    assert "【持仓】" in text and "【纪律诊断】" in text


def test_template_report():
    content = DailyReportService.template_report("2026-08-10", "素材")
    assert "规则模板" in content["text"]
    assert "2026-08-10" in content["text"]


# ---------------------------------------------------------------- 服务主流程
def test_service_generate_degraded(tmp_engine, monkeypatch):
    """无 LLM 配置 -> 规则模板日报落库 + 站内通知."""
    from app import db

    async def fake_collect(self, date, session):
        return {"trades": [], "signals": [], "holdings": [],
                "portfolio": {"total_pct": 0.0},
                "risk": {"day_loss_tripped": False, "defense_mode": False,
                         "consecutive_losses": 0,
                         "config": {"consecutive_loss_limit": 3, "total_position_pct": 80}},
                "gate": {}, "issues": []}

    monkeypatch.setattr(DailyReportService, "_collect", fake_collect)
    report = asyncio.run(report_service.generate("2026-08-10"))
    assert report.date == "2026-08-10"
    assert report.status == "degraded"
    assert report.prompt_version == ""  # 降级模板无 prompt 版本
    content = json.loads(report.content_json)
    assert "规则模板" in content["text"]

    with Session(db.engine) as s:
        notif = s.exec(select(Notification)).first()
        assert notif is not None
        assert notif.title == "交易日报 2026-08-10"
        assert notif.read is False


def test_service_generate_llm_path(tmp_engine, monkeypatch):
    """LLM 启用 -> 结构化日报落库(status=ok)."""
    from app.core.config import config_manager

    async def fake_collect(self, date, session):
        return {"trades": [], "signals": [], "holdings": [],
                "portfolio": {"total_pct": 0.0},
                "risk": {"day_loss_tripped": False, "defense_mode": False,
                         "consecutive_losses": 0,
                         "config": {"consecutive_loss_limit": 3, "total_position_pct": 80}},
                "gate": {}, "issues": []}

    async def fake_memory(query, k=2):
        return "历史记忆"

    async def fake_chain(materials, llm_cfg, memory_lines=""):
        from app.core.report.chain import DailyReportOutput

        return DailyReportOutput(**_mk_output()), "fake-model"

    monkeypatch.setattr(DailyReportService, "_collect", fake_collect)
    monkeypatch.setattr("app.core.ai_review.memory.memory_context", fake_memory)
    monkeypatch.setattr("app.core.report.chain.run_report_chain", fake_chain)
    monkeypatch.setattr(config_manager, "get", lambda: {"llm": {"enabled": True, "api_key": "x"},
                                                        "日报": {"enabled": True, "push_webhook": ""}})

    report = asyncio.run(report_service.generate("2026-08-10"))
    assert report.status == "ok"
    content = json.loads(report.content_json)
    assert content["market_summary"] == "沪深300 多头排列, 闸门看多"
    assert content["discipline_score"] == 78
    assert report.model == "fake-model"
    from app.core.report.chain import REPORT_PROMPT_V

    assert report.prompt_version == REPORT_PROMPT_V  # LLM 路径带 prompt 版本


def test_service_generate_same_date_overwrites(tmp_engine, monkeypatch):
    """同日重复生成 -> 覆盖旧版(不产生重复记录)."""
    from app import db

    async def fake_collect(self, date, session):
        return {"trades": [], "signals": [], "holdings": [],
                "portfolio": {"total_pct": 0.0},
                "risk": {"day_loss_tripped": False, "defense_mode": False,
                         "consecutive_losses": 0,
                         "config": {"consecutive_loss_limit": 3, "total_position_pct": 80}},
                "gate": {}, "issues": []}

    monkeypatch.setattr(DailyReportService, "_collect", fake_collect)
    asyncio.run(report_service.generate("2026-08-10"))
    asyncio.run(report_service.generate("2026-08-10"))
    with Session(db.engine) as s:
        rows = s.exec(select(DailyReport)).all()
        assert len(rows) == 1


# ---------------------------------------------------------------- 通知与调度
def test_push_notification_station(tmp_engine):
    from app import db
    from app.core.report.notify import list_notifications, mark_read, push_notification

    row = asyncio.run(push_notification("report", "标题", "内容"))
    assert row.id is not None
    with Session(db.engine) as s:
        rows = list_notifications(10, session=s)
        assert len(rows) == 1
        updated = mark_read(rows[0].id, s)
        assert updated is not None and updated.read is True


def test_scheduler_registers_daily_report():
    from app.scheduler import scheduler, setup_jobs

    setup_jobs()
    assert scheduler.get_job("daily_report") is not None
    assert scheduler.get_job("after_close_warmup") is not None
