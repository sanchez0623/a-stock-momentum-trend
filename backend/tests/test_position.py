"""仓位管理单测(临时 DB)."""

from __future__ import annotations

import pytest
from app.core.position.manager import PositionManager, PositionManagerError

pm = PositionManager()


def test_open_first_position(tmp_engine):
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    assert pos.qty == 100
    assert pos.cost == 100.0
    assert pos.status == "holding"


def test_pyramid_add_recalculates_cost(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pos = pm.open_or_add("300750", "宁德时代", 100, 120.0, "加仓", None)
    assert pos.qty == 200
    assert pos.cost == 110.0  # (100*100 + 120*100) / 200


def test_reject_downgrade_add(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    with pytest.raises(PositionManagerError):
        pm.open_or_add("300750", "宁德时代", 100, 90.0, "低于成本加仓", None)


def test_reduce_realized_pnl(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 200, 100.0, "首仓", None)
    pnl = pm.reduce("300750", 100, 120.0, "减仓", None)
    assert pnl == pytest.approx(2000.0)  # (120-100)*100
    pos = pm.get_position("300750", None)
    assert pos.qty == 100


def test_close_position(tmp_engine):
    pm.open_or_add("600519", "贵州茅台", 100, 100.0, "首仓", None)
    pnl = pm.close("600519", 110.0, "清仓", None)
    assert pnl == pytest.approx(1000.0)
    assert pm.get_position("600519", None) is None  # 已清仓不在持仓列表


def test_reduce_without_position_raises(tmp_engine):
    with pytest.raises(PositionManagerError):
        pm.reduce("000001", 100, 10.0, "test", None)


def test_pyramid_plan_stage(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    plan = pm.pyramid_plan("300750", None)
    assert plan["strategy"] == "pyramid"
    assert plan["used_stage"] == 0  # 首仓后, 第一档(0.5)已用
    assert plan["suggest_next_pct"] == plan["remaining_ratios"][0] * 100


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
    assert summary["market_value"] == pytest.approx(31000.0)
    assert summary["unrealized_pnl"] == pytest.approx(1000.0)
    assert len(summary["positions"]) == 2
