"""三期测试: 交易日志双写 + 统计 + 评分."""

from __future__ import annotations

from app.core.logger import trade_logger
from app.core.stats import stats
from app.core.tradelog import trade_log


def _seed_trades(tmp_engine):
    """构造 3 笔完整回合: 买A -> 卖A(盈利) / 买B -> 卖B(亏损) / 买C(持有中)."""
    trade_logger.manual_entry("000001", "平安银行", "buy", 10.0, 1000, "首仓")
    trade_logger.manual_entry("000001", "平安银行", "sell", 11.0, 1000, "止盈")
    trade_logger.manual_entry("000002", "万科A", "buy", 20.0, 500, "首仓")
    trade_logger.manual_entry("000002", "万科A", "sell", 18.0, 500, "止损")
    trade_logger.manual_entry("000003", "测试C", "buy", 30.0, 300, "首仓")


# ---------------------------------------------------------------- 双写
def test_trade_logger_dual_write(tmp_engine):
    _seed_trades(tmp_engine)
    rows = trade_log.list_trades(limit=100)
    assert len(rows) == 5
    # CSV 实时追加(双写)
    csv_text = trade_logger.export_csv()
    assert "平安银行" in csv_text
    assert csv_text.splitlines()[0].startswith("time")  # 英文表头


def test_manual_entry_sell_updates_position(tmp_engine):
    _seed_trades(tmp_engine)
    pos = trade_logger._position().get_position("000001")
    assert pos is None  # 已清仓
    pos_b = trade_logger._position().get_position("000002")
    assert pos_b is None  # 已清仓
    pos_c = trade_logger._position().get_position("000003")
    assert pos_c is not None and pos_c.qty == 300


def test_manual_entry_sell_without_position_raises(tmp_engine):
    import pytest
    from app.core.position.manager import PositionManagerError

    with pytest.raises(PositionManagerError):
        trade_logger.manual_entry("999999", "", "sell", 10.0, 100)


# ---------------------------------------------------------------- 统计
def test_stats_summary(tmp_engine):
    _seed_trades(tmp_engine)
    s = stats.summary()
    assert s["trades"] == 2  # 2 笔已平仓
    assert s["wins"] == 1
    assert s["losses"] == 1
    assert s["total_pnl"] == (11.0 - 10.0) * 1000 + (18.0 - 20.0) * 500  # 1000 - 1000 = 0
    assert s["win_rate"] == 50.0


def test_stats_equity_curve(tmp_engine):
    _seed_trades(tmp_engine)
    curve = stats.equity_curve()
    assert len(curve) == 3  # start + 2 平仓点
    assert curve[-1]["equity"] == 0.0


def test_stats_monthly_heatmap(tmp_engine):
    _seed_trades(tmp_engine)
    heat = stats.monthly_heatmap()
    months = heat["months"]
    assert len(months) == 1
    assert months[0]["trades"] == 2


def test_stats_signal_distribution(tmp_engine):
    from app import db
    from app.models.models import SignalRecord
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(SignalRecord(symbol="000001", type="BUY_FIRST", direction="buy", strength=80))
        s.add(SignalRecord(symbol="000002", type="SELL_STOP", direction="sell", strength=90))
        s.commit()
    dist = stats.signal_distribution()
    m = {d["type"]: d["count"] for d in dist}
    assert m.get("BUY_FIRST") == 1
    assert m.get("SELL_STOP") == 1


# ---------------------------------------------------------------- 评分
def test_stats_trade_scores(tmp_engine):
    _seed_trades(tmp_engine)
    result = stats.trade_scores()
    items = result["items"]
    assert len(items) == 2
    # 盈利单分数应高于亏损单
    win_item = next(i for i in items if i["pnl"] > 0)
    loss_item = next(i for i in items if i["pnl"] < 0)
    assert win_item["score"] > loss_item["score"]
    assert 0 <= result["health"] <= 100
