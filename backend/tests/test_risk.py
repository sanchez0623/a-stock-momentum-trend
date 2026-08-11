"""风控模块单测."""

from __future__ import annotations

import pytest
from app.core.risk.manager import RiskManager

rm = RiskManager()


def _portfolio(total_pct: float = 0.0) -> dict:
    return {"total_pct": total_pct}


def test_default_status(tmp_engine):
    st = rm.status(None)
    assert st["day_loss_tripped"] is False
    assert st["defense_mode"] is False
    assert st["consecutive_losses"] == 0


def test_entry_allowed_when_clean(tmp_engine):
    allowed, reasons, pct = rm.check_entry("300750", 20.0, _portfolio(30.0), None)
    assert allowed is True
    assert reasons == []
    assert pct == 20.0


def test_entry_blocked_when_day_loss_tripped(tmp_engine):
    rm.reset(None)
    # 手动触发日亏损熔断
    from app import db
    from app.models.models import RiskState
    from sqlmodel import Session

    with Session(db.engine) as s:
        st = s.get(RiskState, 1)
        st.day_loss_tripped = True
        s.add(st)
        s.commit()
    allowed, reasons, _ = rm.check_entry("300750", 20.0, _portfolio(0.0), None)
    assert allowed is False
    assert any("熔断" in r for r in reasons)


def test_entry_capped_by_total_position(tmp_engine):
    rm.reset(None)
    allowed, reasons, pct = rm.check_entry("300750", 50.0, _portfolio(60.0), None)
    # 总仓位 60% + 50% = 110% > 80% 上限 -> 压到 20%
    assert allowed is True
    assert pct == pytest.approx(20.0)


def test_consecutive_losses_reduces_cap(tmp_engine):
    rm.reset(None)
    rm.record_trade_result(-500.0, None)
    rm.record_trade_result(-300.0, None)
    rm.record_trade_result(-200.0, None)  # 连亏 3 笔 -> 触发降仓
    st = rm.status(None)
    assert st["consecutive_losses"] == 3
    assert st["position_multiplier"] == 0.5


def test_win_resets_loss_streak(tmp_engine):
    rm.reset(None)
    rm.record_trade_result(-500.0, None)
    rm.record_trade_result(600.0, None)
    st = rm.status(None)
    assert st["consecutive_losses"] == 0


def test_reset_clears_state(tmp_engine):
    rm.record_trade_result(-100.0, None)
    rm.record_trade_result(-100.0, None)
    rm.record_trade_result(-100.0, None)
    rm.reset(None)
    st = rm.status(None)
    assert st["consecutive_losses"] == 0
    assert st["day_loss_tripped"] is False


def test_drawdown_triggers_defense(tmp_engine):
    rm.reset(None)
    assert rm.update_drawdown(100_000.0, 89_000.0, None) is True  # 回撤 11% > 10%
    st = rm.status(None)
    assert st["defense_mode"] is True

def test_dynamic_limits_base(tmp_engine):
    """无状态/无环境/无凯利数据 -> 上限保持基础 80/25."""
    rm.reset(None)
    lim = rm.dynamic_limits(None)
    assert lim["total_pct"] == 80.0
    assert lim["single_pct"] == 25.0
    assert lim["notes"] == []


def test_dynamic_limits_defense_halves(tmp_engine):
    """防守模式(回撤超限) -> 总/单票上限各砍半."""
    rm.reset(None)
    rm.update_drawdown(100_000.0, 89_000.0, None)  # 回撤 11% -> 防守
    lim = rm.dynamic_limits(None)
    assert lim["total_pct"] == 40.0
    assert lim["single_pct"] == 12.5
    assert any("砍半" in n for n in lim["notes"])


def test_dynamic_limits_market_env(tmp_engine):
    """大盘看空 -> 上限 ×0.6; 中性 ×0.85; 看多不调整."""
    rm.reset(None)
    assert rm.dynamic_limits(None, market_env="bull")["total_pct"] == 80.0
    assert rm.dynamic_limits(None, market_env="neutral")["total_pct"] == 68.0
    assert rm.dynamic_limits(None, market_env="bear")["total_pct"] == 48.0
    assert any("大盘看空" in n for n in rm.dynamic_limits(None, market_env="bear")["notes"])


def test_dynamic_limits_defense_plus_bear(tmp_engine):
    """防守砍半 × 大盘看空 叠加 -> 80×0.5×0.6 = 24%."""
    rm.reset(None)
    rm.update_drawdown(100_000.0, 89_000.0, None)
    lim = rm.dynamic_limits(None, market_env="bear")
    assert lim["total_pct"] == 24.0
