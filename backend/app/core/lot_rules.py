"""A 股申报数量规则(交易所合规).

- 科创板(688/689): 买入 ≥200 股, 1 股递增
- 北交所(43/83/87/88/92): 买入 ≥100 股, 1 股递增
- 主板/创业板(其余): 买入 ≥100 股, 100 股整数倍
- 卖出: 任意数量可申报, 但卖出后剩余持仓不足最小单位(碎股)时必须一次性全部卖出

供回测撮合 / 交易计划生成 / 持仓录入 共用, 避免各模块规则漂移(曾出现计划建议
科创板加仓 180 股 <200 的违规数量, 根因即此处规则未收敛到单一实现).
"""

from __future__ import annotations

_STAR_PREFIX = ("688", "689")
_BJ_PREFIX = ("43", "83", "87", "88", "92")


def min_buy_unit(symbol: str) -> int:
    """最小买入申报单位(股)."""
    return 200 if symbol.startswith(_STAR_PREFIX) else 100


def round_buy_qty(raw: int, symbol: str) -> int:
    """买入数量按板块规则取整: 科创板/北交所 1 股递增(≥下限), 其余 100 整数倍. 不足下限返回 0."""
    if raw <= 0:
        return 0
    if symbol.startswith(_STAR_PREFIX):
        return raw if raw >= 200 else 0
    if symbol.startswith(_BJ_PREFIX):
        return raw if raw >= 100 else 0
    qty = raw // 100 * 100
    return qty if qty >= 100 else 0


def sell_qty(qty: int, pos_qty: int, symbol: str) -> int:
    """卖出数量合规: 卖出后剩余不足最小单位(碎股)时一次性全部卖出."""
    if qty <= 0 or pos_qty <= 0:
        return 0
    qty = min(qty, pos_qty)
    remain = pos_qty - qty
    if 0 < remain < min_buy_unit(symbol):
        return pos_qty  # 碎股必须清仓
    return qty


def board_note(symbol: str) -> str:
    """买入板块提示文案(主板/创业板返回空串, 避免啰嗦)."""
    if symbol.startswith(_STAR_PREFIX):
        return "科创板单笔申报≥200股"
    if symbol.startswith(_BJ_PREFIX):
        return "北交所单笔申报≥100股"
    return ""
