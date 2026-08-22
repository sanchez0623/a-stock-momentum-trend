"""④ 择时闸门 + ⑤ 行业限配 纯函数单测(不依赖网络/数据库).

覆盖:
- apply_per_industry_cap: 分组截断、未知行业不限额、sw_l1/l2/l3 级别、顺序保持、per<=0 不生效
- compute_market_gate: 全多/全空/中性/无指数/数据不足 各分支
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from app.core.classification import apply_per_industry_cap
from app.core.market_gate import compute_market_gate


# ---------------------------------------------------------------- 测试数据构造
class _Cls:
    """极简 StockClassification 替身, 仅提供 sw_l1/l2/l3 属性."""

    def __init__(self, sw_l1="", sw_l2="", sw_l3="", industry=""):
        self.sw_l1 = sw_l1
        self.sw_l2 = sw_l2
        self.sw_l3 = sw_l3
        self.industry = industry


def _mk_results(scores: list[tuple[str, float]]) -> list[dict]:
    """构造已按 total 降序的扫描结果."""
    return [{"symbol": s, "total": t} for s, t in scores]


def _mk_index_df(n: int, trend: str = "up") -> pd.DataFrame:
    """构造指数 K 线(含 close, 行数 n). trend=up 走牛, down 走熊."""
    dates = pd.bdate_range("2024-01-01", periods=n)
    if trend == "up":
        close = np.linspace(100, 200, n)  # 单调上行
    else:
        close = np.linspace(200, 100, n)  # 单调下行
    open_ = close
    high = close + 1.0
    low = close - 1.0
    volume = np.full(n, 1_000_000.0)
    amount = volume * close
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount,
    })


# ---------------------------------------------------------------- ⑤ 行业限配
def test_cap_disabled_when_zero():
    results = _mk_results([("A", 90), ("B", 80), ("C", 70)])
    cmap = {"A": _Cls("电子"), "B": _Cls("电子"), "C": _Cls("医药")}
    out = apply_per_industry_cap(results, cmap, 0, "sw_l1")
    assert [r["symbol"] for r in out] == ["A", "B", "C"]


def test_cap_basic_sw_l1():
    # 电子 3 只(按分降序), 医药 1 只; 每组限 2
    results = _mk_results([("A", 95), ("B", 90), ("C", 85), ("D", 80)])
    cmap = {"A": _Cls("电子"), "B": _Cls("电子"), "C": _Cls("电子"), "D": _Cls("医药")}
    out = apply_per_industry_cap(results, cmap, 2, "sw_l1")
    syms = [r["symbol"] for r in out]
    assert syms == ["A", "B", "D"]   # 电子只留前 2, 医药全留
    assert len(out) == 3


def test_cap_preserves_order():
    results = _mk_results([("z", 99), ("a", 98), ("m", 97)])
    cmap = {"z": _Cls("电子"), "a": _Cls("电子"), "m": _Cls("电子")}
    out = apply_per_industry_cap(results, cmap, 1, "sw_l1")
    assert [r["symbol"] for r in out] == ["z"]  # 仅最高分入选, 顺序不变


def test_cap_unknown_not_limited():
    # 未知行业(无映射)不计入限额, 全部保留
    results = _mk_results([("X", 99), ("Y", 98), ("Z", 97)])
    cmap = {}  # 全部未知
    out = apply_per_industry_cap(results, cmap, 1, "sw_l1")
    assert [r["symbol"] for r in out] == ["X", "Y", "Z"]


def test_cap_levels_sw_l2_sw_l3():
    # sw_l2 分组
    results = _mk_results([("A", 95), ("B", 90), ("C", 85)])
    cmap = {"A": _Cls(sw_l2="半导体"), "B": _Cls(sw_l2="半导体"), "C": _Cls(sw_l2="消费电子")}
    out = apply_per_industry_cap(results, cmap, 1, "sw_l2")
    assert [r["symbol"] for r in out] == ["A", "C"]

    # sw_l3 分组
    results = _mk_results([("A", 95), ("B", 90), ("C", 85)])
    cmap = {"A": _Cls(sw_l3="IC设计"), "B": _Cls(sw_l3="IC设计"), "C": _Cls(sw_l3="面板")}
    out = apply_per_industry_cap(results, cmap, 1, "sw_l3")
    assert [r["symbol"] for r in out] == ["A", "C"]


# ---------------------------------------------------------------- ④ 择时闸门
def test_gate_all_bull():
    dfs = {"沪深300": _mk_index_df(250, "up"), "创业板指": _mk_index_df(250, "up")}
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3})
    assert g["environment"] == "bull"
    assert g["multiplier"] == 1.0
    assert g["details"][0]["above_ma"] is True
    assert g["details"][0]["aligned"] is True


def test_gate_all_bear():
    dfs = {"沪深300": _mk_index_df(250, "down"), "创业板指": _mk_index_df(250, "down")}
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3})
    assert g["environment"] == "bear"
    assert g["multiplier"] == 0.3


def test_gate_neutral_mixed():
    # 一多一空 -> 中性, 乘数取均值
    dfs = {"沪深300": _mk_index_df(250, "up"), "创业板指": _mk_index_df(250, "down")}
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3})
    assert g["environment"] == "neutral"
    assert g["multiplier"] == (1.0 + 0.3) / 2


def test_gate_no_indices():
    g = compute_market_gate({}, {"bull_top_n_ratio": 1.0})
    assert g["environment"] == "neutral"
    assert g["multiplier"] == 1.0
    assert "不生效" in g["reason"]


def test_gate_all_insufficient_data():
    # 全部数据不足(冷启动/缓存未命中): 闸门降级为中性, 不误判空头
    dfs = {"沪深300": _mk_index_df(50, "up")}  # < ma_long(200)
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60})
    assert g["details"][0]["status"] == "数据不足"
    assert g["details"][0]["above_ma"] is None
    assert g["environment"] == "neutral"
    assert g["multiplier"] == 1.0
    assert "不生效" in g["reason"]


def test_gate_partial_insufficient_bull():
    # 1 个有效走牛 + 1 个数据不足 -> 有效指数全牛, 应判 bull(不足不计入空头)
    dfs = {"沪深300": _mk_index_df(250, "up"), "创业板指": _mk_index_df(50, "up")}
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3})
    assert g["environment"] == "bull"
    assert g["multiplier"] == 1.0


def test_gate_partial_insufficient_bear():
    # 1 个有效走熊 + 1 个数据不足 -> 有效指数全熊, 应判 bear
    dfs = {"沪深300": _mk_index_df(250, "down"), "创业板指": _mk_index_df(50, "up")}
    g = compute_market_gate(dfs, {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3})
    assert g["environment"] == "bear"
    assert g["multiplier"] == 0.3


if __name__ == "__main__":
    import sys

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(funcs)-failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
