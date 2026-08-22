"""信号审计回测单测(方案 v2 §6): 真实 vs 纪律两线 / 违背·滞后·提前分类."""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.backtest import audit as au
from app.models.models import Trade


def _kline_df(close_list: list[float], base_date: str = "2024-01-02") -> pd.DataFrame:
    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume = np.full(len(close), 5_000_000.0)
    dates = pd.bdate_range(base_date, periods=len(close))
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close,
    })


def _up_series() -> pd.DataFrame:
    """横盘 70 根后一路上涨(10 -> 20), 供上涨票场景."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    rise = [10.0 + i * 0.055 for i in range(1, 181)]
    return _kline_df(flat + rise)


def _down_series() -> pd.DataFrame:
    """横盘 -> 小跌 -> 强反弹(触发纪律 BUY_FIRST) -> 崩盘(触发 SELL_STOP 建议)."""
    flat = [10.0 + np.sin(i / 8) * 0.15 for i in range(70)]
    dip = [10.0 - i * 0.05 for i in range(1, 21)]            # 10 -> 9
    rally = [9.0 + i * 0.067 for i in range(1, 31)]          # 9 -> 11(强多头)
    crash = [11.0 - i * 0.10 for i in range(1, 61)]          # 11 -> 5
    return _kline_df(flat + dip + rally + crash)


class _FakeStore:
    def __init__(self, series: dict[str, pd.DataFrame]) -> None:
        self.series = series

    def ensure_range(self, symbol: str, start: str = "", end: str = "",
                     period: str = "daily", adjust: str = "qfq",
                     force: bool = False, secid: str | None = None) -> dict:
        df = self.series.get(symbol)
        if df is None or df.empty:
            return {"symbol": symbol, "source": "none", "rows": [], "row_count": 0,
                    "fetched": 0, "note": ""}
        rows = df.to_dict("records")
        return {"symbol": symbol, "source": "backtest_kline", "rows": rows,
                "row_count": len(rows), "fetched": 0, "note": ""}


def _seed_trades(tmp_engine) -> None:
    """真实成交: UP1 上涨中提前卖出(纪律无卖出建议 -> 提前);
    DOWN1 下跌中持有不动(纪律建议止损 -> 违背)."""
    from app import db

    up = _up_series()
    down = _down_series()
    with db.session_scope() as s:
        s.add(Trade(time=f"{up['date'].iloc[90]} 10:00:00", symbol="UP1", name="上行票",
                    action="buy", price=float(up["close"].iloc[90]), qty=1000,
                    amount=float(up["close"].iloc[90]) * 1000, fee=5.0))
        s.add(Trade(time=f"{up['date'].iloc[110]} 10:00:00", symbol="UP1", name="上行票",
                    action="sell", price=float(up["close"].iloc[110]), qty=1000,
                    amount=float(up["close"].iloc[110]) * 1000, fee=8.0))
        # 反弹段买入(约 index 95, 价格 ~9.7), 之后死扛崩盘
        s.add(Trade(time=f"{down['date'].iloc[95]} 10:00:00", symbol="DOWN1", name="下行票",
                    action="buy", price=float(down["close"].iloc[95]), qty=1000,
                    amount=float(down["close"].iloc[95]) * 1000, fee=5.0))
        s.commit()


def _fake_data(monkeypatch) -> None:
    up = _up_series()
    down = _down_series()
    store = _FakeStore({"UP1": up, "DOWN1": down})
    monkeypatch.setattr(au.backtest_data, "ensure_range", store.ensure_range)


def test_audit_two_lines_and_deviations(tmp_engine, monkeypatch):
    """两线曲线存在; UP1 提前卖出(提前), DOWN1 未止损(违背)."""
    _seed_trades(tmp_engine)
    _fake_data(monkeypatch)

    rep = au.run_signal_audit()
    assert "error" not in rep, rep
    assert rep["meta"]["symbols"] == 2
    assert len(rep["curves"]["real"]) == len(rep["curves"]["discipline"]) > 100
    assert rep["curves"]["real"][-1]["equity"] != 0

    by_sym = {b["symbol"]: b for b in rep["by_symbol"]}
    assert set(by_sym) == {"UP1", "DOWN1"}
    # DOWN1: 纪律止损应显著优于真实死扛
    assert by_sym["DOWN1"]["discipline_return_pct"] > by_sym["DOWN1"]["real_return_pct"], by_sym["DOWN1"]

    kinds = {(a["symbol"], a["deviation"]) for a in rep["audits"]}
    assert ("UP1", "提前") in kinds, f"UP1 上涨中卖出应判提前: {kinds}"
    assert ("DOWN1", "违背") in kinds, f"DOWN1 建议止损未执行应判违背: {kinds}"
    # 审计表字段完整
    for a in rep["audits"]:
        assert a["date"] and a["symbol"] and a["real_action"] and a["deviation"]


def test_audit_no_trades(tmp_engine):
    """无真实成交: 返回 error."""
    rep = au.run_signal_audit()
    assert "error" in rep


def test_audit_api(tmp_engine, monkeypatch):
    """API 层: POST /api/backtest/audit 正常返回两线报告."""
    from app.main import app
    from fastapi.testclient import TestClient

    _seed_trades(tmp_engine)
    _fake_data(monkeypatch)
    c = TestClient(app)
    r = c.post('/api/backtest/audit', json={})
    d = r.json()
    assert d["code"] == 0, d
    data = d["data"]
    assert "curves" in data and "audits" in data and "stats" in data
    assert data["stats"]["audit_count"] > 0
