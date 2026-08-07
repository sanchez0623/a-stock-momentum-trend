"""AI 复盘 - 规则诊断(方案 §4.10.1, 确定性规则, 纯函数可测).

规则:
1. 止损纪律: 出现 SELL_STOP 信号但之后未卖出 -> 违反止损纪律
2. 追高: 买入价偏离当日收盘价过高(>2%, 需 K 线)
3. 频繁交易: 同标的同一交易日超过 2 笔
4. 逆势操作: 买入时均线空头排列(短<中<长, 需 K 线)

输入: trades/signals 对象列表 + 可选 klines {symbol: DataFrame}
输出: [{code, level, title, detail, evidence}]
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def diagnose(trades: list[Any], signals: list[Any], klines: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """对交易记录+信号执行规则诊断, 返回问题清单(按严重度排序)."""
    issues: list[dict[str, Any]] = []
    klines = klines or {}

    # 1. 止损纪律: SELL_STOP 信号后无卖出
    for sig in signals:
        if getattr(sig, "type", "") != "SELL_STOP":
            continue
        later_sells = [t for t in trades
                       if t.symbol == sig.symbol and t.action == "sell" and t.time >= sig.time]
        if not later_sells:
            issues.append({
                "code": "stop_loss_ignored",
                "level": "high",
                "title": "违反止损纪律",
                "detail": f"{sig.symbol} 在 {sig.time} 触发止损信号(强度{sig.strength}), 但至今未卖出",
                "evidence": f"signal@{sig.time} -> 无后续卖出",
            })

    # 2. 频繁交易: 同标的同一交易日 > 2 笔
    day_counts = Counter((t.symbol, t.time[:10]) for t in trades)
    for (sym, day), cnt in sorted(day_counts.items()):
        if cnt > 2:
            issues.append({
                "code": "over_trading",
                "level": "medium",
                "title": "频繁交易",
                "detail": f"{sym} 在 {day} 交易 {cnt} 笔, 日内过度操作",
                "evidence": f"{cnt} 笔/日",
            })

    # 3/4. 追高与逆势(需要 K 线)
    for t in trades:
        if t.action != "buy":
            continue
        df = klines.get(t.symbol)
        if df is None or df.empty:
            continue
        try:
            day = t.time[:10]
            row = df[df["date"] == day]
            if row.empty:
                row = df.tail(1)
            last = row.iloc[-1]
            close = float(last["close"])
            # 3. 追高: 买入价 > 当日收盘 2%
            if t.price > close * 1.02:
                issues.append({
                    "code": "chase_high",
                    "level": "medium",
                    "title": "追高买入",
                    "detail": f"{t.symbol} {day} 买入价 {t.price:.2f} 高于当日收盘 {close:.2f} ({(t.price/close-1)*100:.1f}%)",
                    "evidence": f"buy@{t.price:.2f} vs close@{close:.2f}",
                })
            # 4. 逆势: 买入日 MA 空头(短<中<长)
            ma_s = float(last.get("ma5", last.get("ma10", close)))
            ma_m = float(last.get("ma10", last.get("ma20", close)))
            ma_l = float(last.get("ma20", last.get("ma60", close)))
            if 0 < ma_s < ma_m < ma_l:
                issues.append({
                    "code": "counter_trend",
                    "level": "high",
                    "title": "逆势买入",
                    "detail": f"{t.symbol} {day} 买入时均线空头排列 (MA{ma_s:.1f}<MA{ma_m:.1f}<MA{ma_l:.1f})",
                    "evidence": f"buy@{t.price:.2f} on bearish MA",
                })
        except (KeyError, IndexError, TypeError, ValueError):
            continue

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: order.get(x["level"], 9))
    return issues
