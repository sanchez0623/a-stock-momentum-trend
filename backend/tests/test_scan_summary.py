"""集成测试: 扫描结束后 self.last_scan_summary 是否正确汇总 ④ 闸门 + ⑤ 限配.

不依赖网络/数据库: 用合成 K 线与假分类映射, mock 掉真实 datasource / 指数拉取.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from app.core import screener as screener_pkg
from app.core.config import config_manager
from app.core.screener import engine as engine_mod


def _mk_kline(n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 10.0 + np.arange(n) * 0.1  # 单调上行, 保证评分>0
    return pd.DataFrame({
        "date": idx,
        "open": close - 0.02,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "volume": np.full(n, 1_000_000.0),
        "amount": np.full(n, 1.0e8),  # 1亿 >= 流动性下限
    })


def _mk_index(n: int = 30) -> pd.DataFrame:
    close = 3000.0 + np.arange(n) * 5.0
    return pd.DataFrame({"close": close})


SYMS = [f"00000{i}" for i in range(1, 7)]
_IND = ["电子", "电子", "电子", "医药", "医药", "医药"]


@pytest.fixture
def patched(monkeypatch):
    base = config_manager.get()
    cfg = {k: v for k, v in base.items()}
    cfg["择时闸门"] = {**base.get("择时闸门", {}),
                     "enabled": True, "ma_long": 20, "ma_mid": 5, "min_index_bars": 25}
    cfg["行业限配"] = base.get("行业限配", {})
    # 固定全A: 默认配置(未 load DB)的选股池可能是 sz50, 会把测试假票预筛掉
    cfg["选股池"] = {**base.get("选股池", {}), "universe": "all"}
    monkeypatch.setattr(config_manager, "get", lambda: cfg)

    calls = []

    async def fake_kline(*a, **k):
        calls.append(1)
        return _mk_kline()
    monkeypatch.setattr(engine_mod.data_source_manager, "get_kline", fake_kline)
    cfg["_calls"] = calls  # 供测试断言 mock 真的被调用

    async def fake_idx(gate_cfg):
        return {"沪深300": _mk_index(30)}
    from app.core import market_gate
    monkeypatch.setattr(market_gate, "fetch_gate_index_dfs", fake_idx)

    fake_map = {s: SimpleNamespace(sw_l1=ind, sw_l2="", sw_l3="") for s, ind in zip(SYMS, _IND, strict=True)}
    from app.core import classification
    monkeypatch.setattr(classification, "load_classification_map", lambda syms: fake_map)
    # 隔离选股池: 不预筛(本组测试只验证闸门/限配/行业过滤, 避免真实拉取指数成分股)
    from app.core import universe as universe_mod

    async def fake_ensure_universe(*a, **k):
        return set(), "test: 不预筛"
    monkeypatch.setattr(universe_mod, "ensure_universe", fake_ensure_universe)
    return cfg


def test_scan_summary_captures_gate_and_cap(patched):
    # apply_factors=False: 本测试只验证闸门/限配汇总, 与基本面/事件因子解耦(因子默认已启用)
    asyncio.run(screener_pkg.screener.scan(symbols=SYMS, per_industry=2, apply_gate=True, apply_factors=False))
    assert patched.get("_calls"), "mock get_kline 未被调用 — 说明 patch 未生效"
    summary = screener_pkg.screener.last_scan_summary

    assert summary is not None
    assert summary["scanned_at"] is not None
    assert summary["requested_top_n"] == 30
    # 牛市乘数 1.0, top_n 30, 限配后剩 4 只, 未再截断
    assert summary["final_count"] == 4

    # ④ 闸门
    g = summary["gate"]
    assert g["enabled"] is True
    assert g["applied"] is True
    assert g["environment"] == "bull"
    assert g["multiplier"] == 1.0
    assert g["details"]  # 含指数明细

    # ⑤ 限配: 6 只 -> 每组 2 -> 4 只, 触顶 2 组
    c = summary["cap"]
    assert c["enabled"] is True
    assert c["per_industry"] == 2
    assert c["level"] == "sw_l1"
    assert c["before"] == 6
    assert c["after"] == 4
    assert c["removed"] == 2
    assert len(c["capped_groups"]) == 2
    for grp in c["capped_groups"]:
        assert grp["before"] == 3 and grp["after"] == 2
        assert grp["industry"] in ("电子", "医药")


def test_scan_summary_off_when_disabled(patched):
    # 闸门/限配都不启用 -> 汇总标记未生效, 且结果不被缩减(因子亦隔离)
    asyncio.run(screener_pkg.screener.scan(symbols=SYMS, per_industry=0, apply_gate=False, apply_factors=False))
    summary = screener_pkg.screener.last_scan_summary
    assert summary["gate"]["enabled"] is False
    assert summary["gate"]["applied"] is False
    assert summary["cap"]["enabled"] is False
    assert summary["final_count"] == 6  # 全保留


def test_board_multi_value_filter(patched):
    """板块多值: board=\"main,chinext\" 只保留沪深主板+创业板, 剔除科创板."""
    syms = ["000001", "000002", "300001", "300002", "688001", "600001"]
    res = asyncio.run(screener_pkg.screener.scan(
        symbols=syms, board="main,chinext", per_industry=0, apply_gate=False, apply_factors=False))
    got = {r["symbol"] for r in res}
    assert got == {"000001", "000002", "300001", "300002", "600001"}
    assert "688001" not in got


def test_industry_multi_value_filter(patched, monkeypatch):
    """行业多值(申万三级): 有映射精确命中 sw_l1 通过; 无映射回退东财行业包含匹配.

    注意覆盖 fixture 默认 fake_map(SYMS 映射为电子/医药), 否则 000001 会因
    映射为"电子"而被精确匹配排除, 与断言矛盾.
    """
    from app.core import classification

    pool = [("000001", "A", "半导体"), ("000002", "B", "医药生物"),
            ("300001", "C", "电子"), ("300002", "D", "电力设备")]
    # 000001 有申万映射且精确命中; 300002 无映射走回退包含匹配
    monkeypatch.setattr(
        classification, "load_classification_map",
        lambda syms: {"000001": SimpleNamespace(sw_l1="半导体", sw_l2="", sw_l3="")},
    )

    async def fake_resolve(market):
        return pool
    monkeypatch.setattr(screener_pkg.screener, "_resolve_symbols", fake_resolve)

    res = asyncio.run(screener_pkg.screener.scan(
        market="all", industry="半导体,电力设备", per_industry=0, apply_gate=False, apply_factors=False))
    got = {r["symbol"] for r in res}
    assert got == {"000001", "300002"}
