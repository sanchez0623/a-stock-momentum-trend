"""交易计划生成: 信号 + 仓位 + 风控 -> 一份人话操作指引(方案 §4.6 模板)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.config import config_manager
from app.core.position import position_manager
from app.core.risk import risk_manager


class PlanGenerator:
    """把结构化信号翻译成人话操作指引."""

    ACTION_MAP = {
        "BUY_FIRST": "buy_first",
        "BUY_ADD": "buy_add",
        "SELL_REDUCE": "sell_reduce",
        "SELL_STOP": "sell_stop",
        "T_BUY": "t_buy",
        "T_SELL": "t_sell",
    }

    def generate(
        self,
        symbol: str,
        name: str,
        signal: Any,
        quote: Any = None,
        portfolio: dict[str, Any] | None = None,
        risk_status: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """生成计划内容. 返回 {action, content}."""
        cfg = config_manager.get()
        risk = cfg["风控"]
        position_cfg = cfg["仓位"]
        t_cfg = cfg["做T"]

        price = float(getattr(signal, "price", 0) or (quote.price if quote else 0))
        pos = position_manager.get_position(symbol)
        portfolio = portfolio or {}
        risk_status = risk_status or risk_manager.status()

        state_desc = self._state_desc(pos, price)
        signal_desc = f"{signal.type}(强度{signal.strength:.0f}) — {signal.reason}"

        if signal.type == "BUY_FIRST":
            action, advice = "buy_first", self._advice_buy_first(price)
            stop_line = "—"
        elif signal.type == "BUY_ADD":
            action, advice = "buy_add", self._advice_buy_add(pos)
            stop_line = f"{pos.cost * (1 - risk['stop_loss_pct'] / 100):.2f}(成本下移{risk['stop_loss_pct']:.0f}%)"
        elif signal.type == "SELL_REDUCE":
            action, advice = "sell_reduce", "按信号减仓: 建议减 1/3 ~ 1/2 仓位"
            stop_line = "—"
        elif signal.type == "SELL_STOP":
            action, advice = "sell_stop", "立即止损清仓, 不犹豫不补仓"
            stop_line = f"{pos.cost * (1 - risk['stop_loss_pct'] / 100):.2f}(止损线)"
        elif signal.type == "T_BUY":
            action, advice = "t_buy", f"做T买入: 动用底仓的 {t_cfg['t_position_ratio'] * 100:.0f}% 资金, 日内必须平掉"
            stop_line = "—"
        elif signal.type == "T_SELL":
            action, advice = "t_sell", "做T卖出: 平掉今日做T买入部分, 不留隔夜增量"
            stop_line = "—"
        else:
            action, advice = "hold", "保持观察"
            stop_line = "—"

        # 止盈计划文本
        if pos:
            tp_parts = []
            for lv in position_cfg["take_profit_levels"]:
                tp_parts.append(f"{pos.cost * lv:.2f} 减30%")
            tp_text = " | ".join(tp_parts) + f" | 余仓移动止损{risk['trailing_stop_pct']:.0f}%"
        else:
            tp_text = "首仓后按金字塔止盈计划执行"

        # 风控检查文本
        rc_parts = []
        rc_parts.append(f"日亏损熔断: {'⚠ 已触发' if risk_status.get('day_loss_tripped') else '未触发'}")
        rc_parts.append(f"防守模式: {'⚠ 开启' if risk_status.get('defense_mode') else '关闭'}")
        total_pct = portfolio.get("total_pct", 0.0)
        rc_parts.append(f"总仓位 {total_pct:.0f}% / 上限 {risk['total_position_pct']:.0f}%")
        rc_ok = not risk_status.get("day_loss_tripped") and not risk_status.get("defense_mode")
        rc_text = " | ".join(rc_parts) + ("  ✓" if rc_ok else "  ⚠ 注意!")

        lines = [
            f"【交易计划 {dt.date.today().isoformat()}】",
            f"标的状态: {name}({symbol}) {state_desc}",
            f"当前信号: {signal_desc}",
            f"建议操作: {advice}",
            f"触发价位: 现价附近 {price:.2f} ±0.5%",
            f"止损价位: {stop_line}",
            f"止盈计划: {tp_text}",
            f"风控检查: {rc_text}",
            f"纪律提醒: 加仓后总仓位勿超 {risk['total_position_pct']:.0f}%; 信号与计划一致时才动手",
        ]
        return {"action": action, "content": "\n".join(lines)}

    # ------------------------------------------------------------ 文案助手
    @staticmethod
    def _state_desc(pos: Any, price: float) -> str:
        if pos is None or pos.qty <= 0:
            return "空仓"
        pnl_pct = (price - pos.cost) / pos.cost * 100 if pos.cost else 0
        return f"已持仓 {pos.qty} 股, 成本 {pos.cost:.2f}, 浮盈 {pnl_pct:+.1f}%"

    @staticmethod
    def _advice_buy_first(price: float) -> str:
        return f"首仓买入: 金字塔第一档 50% 目标仓位, 触发价 {price:.2f} 附近"

    @staticmethod
    def _advice_buy_add(pos: Any) -> str:
        cfg = config_manager.get()
        ratios = cfg["仓位"]["pyramid_ratios"]
        stage = position_manager.pyramid_plan(pos.symbol).get("used_stage", 0)
        if stage < len(ratios):
            return f"金字塔加仓: 第 {stage + 1} 档, 建议 {ratios[stage] * 100:.0f}% 目标仓位"
        return "金字塔档位已用尽, 暂不加仓"
