"""盘后 AI 日报服务(全只读).

流程: 收集素材(今日交易/持仓/信号/自选/风控/市况/纪律诊断)
      -> 组装素材文本 -> (可选)检索复盘记忆 -> LLM 链
      -> LLM 失败/未配置降级规则模板
      -> 落库 DailyReport(同日覆盖) + 站内通知 + 可选 webhook 推送。

任何一步失败不影响整体: 日报尽力而为, 缺失的部分就只报有的数据。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import pandas as pd
from sqlmodel import Session, select

from app import db
from app.core.config import config_manager
from app.core.indicators.indicators import atr
from app.core.position import position_manager
from app.core.risk import risk_manager
from app.core.signals.engine import SignalEngine
from app.models.models import DailyReport, SignalRecord, Trade

logger = logging.getLogger(__name__)

SIGNAL_LABEL = {
    "BUY_FIRST": "首仓信号", "BUY_ADD": "加仓信号", "SELL_REDUCE": "减仓信号",
    "SELL_STOP": "止损信号", "T_BUY": "做T买入", "T_SELL": "做T卖出",
}


class DailyReportService:
    # ------------------------------------------------------------ 素材(纯函数)
    @staticmethod
    def _trades_text(trades: list[Trade]) -> str:
        if not trades:
            return "- 无"
        lines = []
        for t in trades:
            act = "买入" if t.action == "buy" else "卖出"
            pnl = f" 盈亏{t.pnl:+.0f}元" if t.action == "sell" and t.pnl is not None else ""
            lines.append(f"- {t.time[11:16]} {t.symbol} {t.name} {act} {t.qty}股@{t.price:.2f}{pnl}")
        return "\n".join(lines)

    @staticmethod
    def _signals_text(signals: list[SignalRecord]) -> str:
        if not signals:
            return "- 无"
        lines = []
        for s in signals:
            label = SIGNAL_LABEL.get(s.type, s.type)
            lines.append(f"- {s.symbol} {s.name} {label}(强度{s.strength:.0f}) {s.reason}")
        return "\n".join(lines)

    @staticmethod
    def _holdings_text(items: list[dict]) -> str:
        if not items:
            return "- 无持仓"
        lines = []
        for it in items:
            tp = " / ".join(f"{t:.2f}" for t in it["tp_targets"]) or "-"
            lines.append(
                f"- {it['symbol']} {it['name']} {it['qty']}股 成本{it['cost']:.2f} "
                f"现价{it['price']:.2f}({it['pnl_pct']:+.2f}%) "
                f"止损线{it['stop_line']:.2f}(距{it['dist_to_stop_pct']:+.1f}%) "
                f"止盈档: {tp}"
            )
        return "\n".join(lines)

    @staticmethod
    def _market_text(gate: dict) -> str:
        if not gate or not gate.get("details"):
            return "参考指数数据不足, 市况判空"
        env = {"bull": "看多", "bear": "看空", "neutral": "中性"}.get(gate.get("environment"), "中性")
        return f"{gate.get('reason', '')} -> 环境{env}"

    @staticmethod
    def _risk_text(risk: dict, portfolio: dict) -> str:
        parts = []
        parts.append("日亏损熔断: " + ("⚠ 已触发" if risk.get("day_loss_tripped") else "未触发"))
        parts.append("防守模式: " + ("⚠ 开启" if risk.get("defense_mode") else "关闭"))
        parts.append(f"连亏 {risk.get('consecutive_losses', 0)} 笔"
                     f"(上限 {risk['config'].get('consecutive_loss_limit', 3)})")
        parts.append(f"总仓位 {portfolio.get('total_pct', 0):.0f}%"
                     f"(上限 {risk['config'].get('total_position_pct', 80):.0f}%)")
        return " | ".join(parts)

    @staticmethod
    def _discipline_text(issues: list[dict]) -> str:
        if not issues:
            return "- 无纪律问题"
        return "\n".join(f"- [{i.get('level')}] {i.get('title')}" for i in issues)

    @staticmethod
    def build_report_text(market: str, trades: str, holdings: str, signals: str,
                          risk: str, discipline: str) -> str:
        """汇总素材文本(喂给 LLM 与降级模板共用)."""
        return (
            f"【市况】{market}\n"
            f"【今日操作】\n{trades}\n"
            f"【持仓】\n{holdings}\n"
            f"【今日信号】\n{signals}\n"
            f"【风控】{risk}\n"
            f"【纪律诊断】\n{discipline}"
        )

    # ------------------------------------------------------------ 降级模板
    @staticmethod
    def template_report(date: str, text: str) -> dict:
        """无 LLM 时的规则模板日报(仍是可读文本)."""
        return {
            "market_summary": "", "trade_summary": "", "holdings_review": [],
            "signals_today": [], "tomorrow_watch": [], "risk_notes": [],
            "discipline_score": 0,
            "text": f"📊 交易日报 {date}(规则模板, 未启用 LLM)\n\n{text}",
        }

    @staticmethod
    def _llm_content(out: Any) -> dict:
        """LLM 结构化输出 -> 内容 JSON(含拼接文本)."""
        text = "\n".join([
            f"📊 交易日报\n【市况】{out.market_summary}",
            f"【今日操作】{out.trade_summary}",
            "【持仓点评】" + ("; ".join(out.holdings_review) if out.holdings_review else "无"),
            "【今日信号】" + ("; ".join(out.signals_today) if out.signals_today else "无"),
            "【明日关注】" + ("; ".join(out.tomorrow_watch) if out.tomorrow_watch else "无"),
            "【风险提示】" + ("; ".join(out.risk_notes) if out.risk_notes else "无"),
            f"【纪律评分】{out.discipline_score}",
        ])
        return {
            "market_summary": out.market_summary,
            "trade_summary": out.trade_summary,
            "holdings_review": out.holdings_review,
            "signals_today": out.signals_today,
            "tomorrow_watch": out.tomorrow_watch,
            "risk_notes": out.risk_notes,
            "discipline_score": out.discipline_score,
            "text": text,
        }

    # ------------------------------------------------------------ 数据收集
    async def _collect(self, date: str, session: Session) -> dict[str, Any]:
        """收集当日素材(DB + 数据源 + 风控 + 闸门). 单项失败降级, 不中断."""
        from app.core.ai_review.rules import diagnose
        from app.core.market_gate import compute_market_gate, fetch_gate_index_dfs

        day_start, day_end = f"{date} 00:00:00", f"{date} 23:59:59"
        trades = list(session.exec(
            select(Trade).where(Trade.time >= day_start, Trade.time <= day_end)
            .order_by(Trade.time)).all())
        signals = list(session.exec(
            select(SignalRecord).where(SignalRecord.time >= day_start, SignalRecord.time <= day_end)
            .order_by(SignalRecord.time)).all())

        # 市况闸门(失败 -> 判空). 日报只做信息展示, 不跟随闸门 enabled 开关
        try:
            gate = compute_market_gate(
                await fetch_gate_index_dfs(config_manager.get().get("择时闸门", {}) or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("日报: 市况闸门获取失败 %s", exc, exc_info=True)
            gate = {}

        # 持仓 + 现价 + 止损/止盈
        positions = position_manager.list_positions(session)
        symbols = [p.symbol for p in positions]
        prices: dict[str, float] = {}
        if symbols:
            try:
                from app.core.datasource import data_source_manager

                quotes = await data_source_manager.get_realtime_quote(symbols)
                prices = {q.symbol: q.price for q in quotes if q and q.price}
            except Exception as exc:  # noqa: BLE001
                logger.warning("日报: 实时报价失败 %s", exc)
        cfg = config_manager.get()
        stop_pct = float(cfg["风控"]["stop_loss_pct"])
        holdings_items: list[dict] = []
        for p in positions:
            price = prices.get(p.symbol, p.cost or 0.0)
            stop_line = (p.cost or 0.0) * (1 - stop_pct / 100)
            # ATR 动态止盈档(K 线有缓存, 失败降级 fixed 档)
            tp_targets: list[float] = []
            try:
                from app.core.datasource import data_source_manager

                df = await data_source_manager.get_kline(p.symbol, "daily", 80)
                if df is not None and not df.empty:
                    last = atr(df)["atr14"].iloc[-1]
                    last_close = float(df["close"].iloc[-1])
                    if last_close > 0:
                        tp_targets = SignalEngine().take_profit_targets(
                            p.cost or 0.0, pd.Series({"atr14": last, "close": last_close}))
            except Exception as exc:  # noqa: BLE001
                logger.debug("日报: %s 止盈档计算失败 %s", p.symbol, exc)
            if not tp_targets:
                try:
                    tp_targets = [t["target_price"] for t in
                                  position_manager.take_profit_levels(p.cost or 0.0, None)]
                except Exception:  # noqa: BLE001
                    tp_targets = []
            cost = p.cost or 0.0
            pnl_pct = (price / cost - 1) * 100 if cost else 0.0
            dist_to_stop = (price / stop_line - 1) * 100 if stop_line else 0.0
            holdings_items.append({
                "symbol": p.symbol, "name": p.name, "qty": p.qty,
                "cost": cost, "price": price, "pnl_pct": pnl_pct,
                "stop_line": stop_line, "dist_to_stop_pct": dist_to_stop,
                "tp_targets": tp_targets,
            })

        # 组合仓位占比(总市值 / 启动资金)
        portfolio = {"total_pct": 0.0}
        from app.core.account import account_manager  # 延迟导入避免循环

        start_capital = account_manager.get(session)["start_capital"]
        if start_capital and holdings_items:
            mv = sum(it["price"] * it["qty"] for it in holdings_items)
            portfolio["total_pct"] = mv / start_capital * 100

        risk = risk_manager.status(session)

        # 纪律诊断(仅用当日交易, 无 K 线时跳过追高/逆势类规则)
        try:
            issues = diagnose(trades, signals)
        except Exception as exc:  # noqa: BLE001
            logger.warning("日报: 纪律诊断失败 %s", exc)
            issues = []

        return {
            "trades": trades, "signals": signals, "holdings": holdings_items,
            "portfolio": portfolio, "risk": risk, "gate": gate, "issues": issues,
        }

    # ------------------------------------------------------------ 主流程
    async def generate(self, date: str | None = None, session: Session | None = None) -> DailyReport:
        """生成某交易日(默认今天)的日报并推送. 同日重复生成覆盖旧版."""
        date = date or dt.date.today().isoformat()
        cfg = config_manager.get()
        llm_cfg = cfg.get("llm", {})
        report_cfg = cfg.get("日报", {})

        if session is not None:
            return await self._generate_impl(date, llm_cfg, report_cfg, session)
        with db.session_scope() as s:
            return await self._generate_impl(date, llm_cfg, report_cfg, s)

    async def _generate_impl(self, date: str, llm_cfg: dict, report_cfg: dict,
                             session: Session) -> DailyReport:
        data = await self._collect(date, session)
        market = self._market_text(data["gate"])
        trades_t = self._trades_text(data["trades"])
        holdings_t = self._holdings_text(data["holdings"])
        signals_t = self._signals_text(data["signals"])
        risk_t = self._risk_text(data["risk"], data["portfolio"])
        disc_t = self._discipline_text(data["issues"])
        materials_text = self.build_report_text(market, trades_t, holdings_t, signals_t, risk_t, disc_t)

        model, status, content = "", "ok", None
        if llm_cfg.get("enabled") and llm_cfg.get("api_key"):
            try:
                from app.core.ai_review.memory import memory_context
                from app.core.report.chain import run_report_chain

                query = f"问题: {'; '.join(str(i.get('title', '')) for i in data['issues'])}; 今日操作与持仓回顾"
                memory_lines = await memory_context(query, k=2)
                out, model = await run_report_chain(
                    {"text": materials_text}, llm_cfg, memory_lines)
                content = self._llm_content(out)
            except Exception:  # noqa: BLE001 - 降级规则模板
                logger.warning("日报 LLM 生成失败, 降级规则模板", exc_info=True,
                               extra={"component": "daily_report",
                                      "llm_model": llm_cfg.get("model"),
                                      "llm_max_tokens": llm_cfg.get("max_tokens"),
                                      "llm_timeout_sec": llm_cfg.get("timeout_sec")})
                status = "degraded"
                content = self.template_report(date, materials_text)
        else:
            status = "degraded"
            content = self.template_report(date, materials_text)

        content["materials"] = materials_text

        # 落库(同日覆盖)
        row = session.exec(select(DailyReport).where(DailyReport.date == date)).first()
        if row is None:
            row = DailyReport(date=date, content_json="{}")
            session.add(row)
        row.content_json = json.dumps(content, ensure_ascii=False)
        row.model = model
        row.status = status
        row.created_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        session.commit()
        session.refresh(row)
        session.expunge(row)  # 脱离 session, 避免外层 session 关闭后访问触发 reload

        # 站内通知 + 可选 webhook
        from app.core.report.notify import push_notification

        try:
            await push_notification(
                "report", f"交易日报 {date}",
                content.get("text", ""), report_cfg.get("push_webhook", ""), session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("日报通知失败: %s", exc)
        return row

    # ------------------------------------------------------------ 查询
    def get(self, date: str, session: Session | None = None) -> DailyReport | None:
        def _q(s: Session) -> DailyReport | None:
            return s.exec(select(DailyReport).where(DailyReport.date == date)).first()

        if session is not None:
            return _q(session)
        with db.session_scope() as s:
            return _q(s)


report_service = DailyReportService()
