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


def test_plan_unknown_mode_blocks_buy_but_allows_stop(tmp_engine):
    """市况不明(unknown): 买入类信号转观望(hold), 卖出/止损类照常(风控优先)."""
    from app.core.modes import ModeDecision

    unknown = ModeDecision(
        mode_key="unknown",
        mode={"label": "市况不明", "pyramid_ratios": [0.5, 0.3, 0.2],
              "max_stages": 0, "stop_loss_pct": 5.0, "trailing_stop_pct": 8.0,
              "take_profit_ratios": [0.2, 0.3, 0.5], "atr_multipliers": [1.5, 3.0, 5.0]},
        regime={}, reason="市况不明", label="市况不明",
    )

    # 买入类 -> hold 观望
    buy_sig = Signal(type="BUY_FIRST", symbol="600001", name="测试", direction="buy",
                     strength=80.0, reason="三共振入场", price=10.0)
    plan = gen.generate("600001", "测试", buy_sig, mode=unknown)
    assert plan["action"] == "hold"
    assert "观望" in plan["content"]

    # 止损类 -> 照常给止损计划
    from app.core.position import position_manager

    position_manager.open_or_add("600001", "测试", 100, 10.0, "首仓", None)
    _backdate("600001")
    stop_sig = Signal(type="SELL_STOP", symbol="600001", name="测试", direction="sell",
                      strength=90.0, reason="跌破止损线", price=9.0)
    plan = gen.generate("600001", "测试", stop_sig, mode=unknown)
    assert plan["action"] == "sell_stop"
    assert "止损" in plan["content"]


def test_plan_consistency_line(tmp_engine):
    """一致性行: 信号与计划一致显示 ✓; 不一致时给出拦截原因(档位用尽/市况不明)."""
    from app.core.modes import ModeDecision
    from app.core.position import position_manager

    # ① 一致: 首仓信号 -> 建仓计划
    sig = Signal(type="BUY_FIRST", symbol="600001", name="测试", direction="buy",
                 strength=72.0, reason="三共振入场", price=10.0)
    plan = gen.generate("600001", "测试", sig)
    assert "一致性: 信号→计划一致 ✓ (建仓)" in plan["content"]

    # ② 不一致-档位用尽: 两档加仓都用尽后 BUY_ADD -> 观望
    position_manager.open_or_add("600001", "测试", 100, 10.0, "首仓", None)
    position_manager.open_or_add("600001", "测试", 100, 11.0, "加仓1", None)
    position_manager.open_or_add("600001", "测试", 100, 12.0, "加仓2", None)
    add_sig = Signal(type="BUY_ADD", symbol="600001", name="测试", direction="buy",
                     strength=76.0, reason="回踩企稳", price=12.0)
    plan = gen.generate("600001", "测试", add_sig)
    assert "一致性: 信号建议加仓, 计划观望: 金字塔档位已用尽" in plan["content"]

    # ③ 不一致-市况不明: unknown 模式下买入类转观望
    unknown = ModeDecision(
        mode_key="unknown",
        mode={"label": "市况不明", "pyramid_ratios": [0.5, 0.3, 0.2], "max_stages": 0,
              "stop_loss_pct": 5.0, "trailing_stop_pct": 8.0,
              "take_profit_ratios": [0.2, 0.3, 0.5], "atr_multipliers": [1.5, 3.0, 5.0]},
        regime={}, reason="市况不明", label="市况不明",
    )
    plan = gen.generate("600001", "测试", sig, mode=unknown)
    assert "一致性: 信号建议建仓, 计划观望: 市况特征不明确, 观望" in plan["content"]


def test_capital_note_caps_by_dynamic_total_limit():
    """加仓建议按动态总仓位上限自动缩减股数并注明(不再只显示不管)."""
    import re as _re
    from types import SimpleNamespace

    # 当前仓位 75%, 启动资金 10w, 动态上限 80% -> 只允许再加 5% = 5000 元
    pos = SimpleNamespace(pyramid_stage=0, qty=1000, cost=10.0)
    note = PlanGenerator._capital_aware_add_note(
        pos, [0.5, 0.3, 0.2], 1,
        {"total_pct": 75.0, "start_capital": 100000.0, "available_capital": 50000.0},
        "600519", {"total_pct": 80.0},
    )
    # 未缩减时会建议 600 股(超限), 缩减后为 500 股(恰至上限)
    m = _re.search(r"约加 (\d+) 股", note)
    assert m and int(m.group(1)) == 500
    assert "已按总仓位上限 80% 缩减" in note

    # 已近上限 -> 缩减后不足最小单位, 建议不加仓
    note = PlanGenerator._capital_aware_add_note(
        pos, [0.5, 0.3, 0.2], 1,
        {"total_pct": 79.5, "start_capital": 100000.0, "available_capital": 50000.0},
        "600519", {"total_pct": 80.0},
    )
    assert "本次建议暂不加仓" in note


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
    # 1/3=140 不足科创板 200 股一单 -> 只给 1/2 档 200 股(取整到 200 整数倍)
    assert "建议减 200 股" in content
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


# ---------------------------------------------------------------- 条件组合预设 + universe 多值

def test_universe_multi_value_parse():
    """universe 多值: 逗号分隔解析/去重/组合 key 兼容/全A 短路."""
    from app.core.universe import parse_universe, universe_label

    assert parse_universe("hs300,zz500") == ["hs300", "zz500"]
    assert parse_universe("hs300,hs300,sz50") == ["hs300", "sz50"]  # 去重保序
    assert parse_universe("hs300+zz500") == ["hs300", "zz500"]      # 预置组合 key 兼容
    assert parse_universe("all") == []
    assert parse_universe("hs300,all") == []                          # 含 all 即全A
    assert parse_universe("hs300,unknown") == ["hs300"]              # 未知名忽略
    assert universe_label("hs300,zz500") == "沪深300+中证500"
    assert universe_label("all") == "全A"


def test_screener_preset_roundtrip(tmp_engine):
    """预设: 保存 -> 列表 -> 同名覆盖 -> 删除 全链路."""
    from app.core.screener.presets import delete_preset, list_presets, save_preset

    pid = save_preset("成长池", "hs300,zz500", "star", "半导体,数字芯片设计")
    items = list_presets()
    assert len(items) == 1
    assert items[0]["name"] == "成长池"
    assert items[0]["universe"] == "hs300,zz500"
    assert items[0]["industry"] == "半导体,数字芯片设计"

    # 同名保存 = 覆盖(不新增)
    pid2 = save_preset("成长池", "sz50", "main", "银行")
    assert pid2 == pid
    items = list_presets()
    assert len(items) == 1
    assert items[0]["universe"] == "sz50"

    assert delete_preset(pid) is True
    assert list_presets() == []
    assert delete_preset(pid) is False


# ---------------------------------------------------------------- 断点续传持久化

def test_screener_task_persistence_roundtrip(tmp_engine):
    """任务落库 -> 批次追加 -> 恢复读取 -> 中断自愈 -> 删除 全链路."""
    from app.core.screener import persistence as pers

    pers.create_task("t1", {"market": "all", "top_n": 30, "universe": "hs300",
                            "board": "main", "industry": "半导体"}, ["600000", "600001", "600002"])
    pers.append_batch("t1", 1, [{"symbol": "600000", "total": 70.0}, {"symbol": "600001", "total": 65.0}])
    pers.update_task("t1", done=2)

    # 恢复: 任务参数 + 结果批次(结果即进度 -> 已完成票集合)
    t = pers.load_task("t1")
    assert t is not None and t["done"] == 2 and t["universe"] == "hs300"
    batches = pers.load_batches("t1")
    assert [r["symbol"] for r in batches] == ["600000", "600001"]

    # 启动自愈: running -> interrupted
    pers.update_task("t1", status="running")
    assert pers.mark_all_running_interrupted() == 1
    items = pers.list_interrupted()
    assert len(items) == 1 and items[0]["task_id"] == "t1" and items[0]["done"] == 2

    pers.delete_task("t1")
    assert pers.load_task("t1") is None
    assert pers.load_batches("t1") == []


def test_screener_task_batch_resume_skips_done(tmp_engine, monkeypatch):
    """恢复扫描: 跳过已完成票, 结果从批次恢复后继续追加新批次."""
    from app.core.screener import persistence as pers

    pers.create_task("t2", {"market": "all", "top_n": 10}, ["600000", "600001", "600002", "600003"])
    pers.append_batch("t2", 1, [{"symbol": "600000", "total": 70.0}])
    done = {r["symbol"] for r in pers.load_batches("t2")}
    assert done == {"600000"}

    # 模拟恢复后的扫描: 新结果来自剩余票, 追加为第 2 批
    pers.append_batch("t2", 2, [{"symbol": "600001", "total": 66.0}])
    all_results = pers.load_batches("t2")
    assert [r["symbol"] for r in all_results] == ["600000", "600001"]  # 已恢复 + 新增
    assert "600002" not in {r["symbol"] for r in all_results}
    pers.delete_task("t2")


# ---------------------------------------------------------------- 得分追踪
def test_tracking_add_list_stop_archive(tmp_engine):
    """追踪: 添加(不重复) -> 列表(含最近采样) -> 手动停止 -> 观察期自动归档."""
    from app.core.tracking import score_tracker as tr

    # 添加(两次同票不重复)
    d1 = tr.track("600000", "浦发银行", 72.0, "launch")
    d2 = tr.track("600000", "浦发银行", 72.0, "launch")
    assert d1["symbol"] == d2["symbol"]
    assert len(tr.list_active()) == 1

    # 采样点写入
    from app import db
    from app.models.models import ScorePoint
    from sqlmodel import select

    with db.session_scope() as s:
        s.add(ScorePoint(symbol="600000", time="2026-08-11 12:30:00", score=70.5,
                         trend_score=38.0, momentum_score=21.0, volume_score=11.5,
                         stage="launch", price=10.2, volume_ratio=1.3, sample_kind="noon"))
        s.commit()
    items = tr.list_active()
    assert items[0]["latest"]["score"] == 70.5
    assert items[0]["latest"]["sample_kind"] == "noon"

    # 时间序列
    pts = tr.points("600000")
    assert len(pts) == 1 and pts[0]["price"] == 10.2

    # 手动停止
    assert tr.stop("600000") is True
    assert tr.list_active() == []
    assert tr.stop("600000") is False  # 已归档, 再停返回 False

    # 重新追踪 + 观察期自动归档(构造过期 track_time)
    tr.track("600000", "浦发银行", 72.0, "launch")
    with db.session_scope() as s:
        t = s.exec(select(tr.TrackedStock).where(
            tr.TrackedStock.symbol == "600000", tr.TrackedStock.status == "active"
        )).first()
        t.track_time = "2026-06-01 10:00:00"  # 超过 30 天
        s.add(t)
        s.commit()
    assert tr.archive_expired() == 1
    assert tr.list_active() == []


def test_tracking_delete_point(tmp_engine):
    """删除单条采样点: 存在则删, 不存在返回 False."""
    from app.core.tracking import score_tracker as tr

    tr.track("600000", "浦发银行", 72.0, "launch")
    from app.models.models import ScorePoint

    with db.session_scope() as s:
        p = ScorePoint(symbol="600000", time="2026-08-11 12:30:00", score=70.5, stage="launch",
                       price=10.2, volume_ratio=1.3, sample_kind="noon")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    assert len(tr.points("600000")) == 1
    assert tr.delete_point(pid) is True
    assert tr.points("600000") == []
    assert tr.delete_point(pid) is False  # 已删, 再删 False


def test_tracking_sim_state_machine(tmp_engine, monkeypatch):
    """模拟交易状态机: BUY_FIRST 建仓 -> 有持仓后不再重复 BUY_FIRST -> SELL_STOP 平仓结算."""
    import asyncio

    from app.core.signals.engine import Signal
    from app.core.tracking import score_tracker as tr

    tr.track("600000", "浦发银行", 72.0, "launch")

    calls = {"n": 0}

    async def fake_score(symbol, cfg):
        calls["n"] += 1
        return {"total": 66.0, "trend_score": 25.0, "momentum_score": 30.0,
                "volume_score": 11.0, "stage": "launch", "close": 10.0,
                "volume_ratio": 1.2, "_kline": None}

    monkeypatch.setattr(tr, "_score_symbol", fake_score)

    # ① 第一次采样: 空仓 + BUY_FIRST -> 模拟建仓
    def fake_eval1(self, symbol, kline_df=None, position=None, **kw):
        assert position is None  # 空仓视角
        return Signal(type="BUY_FIRST", symbol=symbol, direction="buy", strength=70.0)

    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval1)
    r1 = asyncio.run(tr.sample_one("600000"))
    assert r1["sim_action"] == "open" and r1["sim_qty"] == 10000 and r1["sim_cost"] == 10.0

    # ② 第二次采样(同一天): 已持仓 -> 传入模拟持仓视角; 即使引擎报 BUY_FIRST 也被当日去重拦截(一天只一个动作)
    def fake_eval2(self, symbol, kline_df=None, position=None, **kw):
        assert position is not None and position.qty == 10000  # 持仓视角
        return Signal(type="BUY_FIRST", symbol=symbol, direction="buy", strength=70.0)

    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval2)
    r2 = asyncio.run(tr.sample_one("600000"))
    assert r2["sim_action"] == "hold" and r2["sim_qty"] == 10000  # 同日去重, 不重复建仓

    # ③ 次日(清除当日去重标记) SELL_STOP -> 全平结算
    from app.models.models import TrackedStock
    from sqlmodel import select

    with db.session_scope() as s:
        st = s.exec(select(TrackedStock).where(TrackedStock.symbol == "600000")).first()
        st.sim_last_action_date = "2020-01-01"  # 模拟次日
        s.add(st)
        s.commit()

    def fake_eval3(self, symbol, kline_df=None, position=None, **kw):
        return Signal(type="SELL_STOP", symbol=symbol, direction="sell", strength=90.0)

    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval3)
    async def fake_score3(symbol, cfg):
        return {"total": 50.0, "trend_score": 15.0, "momentum_score": 20.0,
                "volume_score": 15.0, "stage": "exhaust", "close": 9.5,
                "volume_ratio": 0.8, "_kline": None}

    monkeypatch.setattr(tr, "_score_symbol", fake_score3)
    r3 = asyncio.run(tr.sample_one("600000"))
    assert r3["sim_action"] == "close" and r3["sim_qty"] == 0
    assert abs(r3["sim_pnl"] - (-5.0)) < 0.01  # (9.5/10-1)*100 = -5%
    # 衰竭期自动归档: 结算价=本次采样价(9.5), final_pnl = 已实现 -5%
    assert r3["archived"] is True
    hist = tr.list_history()
    assert len(hist) == 1 and hist[0]["archive_reason"] == "exhaust"
    assert abs(hist[0]["final_pnl"] - (-5.0)) < 0.01
    assert hist[0]["final_stage"] == "exhaust"
    assert abs(hist[0]["hold_pnl"] - (-5.0)) < 0.01  # 10.0 -> 9.5

    # ④ 归档后(观察已结束)不再采样: stock 非 active, sample_one 返回 None
    monkeypatch.setattr(tr, "_score_symbol", fake_score)  # 恢复 10 元价格

    def fake_eval4(self, symbol, kline_df=None, position=None, **kw):
        return Signal(type="BUY_FIRST", symbol=symbol, direction="buy", strength=70.0)

    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval4)
    r4 = asyncio.run(tr.sample_one("600000"))
    assert r4 is None
    assert tr.list_active() == []  # 已归档不在活跃列表


def test_tracking_sample_one_skips_bad_symbol(tmp_engine, monkeypatch):
    """采样: 评分失败(数据不足)的票跳过, 不写入采样点; 正常票写入."""
    from app.core.tracking import score_tracker as tr

    tr.track("600000", "浦发银行", 72.0, "launch")

    async def fake_score(symbol, cfg):
        return None  # 模拟数据不足

    monkeypatch.setattr(tr, "_score_symbol", fake_score)

    import asyncio

    r = asyncio.run(tr.sample_all())
    assert r["total"] == 1 and r["failed"] == 1
    assert tr.points("600000") == []  # 无采样点写入


def test_tracking_sample_maps_score_fields(tmp_engine, monkeypatch):
    """采样字段与 score_indicators 返回对齐(trend_score/close 等, 防历史 0 值回归)."""
    import asyncio

    from app.core.tracking import score_tracker as tr

    tr.track("600547", "山东黄金", 65.8, "launch")

    async def fake_score(symbol, cfg):
        return {
            "total": 63.6, "trend_score": 22.5, "momentum_score": 25.0,
            "volume_score": 8.1, "stage": "launch", "close": 42.35,
            "volume_ratio": 1.02, "_kline": None,
        }

    monkeypatch.setattr(tr, "_score_symbol", fake_score)
    out = asyncio.run(tr.sample_one("600547"))
    assert out is not None
    pts = tr.points("600547")
    assert len(pts) == 1
    p = pts[0]
    assert p["trend_score"] == 22.5
    assert p["momentum_score"] == 25.0
    assert p["volume_score"] == 8.1
    assert p["price"] == 42.35  # 不再为 0
    assert p["score"] == 63.6


def test_tracking_stage_sub_passthrough(tmp_engine, monkeypatch):
    """加速期细分透传: track 存 stage_sub_at_track; 采样点/最新快照/列表均带 stage_sub 与 trend_age."""
    import asyncio

    from app.core.tracking import score_tracker as tr

    d = tr.track("600000", "浦发银行", 66.0, "accelerate", stage_sub="mid")
    assert d["stage_sub_at_track"] == "mid"

    async def fake_score(symbol, cfg):
        return {"total": 68.0, "trend_score": 26.0, "momentum_score": 30.0,
                "volume_score": 12.0, "stage": "accelerate", "stage_sub": "mid",
                "trend_age": 18, "close": 11.0, "volume_ratio": 1.1, "_kline": None}

    def fake_eval(self, symbol, kline_df=None, position=None, **kw):
        return None

    monkeypatch.setattr(tr, "_score_symbol", fake_score)
    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval)
    out = asyncio.run(tr.sample_one("600000"))
    assert out is not None

    pts = tr.points("600000")
    assert pts[0]["stage_sub"] == "mid" and pts[0]["trend_age"] == 18

    items = tr.list_active()
    assert items[0]["latest"]["stage_sub"] == "mid"
    assert items[0]["latest"]["trend_age"] == 18
    assert items[0]["stage_sub_at_track"] == "mid"


def test_tracking_exhaust_archives_with_open_position(tmp_engine, monkeypatch):
    """衰竭自动归档时若仍持仓: 按采样价平仓, 浮盈计入 final_pnl(不只已实现)."""
    import asyncio

    from app.core.signals.engine import Signal
    from app.core.tracking import score_tracker as tr

    tr.track("600000", "浦发银行", 72.0, "accelerate")

    # 先建仓(10 元)
    async def fake_score_open(symbol, cfg):
        return {"total": 66.0, "trend_score": 25.0, "momentum_score": 30.0,
                "volume_score": 11.0, "stage": "accelerate", "close": 10.0,
                "volume_ratio": 1.2, "_kline": None}

    def fake_eval_open(self, symbol, kline_df=None, position=None, **kw):
        return Signal(type="BUY_FIRST", symbol=symbol, direction="buy", strength=70.0)

    monkeypatch.setattr(tr, "_score_symbol", fake_score_open)
    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval_open)
    r1 = asyncio.run(tr.sample_one("600000"))
    assert r1["sim_action"] == "open" and r1["sim_qty"] > 0

    # 次日衰竭(无卖出信号, 持仓悬浮) -> 自动归档, 浮盈按 12 元结算
    with db.session_scope() as s:
        st = s.exec(select(tr.TrackedStock).where(
            tr.TrackedStock.symbol == "600000", tr.TrackedStock.status == "active"
        )).first()
        st.sim_last_action_date = "2020-01-01"
        s.add(st)
        s.commit()

    async def fake_score_exhaust(symbol, cfg):
        return {"total": 45.0, "trend_score": 12.0, "momentum_score": 18.0,
                "volume_score": 15.0, "stage": "exhaust", "close": 12.0,
                "volume_ratio": 0.9, "_kline": None}

    def fake_eval_none(self, symbol, kline_df=None, position=None, **kw):
        return None

    monkeypatch.setattr(tr, "_score_symbol", fake_score_exhaust)
    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval_none)
    r2 = asyncio.run(tr.sample_one("600000"))
    assert r2["archived"] is True
    assert r2["sim_action"] == "hold"  # 无卖出信号, 采样动作是 hold
    # 结算: 建仓 10 -> 归档价 12 = +20% 浮盈计入 final_pnl
    hist = tr.list_history()
    assert abs(hist[0]["final_pnl"] - 20.0) < 0.01
    assert hist[0]["final_stage"] == "exhaust"


def test_tracking_exhaust_switch_off(tmp_engine, monkeypatch):
    """配置关闭衰竭自动归档: 衰竭期采样照常, 不归档.

    注意: config_manager.get() 返回 deepcopy, 直接改副本无效;
    这里 monkeypatch get() 返回关闭开关的配置副本。
    """
    import asyncio

    import copy as _copy

    from app.core.config import config_manager
    from app.core.tracking import score_tracker as tr

    cfg_off = _copy.deepcopy(config_manager.get())
    cfg_off["追踪"]["auto_archive_on_exhaust"] = False
    monkeypatch.setattr(tr.config_manager, "get", lambda: cfg_off)

    tr.track("600000", "浦发银行", 72.0, "accelerate")

    async def fake_score(symbol, cfg):
        return {"total": 45.0, "trend_score": 12.0, "momentum_score": 18.0,
                "volume_score": 15.0, "stage": "exhaust", "close": 12.0,
                "volume_ratio": 0.9, "_kline": None}

    def fake_eval(self, symbol, kline_df=None, position=None, **kw):
        return None

    monkeypatch.setattr(tr, "_score_symbol", fake_score)
    monkeypatch.setattr(tr.SignalEngine, "evaluate", fake_eval)
    r = asyncio.run(tr.sample_one("600000"))
    assert r is not None and r["archived"] is False
    assert len(tr.list_active()) == 1  # 仍活跃


def test_tracking_stop_settles_open_position(tmp_engine):
    """手动停止: 持仓按最近采样价结算, final_pnl = 已实现 + 平仓浮盈."""
    from app.core.tracking import score_tracker as tr
    from app.models.models import ScorePoint

    tr.track("600000", "浦发银行", 72.0, "launch")
    with db.session_scope() as s:
        # 两笔已实现 + 当前持仓(成本 10, 采样价 11 -> 平仓 +10%)
        st = s.exec(select(tr.TrackedStock).where(
            tr.TrackedStock.symbol == "600000", tr.TrackedStock.status == "active"
        )).first()
        st.sim_qty = 10000
        st.sim_cost = 10.0
        st.sim_realized_pnl = 3.0
        s.add(st)
        s.add(ScorePoint(symbol="600000", time="2026-08-11 12:30:00", score=70.0,
                         stage="accelerate", price=11.0, sample_kind="noon"))
        s.commit()

    assert tr.stop("600000") is True
    hist = tr.list_history()
    assert len(hist) == 1 and hist[0]["archive_reason"] == "manual"
    assert abs(hist[0]["final_pnl"] - 13.0) < 0.01  # 3% 已实现 + 10% 平仓
    assert hist[0]["final_stage"] == "accelerate"
    # 归档后持仓清零
    with db.session_scope() as s:
        st = s.exec(select(tr.TrackedStock).where(
            tr.TrackedStock.symbol == "600000")).first()
        assert st.sim_qty == 0 and st.sim_cost == 0.0


# ---------------------------------------------------------------- 选股器评分
def test_score_indicators_uptrend(kline_df):
    from app.core.indicators import compute_all

    ind = compute_all(kline_df)
    score = score_indicators(ind)
    # 确定性上涨行情: 趋势分应显著 > 0
    assert score["trend_score"] > 10
    assert score["total"] > 0
    assert score["attention"] in ("强烈关注", "重点观察", "一般关注", "观察")


def test_score_symbol_applies_fundamental_factors(tmp_engine, monkeypatch, kline_df):
    """score_symbol 与扫描同口径: 叠加基本面/事件因子(总分与选股表格可比)."""
    import asyncio

    from app.core.config import config_manager
    from app.core.screener import engine as engine_mod

    async def fake_kline(symbol, period, count):
        return kline_df

    def fake_fund_map(symbols):
        return {}

    def fake_events(symbols, days):
        return {}

    def fake_apply(results, fund_map, event_map, q_cfg, e_cfg):
        for r in results:
            r["base_total"] = float(r.get("total", 0.0))
            r["total"] = round(r["base_total"] + 2.2, 1)
            r["factor_delta"] = 2.2
        return results, {"applied": True}

    monkeypatch.setattr(engine_mod.data_source_manager, "get_kline", fake_kline)
    monkeypatch.setattr("app.core.fundamentals.load_fundamentals_map", fake_fund_map)
    monkeypatch.setattr("app.core.fundamentals.load_recent_events", fake_events)
    monkeypatch.setattr("app.core.fundamentals.apply_fundamental_factors", fake_apply)

    cfg = config_manager.get()
    cfg["基本面因子"] = {"enabled": True, "mode": "both"}
    cfg["业绩事件"] = {"enabled": True, "lookback_days": 90}
    score = asyncio.run(engine_mod.score_symbol("600000", cfg))
    assert score is not None
    assert "base_total" in score
    assert score["factor_delta"] == 2.2
    assert abs(score["total"] - (score["base_total"] + 2.2)) < 0.05


def test_volume_score_linear_segments(kline_df):
    """量能分线性分段: 明显缩量收阴 0 / 缩量上涨给少量分 / 温和量中间分 / 放量收阳高分."""
    from app.core.indicators import compute_all

    ind = compute_all(kline_df)

    def _score(vr: float, price_up: bool = True) -> float:
        df = ind.copy()
        df["volume_ratio20"] = vr  # 覆盖最后一根量比
        if not price_up:
            df.loc[df.index[-1], "close"] = float(df["close"].iloc[-2]) - 0.1  # 最后收阴
        return score_indicators(df)["volume_score"]

    assert _score(0.4, price_up=False) == 0.0          # 明显缩量 + 收阴 -> 0 分
    assert _score(0.4) == 2.0                           # 缩量上涨(惜售) -> 仅 2 分(不再是 0)
    assert abs(_score(1.0) - 7.5) < 0.05                # 缩量~阈值线性 2.5 + 温和量收阳 5
    assert _score(1.5) == 10.0                          # 临界量比(恰等于阈值, 严格大于才算放量): 基础5+温和量5
    assert abs(_score(2.5) - 16.7) < 0.05               # 放量: 基础 6.7 + 10
    assert _score(4.5) == 20.0                          # 满量比: 满分
    assert abs(_score(4.5, price_up=False) - 10.0) < 0.05  # 放量收阴: 基础 10, 无配合分


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
    items, total = list_scan_history()
    assert total == 1 and len(items) == 1
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

