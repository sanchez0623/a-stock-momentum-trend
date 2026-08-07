"""AI 复盘服务(方案 §4.10): 规则诊断 + LLM 深度复盘 + 结果落库 + 建议追踪."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from sqlmodel import Session, select

from app import db
from app.core.ai_review.llm import LLMError, build_client_from_config
from app.core.ai_review.rules import diagnose
from app.core.config import config_manager
from app.models.models import AiReview, SignalRecord, Trade

logger = logging.getLogger(__name__)

DEFAULT_SCOPE_DAYS = 14  # 默认复盘最近 N 天


class ReviewService:
    # ------------------------------------------------------------ 范围
    @staticmethod
    def _scope_range(scope: str) -> tuple[str, str]:
        """解析范围: 'week' | 'month' | 'all' | 'YYYY-MM-DD..YYYY-MM-DD'."""
        today = dt.date.today()
        if scope == "week":
            monday = today - dt.timedelta(days=today.weekday())
            return monday.isoformat(), today.isoformat()
        if scope == "month":
            return today.replace(day=1).isoformat(), today.isoformat()
        if ".." in scope:
            a, b = scope.split("..", 1)
            return a, b
        if scope in ("all", ""):
            return "", ""
        # 任意日期字符串: 该日至今
        return scope, today.isoformat()

    # ------------------------------------------------------------ 主流程
    async def run(self, scope: str = "week", session: Session | None = None) -> AiReview:
        """执行一次复盘: 取记录 -> 规则诊断 -> (可选)LLM -> 落库."""
        start, end = self._scope_range(scope)
        with session or db.session_scope() as s:
            stmt = select(Trade).order_by(Trade.time)
            if start:
                stmt = stmt.where(Trade.time >= start)
            if end:
                stmt = stmt.where(Trade.time <= end + " 23:59:59")
            trades = list(s.exec(stmt).all())

            sig_stmt = select(SignalRecord).order_by(SignalRecord.time)
            if start:
                sig_stmt = sig_stmt.where(SignalRecord.time >= start)
            if end:
                sig_stmt = sig_stmt.where(SignalRecord.time <= end + " 23:59:59")
            signals = list(s.exec(sig_stmt).all())

            # 行情快照(有交易记录的标的, 取日线用于追高/逆势规则)
            klines = await self._load_klines([t.symbol for t in trades])

        issues = diagnose(trades, signals, klines)
        stats = self._summary(trades)

        # LLM 深度复盘(可关)
        llm_cfg = config_manager.get().get("llm", {})
        content, suggestions, model = "", [], ""
        if llm_cfg.get("enabled") and llm_cfg.get("api_key"):
            try:
                content, suggestions, model = await self._llm_review(trades, signals, issues, stats, llm_cfg)
            except LLMError as exc:
                logger.warning("LLM 复盘失败, 降级为纯规则诊断: %s", exc)
                content = f"⚠️ LLM 调用失败(已降级为纯规则诊断): {exc}"
        else:
            content = "未启用 LLM(可在「AI 复盘」页配置 DeepSeek Key), 本次仅规则诊断。"

        review = AiReview(
            range=scope or "all",
            content=content or "无复盘内容",
            suggestions_json=json.dumps(suggestions, ensure_ascii=False),
            model=model,
            rule_result_json=json.dumps({"issues": issues, "stats": stats}, ensure_ascii=False),
        )
        with session or db.session_scope() as s:
            s.add(review)
            s.commit()
            s.refresh(review)
        return review

    @staticmethod
    async def _load_klines(symbols: list[str]) -> dict[str, Any]:
        from app.core.datasource import data_source_manager

        out: dict[str, Any] = {}
        for sym in dict.fromkeys(symbols):
            try:
                df = await data_source_manager.get_kline(sym, "daily", 80)
                if df is not None and not df.empty:
                    out[sym] = df
            except Exception:  # noqa: BLE001
                continue
        return out

    @staticmethod
    def _summary(trades: list[Any]) -> dict[str, Any]:
        sells = [t for t in trades if t.action == "sell" and t.pnl is not None]
        total_pnl = sum(float(t.pnl or 0) for t in sells)
        wins = [t for t in sells if t.pnl > 0]
        return {
            "trades": len(trades),
            "closed": len(sells),
            "wins": len(wins),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0.0,
        }

    # ------------------------------------------------------------ LLM
    @staticmethod
    async def _llm_review(trades: list[Any], signals: list[Any], issues: list[dict],
                          stats: dict, llm_cfg: dict) -> tuple[str, list[dict], str]:
        client = build_client_from_config(llm_cfg)
        trade_lines = "\n".join(
            f"- {t.time} {t.symbol} {t.name} {'买入' if t.action == 'buy' else '卖出'} "
            f"{t.qty}股@{t.price} 盈亏{t.pnl if t.pnl is not None else '-'} {t.reason or ''}"
            for t in trades[-40:]
        )
        issue_lines = "\n".join(
            f"- [{i['level']}] {i['title']}: {i['detail']}" for i in issues
        ) or "- 无"
        prompt = f"""你是 A 股动量/趋势交易系统的复盘教练。基于以下交易记录与规则诊断, 输出严格的 JSON(不要输出 JSON 外的任何内容):
{{
  "analysis": "整体问题归因与亮点, 150字内",
  "suggestions": [{{"text": "一条可执行改进建议"}}],  // 2-5 条
  "discipline_score": 0-100
}}

近 {stats['closed']} 笔已平仓, 胜率 {stats['win_rate']}%, 总盈亏 {stats['total_pnl']} 元。
规则诊断结果:
{issue_lines}

交易记录(最近):
{trade_lines}
"""
        try:
            text = await client.chat([
                {"role": "system", "content": "你是一位严谨的量化交易复盘教练, 输出简洁可执行。"},
                {"role": "user", "content": prompt},
            ])
        except LLMError:
            raise
        content, suggestions = ReviewService._parse_llm_output(text)
        return content, suggestions, client.model

    @staticmethod
    def _parse_llm_output(text: str) -> tuple[str, list[dict]]:
        """解析 LLM 输出: 提取 JSON(容忍 ```json 包裹/前后噪声)."""
        suggestions: list[dict] = []
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        try:
            start = cleaned.index("{")
            end = cleaned.rindex("}")
            data = json.loads(cleaned[start:end + 1])
            content = str(data.get("analysis", cleaned[:500]))
            for item in data.get("suggestions", []):
                if isinstance(item, dict) and item.get("text"):
                    suggestions.append({"text": str(item["text"]), "status": "pending"})
                elif isinstance(item, str):
                    suggestions.append({"text": item, "status": "pending"})
        except (ValueError, json.JSONDecodeError):
            content = cleaned[:1000]
        if not suggestions:
            # 兜底: 把整段拆成建议(按换行)
            for line in text.splitlines():
                line = line.strip("•- \t")
                if len(line) > 4:
                    suggestions.append({"text": line[:100], "status": "pending"})
        return content or "LLM 未返回分析", suggestions

    # ------------------------------------------------------------ 查询与追踪
    def history(self, limit: int = 20, session: Session | None = None) -> list[AiReview]:
        with session or db.session_scope() as s:
            return list(s.exec(select(AiReview).order_by(AiReview.time.desc()).limit(limit)).all())

    def get(self, review_id: int, session: Session | None = None) -> AiReview | None:
        with session or db.session_scope() as s:
            return s.get(AiReview, review_id)

    def update_suggestion(self, review_id: int, index: int, status: str,
                          session: Session | None = None) -> AiReview | None:
        """改进建议追踪: 标记 已采纳/未采纳."""
        if status not in ("accepted", "rejected"):
            raise ValueError("status 需为 accepted/rejected")
        with session or db.session_scope() as s:
            review = s.get(AiReview, review_id)
            if review is None:
                return None
            items = json.loads(review.suggestions_json or "[]")
            if 0 <= index < len(items):
                items[index]["status"] = status
                review.suggestions_json = json.dumps(items, ensure_ascii=False)
                s.commit()
                s.refresh(review)
            return review


review_service = ReviewService()
