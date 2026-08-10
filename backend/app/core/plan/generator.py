"""交易计划生成: 信号 + 仓位 + 风控 -> 一份人话操作指引(方案 §4.6 模板)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from app.core.config import config_manager
from app.core.lot_rules import board_note, min_buy_unit, round_buy_qty, sell_qty
from app.core.modes import ModeDecision, mode_for_ind
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
        mode: ModeDecision | None = None,
    ) -> dict[str, str]:
        """生成计划内容. 返回 {action, content}.

        mode: 当前交易模式决策(Q2, 规则化市况分类器选出). 为 None 时回退默认模式.
        计划中仓位/止损/止盈档均取自该模式的配置对象, 而非全局固定值.
        """
        cfg = config_manager.get()
        risk = cfg["风控"]
        t_cfg = cfg["做T"]
        mode = mode or mode_for_ind(None)  # None -> 默认模式决策
        mode_cfg = mode.mode  # 本模式的配置对象(比率/止损/止盈/加仓规则)
        stop_pct = float(mode_cfg.get("stop_loss_pct", risk["stop_loss_pct"]))
        trailing_pct = float(mode_cfg.get("trailing_stop_pct", risk["trailing_stop_pct"]))

        price = float(getattr(signal, "price", 0) or (quote.price if quote else 0))
        pos = position_manager.get_position(symbol)
        portfolio = portfolio or {}
        risk_status = risk_status or risk_manager.status()
        # T+1 锁定期: 持仓当日买入, 当日不可减仓/卖出(与 A 股 T+1 规则一致)
        t_locked = bool(pos) and position_manager.is_t_plus_one_locked(symbol)

        state_desc = self._state_desc(pos, price)
        signal_desc = f"{signal.type}(强度{signal.strength:.0f}) — {signal.reason}"

        if signal.type == "BUY_FIRST":
            action, advice = "buy_first", self._advice_buy_first(price, mode, symbol)
            stop_line = "—"
        elif signal.type == "BUY_ADD":
            action, advice = "buy_add", self._advice_buy_add(pos, portfolio, mode, symbol)
            stop_line = f"{pos.cost * (1 - stop_pct / 100):.2f}(成本下移{stop_pct:.0f}%)"
        elif signal.type in ("SELL_REDUCE", "SELL_STOP", "T_SELL"):
            if t_locked:
                # 当日买入无法卖出/减仓, 计划顺延至下一交易日
                action, advice = "hold", (
                    f"T+1 限制: 持仓于 {pos.opened_at} 买入, 今日不可减仓/卖出, "
                    "计划顺延至下一交易日; 今日仅可继续持有或做T买入"
                )
                stop_line = "—"
            elif signal.type == "SELL_REDUCE":
                action, advice = "sell_reduce", self._advice_sell_reduce(pos, symbol)
                stop_line = "—"
            elif signal.type == "SELL_STOP":
                action, advice = "sell_stop", "立即止损清仓, 不犹豫不补仓"
                stop_line = f"{pos.cost * (1 - stop_pct / 100):.2f}(止损线)"
            else:  # T_SELL
                action, advice = "t_sell", "做T卖出: 平掉今日做T买入部分, 不留隔夜增量"
                stop_line = "—"
        else:
            action, advice = "hold", "保持观察"
            stop_line = "—"

        # 止盈计划文本(取当前模式的止盈配置: ATR 动态档或 fixed 档, 早期少减让利润奔跑)
        if pos:
            from app.core.signals.engine import SignalEngine

            snapshot = getattr(signal, "indicators_snapshot", {}) or {}
            last_proxy = {"atr14": snapshot.get("atr14", 0) or 0, "close": snapshot.get("close", 0) or price}
            targets = SignalEngine().take_profit_targets(pos.cost, pd.Series(last_proxy), mode_cfg)
            ratios = mode_cfg.get("take_profit_ratios", [0.2, 0.3, 0.5])
            tp_parts = []
            for i, tgt in enumerate(targets):
                ratio = ratios[i] if i < len(ratios) else 0.3
                tp_parts.append(f"止盈+{(tgt / pos.cost - 1) * 100:.1f}% ({tgt:.2f}) 减{ratio * 100:.0f}%")
            tp_text = " | ".join(tp_parts) + f" | 余仓移动止损{trailing_pct:.0f}%"
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

        # 当前交易模式(规则化选型, 供 LLM 解说/人工复核)
        mode_line = f"当前模式: {mode.label}（{mode.reason}）"

        lines = [
            f"【交易计划 {dt.date.today().isoformat()}】",
            f"标的状态: {name}({symbol}) {state_desc}",
            mode_line,
            f"当前信号: {signal_desc}",
            f"建议操作: {advice}",
            f"触发价位: 现价附近 {price:.2f} ±0.5%",
            f"止损价位: {stop_line}",
            f"止盈计划: {tp_text}",
            f"风控检查: {rc_text}",
            f"纪律提醒: 加仓后总仓位勿超 {risk['total_position_pct']:.0f}%; 信号与计划一致时才动手",
        ]
        if t_locked:
            lines.append(
                f"T+1限制: 持仓 {symbol} 于 {pos.opened_at} 买入, 今日({dt.date.today().isoformat()})"
                "无法减仓/卖出, 相关卖出计划顺延至下一交易日"
            )
        return {"action": action, "content": "\n".join(lines)}

    # ------------------------------------------------------------ 文案助手
    @staticmethod
    def _state_desc(pos: Any, price: float) -> str:
        if pos is None or pos.qty <= 0:
            return "空仓"
        pnl_pct = (price - pos.cost) / pos.cost * 100 if pos.cost else 0
        return f"已持仓 {pos.qty} 股, 成本 {pos.cost:.2f}, 浮盈 {pnl_pct:+.1f}%"

    @staticmethod
    def _advice_buy_first(price: float, mode: ModeDecision | None = None, symbol: str = "") -> str:
        """首仓建议: 取当前模式金字塔比例的第 1 档(如强攻/回踩=50%, 震荡=70%).

        symbol 给定(非空)时附加板块最小申报单位提示(科创板≥200股等).
        """
        ratios = (mode.mode["pyramid_ratios"] if mode else [0.5, 0.3, 0.2])
        first = ratios[0] if ratios else 0.5
        text = f"首仓买入: 金字塔第一档 {first * 100:.0f}% 目标仓位, 触发价 {price:.2f} 附近"
        note = board_note(symbol)
        return f"{text}({note})" if note else text

    @staticmethod
    def _advice_buy_add(pos: Any, portfolio: dict[str, Any] | None = None,
                        mode: ModeDecision | None = None, symbol: str = "") -> str:
        """金字塔加仓建议(当前模式的金字塔比例).

        pos.pyramid_stage = 已完成档位(0=仅首仓). 已持仓状态下 BUY_ADD 应建议「下一档」,
        即 ratios[stage+1] —— 而非 ratios[stage](那是首仓本身, 会重复建议第1档50%).
        档位上限取模式 max_stages(如震荡模式最多 2 档).

        另做 Q4 资金感知: 当 portfolio 携带 available_capital 时, 反推该股目标总仓位市值,
        估算本次加仓所需金额并与可用资金比较, 给出「足够/超出」提示.
        symbol 给定(非空)时, 股数按板块申报规则取整并附板块提示(科创板≥200股等).
        """
        ratios = (mode.mode["pyramid_ratios"] if mode else [0.5, 0.3, 0.2])
        max_stages = int((mode.mode.get("max_stages") if mode else 3) or 3)
        stage = pos.pyramid_stage if pos else 0
        # 同时受"模式最大档位数"与"比例数组长度"约束
        effective_max = min(max_stages, len(ratios))
        next_idx = stage + 1  # 下一档加仓索引(首仓 ratios[0] 已占用)
        if next_idx < effective_max:
            advice = f"金字塔加仓: 第 {next_idx + 1} 档, 建议 {ratios[next_idx] * 100:.0f}% 目标仓位"
            capital_note = PlanGenerator._capital_aware_add_note(pos, ratios, next_idx, portfolio, symbol)
            if capital_note:
                advice += "; " + capital_note
            note = board_note(symbol)
            if note:
                advice += f"; {note}"
            return advice
        return "金字塔档位已用尽, 暂不加仓"

    @staticmethod
    def _capital_aware_add_note(
        pos: Any, ratios: list[float], next_idx: int, portfolio: dict[str, Any] | None,
        symbol: str = "",
    ) -> str:
        """Q4: 估算本次加仓金额并与可用资金比较(无可用资金信息时返回空串).

        symbol 给定(非空)时, 股数按板块申报规则取整(科创板≥200/北交所≥100/主板100整数倍),
        金额按取整后股数重算 —— 避免计划中出现 180 股这类无法成交的违规建议.
        """
        if not portfolio:
            return ""
        avail = portfolio.get("available_capital")
        if avail is None:
            return ""
        price = float(getattr(pos, "cost", 0) or 0)
        if price <= 0:
            return ""
        # 已完成档位占比之和(含首仓), 反推该股目标总仓位市值
        completed_ratio = sum(ratios[:next_idx])  # ratios[0..next_idx-1] 已建
        current_capital = float(getattr(pos, "cost", 0) or 0) * float(getattr(pos, "qty", 0) or 0)
        if completed_ratio <= 0 or current_capital <= 0:
            return ""
        target_total = current_capital / completed_ratio
        add_capital = target_total * ratios[next_idx]
        add_qty = int(add_capital / price)
        if symbol:
            add_qty = round_buy_qty(add_qty, symbol)  # 按板块申报规则取整
        if add_qty <= 0:
            if symbol:
                return f"本次加仓金额不足最小申报单位({min_buy_unit(symbol)}股)"
            return ""
        add_capital = add_qty * price  # 金额按合规股数重算
        if add_capital <= avail:
            return f"约加 {add_qty} 股(¥{add_capital:,.0f}), 可用资金 ¥{avail:,.0f} 充足"
        return (
            f"约加 {add_qty} 股(¥{add_capital:,.0f}), 超出可用资金 ¥{avail:,.0f}, "
            "请按资金上限缩减本次加仓"
        )

    @staticmethod
    def _advice_sell_reduce(pos: Any, symbol: str) -> str:
        """减仓建议: 给出 1/3~1/2 的具体股数, 并遵守卖出碎股规则.

        卖出可申报任意数量, 但减仓后剩余持仓不足最小申报单位(碎股)时须一次性清仓.
        """
        if pos is None or pos.qty <= 0:
            return "按信号减仓: 建议减 1/3 ~ 1/2 仓位"
        min_unit = min_buy_unit(symbol)
        lo = sell_qty(pos.qty // 3, pos.qty, symbol)
        hi = sell_qty(pos.qty // 2, pos.qty, symbol)
        if lo <= 0:
            return "按信号减仓: 持仓过小, 建议直接清仓"
        if lo == hi == pos.qty:
            return f"按信号减仓: 减后剩余不足最小申报单位({min_unit}股), 建议一次性清仓"
        return (
            f"按信号减仓: 建议减 {lo} ~ {hi} 股(约 1/3~1/2 仓位), "
            f"减后剩余不足 {min_unit} 股时建议一次性清仓"
        )
