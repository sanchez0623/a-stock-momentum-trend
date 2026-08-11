"""仓位管理单测(临时 DB)."""

from __future__ import annotations

import pytest
from app import db
from app.core.account import account_manager
from app.core.config import config_manager
from app.core.fees import compute_trade_fee
from app.core.position.manager import PositionManager, PositionManagerError
from app.models.models import Position, Trade
from sqlmodel import select

pm = PositionManager()


def _fee(action: str, amount: float) -> float:
    """按当前费率配置算单笔手续费(不写死数值, 费率改了用例仍成立)."""
    return compute_trade_fee(action, amount, config_manager.get().get("手续费"))


def _backdate(symbol: str, opened_at: str = "2020-01-01 09:30:00") -> None:
    """把持仓时间改到过去, 解除 T+1 锁定(用于测试正常减仓/清仓)."""
    with db.session_scope() as s:
        p = s.exec(select(Position).where(Position.symbol == symbol)).first()
        assert p is not None, f"{symbol} 无持仓, 无法 backdate"
        p.opened_at = opened_at
        s.add(p)
        s.commit()


def test_open_first_position(tmp_engine):
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    assert pos.qty == 100
    # cost 为含费摊薄成本(券商 APP 口径), cost_raw 为纯成交均价
    assert pos.cost == pytest.approx((100.0 * 100 + _fee("buy", 10000.0)) / 100)
    assert pos.cost > 100.0
    assert pos.cost_raw == pytest.approx(100.0)
    assert pos.status == "holding"


def test_pyramid_add_recalculates_cost(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pos = pm.open_or_add("300750", "宁德时代", 100, 120.0, "加仓", None)
    assert pos.qty == 200
    expected = (10000.0 + _fee("buy", 10000.0) + 12000.0 + _fee("buy", 12000.0)) / 200
    assert pos.cost == pytest.approx(expected, abs=1e-4)
    assert pos.cost_raw == pytest.approx(110.0)  # (100*100 + 120*100) / 200


def test_reject_downgrade_add(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    with pytest.raises(PositionManagerError):
        pm.open_or_add("300750", "宁德时代", 100, 90.0, "低于成本加仓", None)


def test_flat_price_add_allowed(tmp_engine):
    """平价加仓不应被手续费误杀: 顺向判断走 cost_raw 而非含费成本."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "平价加仓", None)
    assert pos.qty == 200
    assert pos.cost_raw == pytest.approx(100.0)


def test_reduce_realized_pnl(tmp_engine):
    """已实现盈亏 = 现价 − 含费成本 − 卖出费, 即扣掉双边费用的净额."""
    pos = pm.open_or_add("300750", "宁德时代", 200, 100.0, "首仓", None)
    _backdate("300750")  # 解除 T+1 锁定, 否则当日买入不可减仓
    cost_incl = pos.cost  # 已摊入买入手续费
    pnl = pm.reduce("300750", 100, 120.0, "减仓", None)
    expected = round((120.0 - cost_incl) * 100, 2) - _fee("sell", 120.0 * 100)
    assert pnl == pytest.approx(expected, abs=0.01)
    # 严格小于毛盈亏(双边费用都被扣掉了)
    assert pnl < (120.0 - 100.0) * 100
    assert pm.get_position("300750", None).qty == 100


def test_close_position(tmp_engine):
    pos = pm.open_or_add("600519", "贵州茅台", 100, 100.0, "首仓", None)
    _backdate("600519")  # 解除 T+1 锁定, 否则当日买入不可清仓
    cost_incl = pos.cost
    pnl = pm.close("600519", 110.0, "清仓", None)
    expected = round((110.0 - cost_incl) * 100, 2) - _fee("sell", 110.0 * 100)
    assert pnl == pytest.approx(expected, abs=0.01)
    assert pm.get_position("600519", None) is None  # 已清仓不在持仓列表


def test_t_plus_one_blocks_sell(tmp_engine):
    """当日买入的持仓当日不可减仓/卖出(T+1)."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    assert pm.is_t_plus_one_locked("300750", None) is True
    with pytest.raises(PositionManagerError):
        pm.reduce("300750", 100, 120.0, "减仓", None)
    with pytest.raises(PositionManagerError):
        pm.close("300750", 110.0, "清仓", None)


def test_t_plus_one_allows_next_day(tmp_engine):
    """持仓时间改为昨日即解锁, 可正常减仓."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    _backdate("300750", "2020-01-01 09:30:00")
    assert pm.is_t_plus_one_locked("300750", None) is False
    assert pm.reduce("300750", 100, 120.0, "减仓", None) is not None


def test_opened_at_recorded_and_stable_on_add(tmp_engine):
    """首仓记录持仓时间; 加仓不刷新(仍为首仓时刻)."""
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    first = pos.opened_at
    assert first
    pm.open_or_add("300750", "宁德时代", 100, 120.0, "加仓", None)
    assert pm.get_position("300750", None).opened_at == first


def test_account_start_capital_edit_and_default(tmp_engine):
    """启动资金可修改并持久化; 后端不再返回已废弃的 remaining_capital.

    可用资金/总权益由前端派生(含已实现盈亏 realized_pnl), 此处仅校验
    后端资金账户以 start_capital 为唯一真值源。
    """
    acc = account_manager.get(None)
    assert acc["start_capital"] == pytest.approx(500000.0, abs=0.01)  # 默认 50w
    assert "remaining_capital" not in acc  # 旧字段已废弃

    account_manager.set_start(1000000.0, None)
    acc = account_manager.get(None)
    assert acc["start_capital"] == pytest.approx(1000000.0, abs=0.01)

    # 买入/卖出不再改变后端账户(可用资金由前端按持仓市值派生)
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    _backdate("300750")
    assert pm.reduce("300750", 100, 120.0, "减仓", None) is not None
    acc = account_manager.get(None)
    assert acc["start_capital"] == pytest.approx(1000000.0, abs=0.01)

    with pytest.raises(ValueError):
        account_manager.set_start(-1.0, None)



def test_reduce_without_position_raises(tmp_engine):
    with pytest.raises(PositionManagerError):
        pm.reduce("000001", 100, 10.0, "test", None)


def test_pyramid_plan_stage(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    plan = pm.pyramid_plan("300750", None)
    assert plan["strategy"] == "pyramid"
    assert plan["used_stage"] == 0  # 首仓后, 第一档(0.5)已用
    assert plan["suggest_next_pct"] == plan["remaining_ratios"][0] * 100


def test_pyramid_plan_stage_after_add(tmp_engine):
    """已加一档后, 下一档应为第3档(20%), 不再误报第1档."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "加仓", None)
    plan = pm.pyramid_plan("300750", None)
    assert plan["used_stage"] == 1
    assert plan["next_stage_index"] == 2
    assert plan["suggest_next_pct"] == pytest.approx(20.0)


def test_pyramid_stage_not_bumped_on_same_day_double_count(tmp_engine):
    """同日多笔买入(分批建仓)应各算一档, 但仍由显式字段维护, 而非按笔数倒推误判."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "加仓", None)
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "加仓", None)
    pos = pm.get_position("300750", None)
    assert pos.pyramid_stage == 2  # 首仓 + 两次加仓 = 已用2档
    plan = pm.pyramid_plan("300750", None)
    assert plan["next_stage_exhausted"] is True  # 三档(0.5/0.3/0.2)已用尽


def test_take_profit_levels(tmp_engine):
    levels = pm.take_profit_levels(100.0, None)
    assert len(levels) == 3
    assert levels[0]["target_price"] == pytest.approx(103.0)
    assert levels[0]["target_pct"] == pytest.approx(3.0)


def test_kelly_discounted():
    f = pm.kelly(0.6, 2.0, 1.0)
    # f = 0.6 - 0.4/2 = 0.4, 折扣 0.5 -> 0.2
    assert f == pytest.approx(0.2)


def test_kelly_zero_for_bad_odds():
    assert pm.kelly(0.4, 1.0, 2.0) == 0.0  # 负期望 -> 0


def test_portfolio_summary(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pm.open_or_add("600519", "贵州茅台", 100, 200.0, "首仓", None)
    summary = pm.portfolio({"300750": 110.0, "600519": 200.0}, None)
    fees = _fee("buy", 10000.0) + _fee("buy", 20000.0)
    assert summary["market_value"] == pytest.approx(31000.0)
    # 成本含费 -> 浮盈比毛值少掉已付的买入手续费
    assert summary["unrealized_pnl"] == pytest.approx(1000.0 - fees, abs=0.02)
    assert summary["fee_cost"] == pytest.approx(fees, abs=0.02)
    assert summary["cost_raw_value"] == pytest.approx(30000.0)
    assert summary["cost_value"] == pytest.approx(30000.0 + fees, abs=0.02)
    assert len(summary["positions"]) == 2
    # 仅买入未卖出 -> 已实现盈亏为 0
    assert summary["realized_pnl"] == 0.0


def test_portfolio_realized_pnl_accumulates_sells(tmp_engine):
    """历史卖出净额(已扣双边手续费)应累计进 realized_pnl, 供总权益 = 启动资金 + 已实现 + 浮动."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    _backdate("300750")
    pnl1 = pm.reduce("300750", 100, 120.0, "减仓", None)
    pm.open_or_add("600519", "贵州茅台", 100, 100.0, "首仓", None)
    _backdate("600519")
    pnl2 = pm.close("600519", 90.0, "清仓", None)
    summary = pm.portfolio({}, None)
    assert summary["realized_pnl"] == pytest.approx(pnl1 + pnl2, abs=0.01)


def test_open_or_add_force_allows_below_cost(tmp_engine):
    """低于成本加仓默认拒绝; force=True 放行且摊薄成本, 成交原因标注「强制录入」."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    with pytest.raises(PositionManagerError, match="低于当前成本"):
        pm.open_or_add("300750", "宁德时代", 100, 90.0, "低位加仓", None)

    pos = pm.open_or_add("300750", "宁德时代", 100, 90.0, "低位加仓", None, force=True)
    assert pos.qty == 200
    # 摊薄后成本介于两次成交价之间(含费)
    assert 90.0 < pos.cost < 100.0
    assert pos.cost_raw == pytest.approx(95.0, abs=0.01)
    # 成交原因标注强制录入
    with db.session_scope() as s:
        last = s.exec(select(Trade).where(Trade.symbol == "300750").order_by(Trade.id.desc())).first()
        assert last is not None and "强制录入" in (last.reason or "")
    # 数量合规仍是硬规则: force 不能绕过
    with pytest.raises(PositionManagerError, match="买入申报数量"):
        pm.open_or_add("688146", "中船特气", 150, 300.0, "首仓", None, force=True)


def test_open_or_add_rejects_non_compliant_buy_qty(tmp_engine):
    """买入申报数量须符合板块规则(与交易计划同一套): 科创板≥200, 主板100整数倍."""
    with pytest.raises(PositionManagerError, match="买入申报数量"):
        pm.open_or_add("688146", "中船特气", 180, 300.0, "首仓", None)  # 科创板 <200
    with pytest.raises(PositionManagerError, match="买入申报数量"):
        pm.open_or_add("000001", "平安银行", 150, 10.0, "首仓", None)  # 主板非100倍数
    # 合规数量不受影响: 科创板 1 股递增(≥200), 主板 100 整数倍
    pos = pm.open_or_add("688146", "中船特气", 421, 300.0, "首仓", None)
    assert pos.qty == 421
    pos = pm.open_or_add("000001", "平安银行", 200, 10.0, "首仓", None)
    assert pos.qty == 200
