"""AI 助理 - 流水线节点(只调用现有服务, 不复制业务逻辑).

节点: collect(素材) -> dedupe(去重) -> narrate(AI 解说/降级) -> notify(推送)
盘后: daily_report 节点复用 report 模块, 不走 collect 链路。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.core.config import config_manager
from app.models.models import Notification, Watchlist

logger = logging.getLogger(__name__)

SIGNAL_LABEL = {
    "BUY_FIRST": "首仓信号", "BUY_ADD": "加仓信号", "SELL_REDUCE": "减仓信号",
    "SELL_STOP": "止损信号", "T_BUY": "做T买入", "T_SELL": "做T卖出",
}

# 信号解说 prompt 版本(修改 narrate 提示词时必须递增; 随 LlmCall.prompt_version 落库)
NARRATE_PROMPT_V = "narrate-2026-08-14-v1"


# ---------------------------------------------------------------- 工具
def _now() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _assistant_cfg() -> dict[str, Any]:
    return config_manager.get().get("ai_assistant", {}) or {}


def fingerprint(signal: dict, date: str) -> str:
    """去重指纹: 日期:symbol:信号类型(同日同类型只提醒一次)."""
    return f"{date}:{signal.get('symbol', '')}:{signal.get('type', '')}"


def _scope_symbols(session) -> list[str]:
    """观察范围: 持仓 + 自选(scope=positions_watchlist)."""
    from app.core.position import position_manager

    positions = position_manager.list_positions(session)
    watch = session.exec(select(Watchlist.symbol)).all()
    return sorted({p.symbol for p in positions} | {str(w[0]) for w in watch if w and w[0]})


# ---------------------------------------------------------------- 节点: collect
async def collect(state: dict[str, Any]) -> dict[str, Any]:
    """收集素材: 市况 + 持仓/自选批量信号评估(不落库)."""
    from app.core.datasource import data_source_manager
    from app.core.market_gate import compute_market_gate, fetch_gate_index_dfs
    from app.core.position import position_manager
    from app.core.signals import SignalEngine
    from app.core.signals.engine import PositionInfo

    cfg = config_manager.get()
    date = state.get("date") or dt.date.today().isoformat()

    with db.session_scope() as s:
        symbols = _scope_symbols(s)

        market: dict[str, Any] = {}
        try:
            market = compute_market_gate(
                await fetch_gate_index_dfs(cfg.get("择时闸门", {}) or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("助理: 市况闸门获取失败 %s", exc, exc_info=True)

        # 批量评估(与 api/signals 同逻辑, 不落库)
        import asyncio

        engine = SignalEngine()
        sem = asyncio.Semaphore(5)

        async def evaluate_one(sym: str) -> dict | None:
            async with sem:
                try:
                    df = await data_source_manager.get_kline(sym, "daily", 120)
                    if df is None or df.empty:
                        return None
                    quotes = await data_source_manager.get_realtime_quote([sym])
                    quote = quotes[0] if quotes else None
                    pos = position_manager.get_position(sym, s)
                    pos_info = (PositionInfo(symbol=sym, cost=pos.cost, qty=pos.qty, peak_price=pos.peak_price)
                                if pos else None)
                    signal = engine.evaluate(
                        sym, name=quote.name if quote else "", kline_df=df,
                        position=pos_info,
                        quote_price=quote.price if quote else None,
                        quote_high=quote.high if quote else None,
                        quote_low=quote.low if quote else None,
                    )
                    if signal is None:
                        return None
                    return {
                        "symbol": sym, "name": signal.name or "",
                        "type": signal.type, "direction": signal.direction,
                        "strength": round(float(signal.strength), 2),
                        "reason": signal.reason,
                        "price": round(float(signal.price or (quote.price if quote else 0)), 2),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.debug("助理评估失败 %s: %s", sym, exc)
                    return None

        signals = [r for r in await asyncio.gather(*(evaluate_one(x) for x in dict.fromkeys(symbols)))
                   if r is not None]

    # 盘前阶段: 顺带生成做T波幅建议(P1, 失败静默降级到盘中规则计算)
    if state.get("phase") == "premarket":
        try:
            from app.core.assistant.t_swing import generate_premarket_swing

            r = await generate_premarket_swing()
            if r.get("ok"):
                logger.info("做T波幅建议: %d/%d 生成成功", r["ok"], r["total"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("做T波幅建议生成失败(降级规则): %s", exc)

    return {"date": date, "symbols": symbols, "market": market, "signals": signals}


# ---------------------------------------------------------------- 节点: dedupe
async def dedupe(state: dict[str, Any]) -> dict[str, Any]:
    """过滤同日同类型已推送的信号(查 Notification.fingerprint)."""
    date = state.get("date") or dt.date.today().isoformat()
    signals = state.get("signals") or []
    if not signals:
        return {"fresh": [], "pushed": []}
    try:
        with db.session_scope() as s:
            existing = {str(r.fingerprint) for r in s.exec(
                select(Notification).where(
                    Notification.category == "assistant",
                    Notification.fingerprint != "",
                )).all()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("助理去重查询失败 %s", exc)
        existing = set()
    fresh = [sig for sig in signals if fingerprint(sig, date) not in existing]
    return {"fresh": fresh, "pushed": [fingerprint(sig, date) for sig in fresh]}


# ---------------------------------------------------------------- 节点: narrate
async def narrate(state: dict[str, Any]) -> dict[str, Any]:
    """AI 解说新信号(一次调用覆盖整批); LLM 失败降级规则模板."""
    fresh = state.get("fresh") or []
    if not fresh:
        return {"insights": []}
    cfg = config_manager.get()
    llm_cfg = cfg.get("llm", {})
    phase = state.get("phase", "intraday")
    insights: list[dict[str, Any]] = []

    if llm_cfg.get("enabled") and llm_cfg.get("api_key"):
        try:
            from langchain_core.output_parsers import PydanticOutputParser
            from pydantic import BaseModel, Field

            from app.core.ai_review.chain import _call_parsed, build_chain_llm
            from app.core.ai_review.memory import memory_context

            class SignalNote(BaseModel):
                symbol: str = Field(description="代码")
                advice: str = Field(description="对该信号的一句话操作建议")

            class NarrationOutput(BaseModel):
                summary: str = Field(description="整批信号的一句话总览")
                items: list[SignalNote] = Field(description="逐信号建议")

            market = state.get("market") or {}
            env = {"bull": "看多", "bear": "看空", "neutral": "中性"}.get(str(market.get("environment")), "未知")
            sig_lines = "\n".join(
                f"- {s['symbol']} {s.get('name', '')} {SIGNAL_LABEL.get(s['type'], s['type'])}"
                f"(强度{s['strength']:.0f}) {s.get('reason', '')} 现价{s.get('price', 0)}"
                for s in fresh)
            query = f"市况{env}; 信号: {'; '.join(s['symbol'] + s['type'] for s in fresh)}"
            memory_lines = await memory_context(query, k=2)

            llm = build_chain_llm(llm_cfg)
            parser = PydanticOutputParser(pydantic_object=NarrationOutput)
            out = await _call_parsed(
                system="你是 A 股动量趋势交易系统的信号解说员。只解说规则引擎已触发的信号, "
                       "不要质疑信号本身, 不要给出买卖数值决策, 不要建议改参数。",
                user=(
                    f"市况: {env}\n"
                    f"今日已触发信号(规则引擎判定, 只做解说):\n{sig_lines}\n\n"
                    f"{memory_block(memory_lines)}\n\n"
                    f"{parser.get_format_instructions()}"
                ),
                llm=llm, parser=parser,
                feature="assistant.narrate", prompt_version=NARRATE_PROMPT_V,
            )
            insights = [{"symbol": it.symbol, "type": s["type"],
                         "text": it.advice} for it in out.items for s in fresh if s["symbol"] == it.symbol]
            if not insights and out.summary:
                insights = [{"symbol": f.get("symbol"), "type": f.get("type"),
                             "text": out.summary} for f in fresh]
        except Exception:  # noqa: BLE001 - 降级规则模板
            logger.warning("助理解说失败, 降级规则模板", exc_info=True,
                           extra={"component": "assistant", "phase": phase})
    if not insights:
        # 规则模板: PlanGenerator 同款文案风格
        from app.core.plan.generator import PlanGenerator

        gen = PlanGenerator()
        for sig in fresh:
            try:
                plan = gen.generate(sig["symbol"], sig.get("name", ""), _SignalProxy(sig))
                text = plan.get("content", "").splitlines()[-1] if plan else ""
            except Exception:  # noqa: BLE001
                text = f"{SIGNAL_LABEL.get(sig['type'], sig['type'])}: {sig.get('reason', '')}"
            insights.append({"symbol": sig["symbol"], "type": sig["type"], "text": text})
    return {"insights": insights}


class _SignalProxy:
    """把 dict 信号包装成 PlanGenerator 期望的对象(仅用 type/strength/reason/price)."""

    def __init__(self, sig: dict) -> None:
        self.type = sig.get("type", "")
        self.strength = float(sig.get("strength", 0))
        self.reason = sig.get("reason", "")
        self.price = float(sig.get("price", 0))
        self.indicators_snapshot: dict[str, Any] = {}


def memory_block(memory_lines: str) -> str:
    if not memory_lines:
        return ""
    return ("历史复盘记忆(相似信号的经验, 供解说参考, 不要重复建议已证明无效的调整):\n"
            f"{memory_lines}")


# ---------------------------------------------------------------- 节点: notify
async def notify(state: dict[str, Any]) -> dict[str, Any]:
    """站内通知 + 可选 webhook; 记录去重指纹.

    每条通知标题带「代码 名称 信号类型」, 内容为 AI 解说(不依赖上下文也能看懂).
    """
    insights = state.get("insights") or []
    fresh = state.get("fresh") or []
    if not insights:
        return {"notifications": []}
    from app.core.report.notify import push_notification

    cfg = _assistant_cfg()
    webhook = cfg.get("push_webhook", "") or ""
    date = state.get("date") or dt.date.today().isoformat()

    rows = []
    for sig in fresh:
        text = next((it["text"] for it in insights if it.get("symbol") == sig.get("symbol")), "")
        if not text:
            continue
        label = SIGNAL_LABEL.get(sig.get("type"), sig.get("type", ""))
        name = str(sig.get("name", "") or "")
        per_title = f"AI 助理·{sig.get('symbol', '')} {name} {label}".strip()
        row = await push_notification("assistant", per_title, text, webhook,
                                      fingerprint=fingerprint(sig, date))
        rows.append({"id": row.id, "symbol": sig.get("symbol"), "type": sig.get("type")})
    return {"notifications": rows, "pushed": [fingerprint(s, date) for s in fresh]}


# ---------------------------------------------------------------- 节点: daily_report(盘后)
async def daily_report(state: dict[str, Any]) -> dict[str, Any]:
    """盘后: 复用 report 模块生成日报(其内部已推送)."""
    from app.core.report.service import report_service

    date = state.get("date") or dt.date.today().isoformat()
    report = await report_service.generate(date)
    return {"notifications": [{"id": report.id, "type": "daily_report", "status": report.status}]}
