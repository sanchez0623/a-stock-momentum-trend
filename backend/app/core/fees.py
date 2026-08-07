"""A 股交易手续费计算.

费率默认值(可在 设置 -> 手续费 中调整, 热生效):
- 佣金 commission: 万 0.5 (0.00005), 单边, 单笔最低 5 元
- 印花税 stamp_tax: 万 5 (0.0005), 仅卖方
- 经手费 exchange_fee: 万 0.341 (0.0000341), 双边
- 证管费 regulatory_fee: 万 0.2 (0.00002), 双边
- 过户费 transfer_fee: 万 0.1 (0.00001), 双边

计算基于成交金额 amount = price * qty. 返回的 total 为单笔总手续费,
且印花税仅在卖出(action='sell')时计收.
"""

from __future__ import annotations

from typing import Any

# 与 config.DEFAULT_CONFIG["手续费"] 保持一致(作为兜底默认值)
DEFAULT_FEE_CONFIG: dict[str, float] = {
    "commission_rate": 0.00005,      # 万 0.5
    "commission_min": 5.0,           # 单笔最低佣金(元)
    "stamp_tax_rate": 0.0005,        # 万 5, 仅卖方
    "exchange_fee_rate": 0.0000341,  # 经手费, 双边
    "regulatory_fee_rate": 0.00002,  # 证管费, 双边
    "transfer_fee_rate": 0.00001,    # 过户费, 双边
}


def compute_trade_fee(action: str, amount: float, cfg: dict[str, Any] | None = None) -> float:
    """计算单笔成交手续费(元).

    action: 'buy' | 'sell'
    amount: 成交金额 = price * qty
    返回四舍五入(2 位)的总手续费.
    """
    f = cfg or DEFAULT_FEE_CONFIG
    if amount <= 0:
        return 0.0
    # 佣金: 费率 × 金额, 不低于最低佣金, 双边收取
    commission = max(amount * f["commission_rate"], f["commission_min"])
    # 印花税: 仅卖方
    stamp = amount * f["stamp_tax_rate"] if action == "sell" else 0.0
    # 经手费 / 证管费 / 过户费: 双边
    exchange = amount * f["exchange_fee_rate"]
    regulatory = amount * f["regulatory_fee_rate"]
    transfer = amount * f["transfer_fee_rate"]
    total = commission + stamp + exchange + regulatory + transfer
    return round(total, 2)


def fee_breakdown(action: str, amount: float, cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """返回各项费用明细(元), 便于前端/日志展示."""
    f = cfg or DEFAULT_FEE_CONFIG
    commission = max(amount * f["commission_rate"], f["commission_min"]) if amount > 0 else 0.0
    stamp = amount * f["stamp_tax_rate"] if (action == "sell" and amount > 0) else 0.0
    exchange = amount * f["exchange_fee_rate"] if amount > 0 else 0.0
    regulatory = amount * f["regulatory_fee_rate"] if amount > 0 else 0.0
    transfer = amount * f["transfer_fee_rate"] if amount > 0 else 0.0
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp, 2),
        "exchange_fee": round(exchange, 2),
        "regulatory_fee": round(regulatory, 2),
        "transfer_fee": round(transfer, 2),
        "total": round(commission + stamp + exchange + regulatory + transfer, 2),
    }
