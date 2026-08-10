"""交易计划生成单测 + 选股器评分单测."""

from __future__ import annotations

import re

from app import db
from app.core.plan.generator import PlanGenerator
from app.core.screener.engine import score_indicators
from app.core.signals.engine import Signal
from app.models.models import Position
from sqlmodel import select

gen = PlanGenerator()


def _backdate(symbol: str, opened_at: str = "2020-01-01 09:30:00") -> None:
    """把持仓时间改到过去, 解除 T+1 锁定."""
    with db.session_scope() as s:
        p = s.exec(select(Position).where(Position.symbol == symbol)).first()
        if p:
            p.opened_at = opened_at
            s.add(p)
            s.commit()


def _signal() -> Signal:
    return Signal(
        type="BUY_ADD", symbol="300750", name="宁德时代", direction="buy",
        strength=78.0, reason="回踩20日均线企稳,MACD未死叉", price=110.0,
        indicators_snapshot={"ma20": 105.0},
    )


def test_plan_generate_buy_add(tmp_engine):
    from app.core.position import position_manager

    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    plan = gen.generate("300750", "宁德时代", _signal(), portfolio={"total_pct": 65.0})
    assert plan["action"] == "buy_add"
    content = plan["content"]
    assert "交易计划" in content
    assert "宁德时代(300750)" in content
    assert "加仓" in content
    assert "止损价位" in content
    assert "风控检查" in content


def test_plan_buy_add_shows_second_leg_for_held_position(tmp_engine):
    """复现用户 bug: 已持仓(仅首仓)时 BUY_ADD 计划应建议第2档30%, 而非第1档50%."""
    from app.core.position import position_manager

    position_manager.open_or_add("300139", "晓程科技", 3100, 45.85, "首仓", None)
    sig = Signal(type="BUY_ADD", symbol="300139", name="晓程科技", direction="buy",
                 strength=78.0, reason="回踩20日线企稳", price=48.0)
    plan = gen.generate("300139", "晓程科技", sig, portfolio={"total_pct": 45.0})
    assert plan["action"] == "buy_add"
    assert "第 2 档" in plan["content"]
    assert "30%" in plan["content"]
    assert "第 1 档" not in plan["content"]


def test_plan_buy_add_shows_third_leg_after_one_add(tmp_engine):
    """已加一档后, BUY_ADD 计划应建议第3档20%."""
    from app.core.position import position_manager

    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "加仓", None)
    plan = gen.generate("300750", "宁德时代", _signal(), portfolio={"total_pct": 65.0})
    assert plan["action"] == "buy_add"
    assert "第 3 档" in plan["content"]
    assert "20%" in plan["content"]


def test_plan_generate_stop(tmp_engine):
    from app.core.position import position_manager

    position_manager.open_or_add("000001", "平安银行", 100, 100.0, "首仓", None)
    _backdate("000001")  # 非当日买入 -> 正常生成止损计划
    sig = Signal(type="SELL_STOP", symbol="000001", name="平安银行", direction="sell",
                 strength=90.0, reason="跌破止损线", price=90.0)
    plan = gen.generate("000001", "平安银行", sig, portfolio={"total_pct": 20.0})
    assert plan["action"] == "sell_stop"
    assert "止损" in plan["content"]


def test_plan_generate_t_plus_one_blocks_sell(tmp_engine):
    """当日买入的持仓, 减仓/止损/做T卖出计划应转为持有提示(T+1)."""
    from app.core.position import position_manager

    # opened_at 默认为今日 -> 命中 T+1 锁定
    position_manager.open_or_add("000001", "平安银行", 100, 100.0, "首仓", None)
    for sig_type in ("SELL_REDUCE", "SELL_STOP", "T_SELL"):
        sig = Signal(type=sig_type, symbol="000001", name="平安银行", direction="sell",
                     strength=85.0, reason="测试", price=90.0)
        plan = gen.generate("000001", "平安银行", sig, portfolio={"total_pct": 20.0})
        assert plan["action"] == "hold", f"{sig_type} 应被 T+1 拦截为 hold"
        assert "T+1" in plan["content"]


def test_plan_generate_buy_first(tmp_engine):
    sig = Signal(type="BUY_FIRST", symbol="600001", name="测试", direction="buy",
                 strength=72.0, reason="三共振入场", price=10.0)
    plan = gen.generate("600001", "测试", sig)
    assert plan["action"] == "buy_first"
    assert "首仓买入" in plan["content"]


def test_plan_star_board_buy_add_qty_rounded_to_lot(tmp_engine):
    """复现 bug: 科创板加仓建议不得出现 <200 股的违规数量(如 180 股), 须按板块规则取整."""
    from app.core.position import position_manager

    position_manager.open_or_add("688146", "中船特气", 421, 305.91, "首仓", None)
    sig = Signal(type="BUY_ADD", symbol="688146", name="中船特气", direction="buy",
                 strength=51.0, reason="回踩20日线企稳", price=326.90)
    plan = gen.generate("688146", "中船特气", sig,
                        portfolio={"total_pct": 40.0, "available_capital": 179796.0})
    content = plan["content"]
    m = re.search(r"约加 (\d+) 股", content)
    assert m, f"计划应给出具体加仓股数: {content}"
    assert int(m.group(1)) >= 200, f"科创板加仓须≥200股, 实际建议: {content}"
    assert "科创板单笔申报≥200股" in content


def test_plan_star_board_buy_add_insufficient_amount(tmp_engine):
    """反推金额不足以买 200 股时, 应提示不足最小申报单位而非给出违规股数."""
    from app.core.position import position_manager

    position_manager.open_or_add("688146", "中船特气", 200, 300.0, "首仓", None)
    sig = Signal(type="BUY_ADD", symbol="688146", name="中船特气", direction="buy",
                 strength=60.0, reason="回踩20日线企稳", price=310.0)
    # 2 档目标仓位较小 -> 反推金额不足 200 股
    plan = gen.generate("688146", "中船特气", sig,
                        portfolio={"total_pct": 10.0, "available_capital": 200000.0})
    content = plan["content"]
    assert "约加 0 股" not in content
    assert "不足最小申报单位" in content


def test_plan_sell_reduce_shows_compliant_qty_range(tmp_engine):
    """SELL_REDUCE 计划应给出合规的具体减仓股数区间, 并提示碎股一次性清仓规则."""
    from app.core.position import position_manager

    position_manager.open_or_add("688146", "中船特气", 421, 300.0, "首仓", None)
    _backdate("688146")  # 解除 T+1 锁定
    sig = Signal(type="SELL_REDUCE", symbol="688146", name="中船特气", direction="sell",
                 strength=70.0, reason="冲高回落减仓", price=326.90)
    plan = gen.generate("688146", "中船特气", sig, portfolio={"total_pct": 40.0})
    content = plan["content"]
    assert "建议减 140 ~ 210 股" in content
    assert "减后剩余不足 200 股" in content


# ---------------------------------------------------------------- 申万三级行业树

def test_industry_tree_grouped_by_three_levels(tmp_engine):
    """行业树按 一级->二级->三级 聚合, 每级股票数正确, 无三级时无 children 字段."""
    from app.core.classification import industry_tree
    from app.models.models import StockClassification
    from sqlmodel import Session

    with Session(db.engine) as s:
        rows = [
            StockClassification(symbol="600001", sw_l1="电子", sw_l2="半导体", sw_l3="数字芯片"),
            StockClassification(symbol="600002", sw_l1="电子", sw_l2="半导体", sw_l3="数字芯片"),
            StockClassification(symbol="600003", sw_l1="电子", sw_l2="半导体", sw_l3="模拟芯片"),
            StockClassification(symbol="600004", sw_l1="电子", sw_l2="消费电子", sw_l3=""),
            StockClassification(symbol="600005", sw_l1="医药生物", sw_l2="", sw_l3=""),
        ]
        for r in rows:
            s.add(r)
        s.commit()

    tree = industry_tree()
    names = [n["name"] for n in tree]
    assert "电子" in names and "医药生物" in names
    elec = next(n for n in tree if n["name"] == "电子")
    assert elec["count"] == 4
    l2 = {n["name"]: n for n in elec["children"]}
    assert l2["半导体"]["count"] == 3
    assert l2["消费电子"]["count"] == 1
    # 消费电子无三级 -> 无 children 字段; 半导体有两个三级叶子
    assert "children" not in l2["消费电子"]
    l3 = {n["name"]: n for n in l2["半导体"]["children"]}
    assert l3["数字芯片"]["count"] == 2
    assert l3["模拟芯片"]["count"] == 1


def test_filter_by_industry_sw_levels_and_fallback(tmp_engine):
    """行业过滤: 选中名精确命中 sw_l1/l2/l3 任一; 无映射的票回退东财包含匹配."""
    from app.core.classification import load_classification_map
    from app.core.screener.engine import StockScreener
    from app.models.models import StockClassification
    from sqlmodel import Session

    with Session(db.engine) as s:
        s.add(StockClassification(symbol="600001", sw_l1="电子", sw_l2="半导体", sw_l3="数字芯片"))
        s.add(StockClassification(symbol="600002", sw_l1="电子", sw_l2="半导体", sw_l3="模拟芯片"))
        s.commit()

    pool = [
        ("600001", "A", "半导体"),   # 有映射: 命中 sw_l3 数字芯片
        ("600002", "B", "半导体"),   # 有映射: 命中 sw_l2 半导体
        ("600003", "C", "电子"),     # 无映射: 回退东财包含匹配
        ("600004", "D", "银行"),     # 不命中
    ]
    class_map = load_classification_map([s for s, _, _ in pool])
    out = StockScreener._filter_by_industry(pool, ["半导体"], class_map)
    assert {x[0] for x in out} == {"600001", "600002"}  # 600003 东财行业"电子"不含"半导体"
    out = StockScreener._filter_by_industry(pool, ["数字芯片"], class_map)
    assert {x[0] for x in out} == {"600001"}  # 三级精确命中
    out = StockScreener._filter_by_industry(pool, ["电子"], class_map)
    assert {x[0] for x in out} == {"600001", "600002", "600003"}  # 一级 + 回退包含
    # 无关键词 -> 原样返回
    assert StockScreener._filter_by_industry(pool, [], class_map) == pool


# ---------------------------------------------------------------- 选股器评分
def test_score_indicators_uptrend(kline_df):
    from app.core.indicators import compute_all

    ind = compute_all(kline_df)
    score = score_indicators(ind)
    # 确定性上涨行情: 趋势分应显著 > 0
    assert score["trend_score"] > 10
    assert score["total"] > 0
    assert score["attention"] in ("强烈关注", "重点观察", "一般关注", "观察")


def test_score_indicators_downtrend_ranks_low():
    import numpy as np
    import pandas as pd
    from app.core.indicators import compute_all

    n = 120
    close = np.linspace(100, 50, n)
    df = pd.DataFrame({
        "date": pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d"),
        "open": close + 0.2, "high": close + 0.5, "low": close - 0.5, "close": close,
        "volume": np.full(n, 1e6), "amount": np.full(n, 1e8),
    })
    ind = compute_all(df)
    score = score_indicators(ind)
    # 下跌趋势: 动量/量能低分, 总分明显低于上涨行情(ADX 只测强度不测方向, 趋势分可高)
    assert score["total"] < 50


# ---------------------------------------------------------------- 选股扫描历史

def test_screener_history_roundtrip(tmp_engine):
    """扫描历史: 保存 -> 列表(不含结果) -> 详情(含结果) -> 删除 全链路."""
    from app.core.screener.history import (
        delete_scan_history,
        get_scan_history,
        list_scan_history,
        save_scan_history,
    )

    hid = save_scan_history(
        {"status": "done", "total": 120, "result": [
            {"symbol": "600000", "name": "浦发银行", "total": 62.5, "detail": {"趋势": "多头"}},
            {"symbol": "688146", "name": "中船特气", "total": 58.0},
        ]},
        {"market": "all", "top_n": 30, "board": "main,star", "universe": "hs300",
         "per_industry": 5, "apply_gate": True, "apply_factors": True},
    )
    items = list_scan_history()
    assert len(items) == 1
    row = items[0]
    assert row["result_count"] == 2
    assert row["universe"] == "hs300"
    assert row["board"] == "main,star"
    assert "result" not in row  # 列表不携带大 JSON

    detail = get_scan_history(hid)
    assert detail is not None
    assert len(detail["result"]) == 2
    assert detail["result"][0]["detail"]["趋势"] == "多头"
    assert detail["status"] == "done"

    assert delete_scan_history(hid) is True
    assert get_scan_history(hid) is None
    assert delete_scan_history(hid) is False  # 重复删除返回 False
