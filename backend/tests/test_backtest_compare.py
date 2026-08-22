"""变体对比回测(消融实验)单测: 选池过滤 + 抽样可复现 + 防守模式消融 + 编排与 API."""

from __future__ import annotations

import time

import numpy as np

from app.core.backtest.compare import build_pool, default_variants, run_compare, sample_pool
from app.core.backtest.strategy import StrategyBacktest


def _kline_rows(close_list: list[float]) -> list[dict]:
    import pandas as pd

    close = np.array(close_list, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    volume = np.full(len(close), 5_000_000.0)
    amount = volume * close
    dates = pd.bdate_range("2025-01-02", periods=len(close))
    return [
        {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": a}
        for d, o, h, lo, c, v, a in zip(dates.strftime("%Y-%m-%d"), open_, high, low, close, volume, amount, strict=True)
    ]


def _fake_store(monkeypatch, n_syms: int = 8) -> list[str]:
    """构造 n_syms 只'启动后上涨/暴涨后暴跌'行情(会出首仓与止损)."""
    from app.core.datasource import kline_store

    store: dict[str, list[dict]] = {}
    syms = [f"T{i:04d}" for i in range(n_syms)]
    for sym in syms:
        if int(sym[1:]) % 2 == 0:
            flat = [100.0 + np.sin(i / 3) * 3 + np.sin(i / 11) * 1.5 for i in range(100)]
            rows = _kline_rows(flat + [101, 102.2, 103.6, 105.2] + [round(105.2 + i * 0.5, 2) for i in range(1, 35)])
        else:
            slow = [100.0 + i * 0.2 + np.sin(i / 6) * 1.5 for i in range(120)]
            spike = [round(130 + i * 2.5, 2) for i in range(5)]
            crash = [round(spike[-1] - i * 1.5, 2) for i in range(1, 41)]
            rows = _kline_rows(slow + spike + crash)
        store[sym] = rows
    monkeypatch.setattr(kline_store, "load", lambda symbol, period="daily": store.get(symbol))
    monkeypatch.setattr(kline_store, "list_symbols", lambda period="daily": list(store.keys()))
    return syms


# ---------------------------------------------------------------- 抽样可复现
def test_sample_pool_deterministic(tmp_engine, monkeypatch):
    """同 seed 同数量 => 同池; 不同 seed => 不同池(可复现是消融的前提).

    tmp_engine: 清空 Stock 候选缓存, 走 kline 缓存回退分支(与 monkeypatch 的 fake 行情一致).
    """
    _fake_store(monkeypatch, n_syms=20)
    a = sample_pool(8, seed=42)
    b = sample_pool(8, seed=42)
    c = sample_pool(8, seed=7)
    assert a == b
    assert a != c
    assert len(a) == 8


# ---------------------------------------------------------------- 选池过滤(与选股中心同源)
def _fake_board_store(monkeypatch) -> None:
    """混合板块行情缓存: 主板 600xxx / 创业板 300xxx / 科创板 688xxx."""
    from app.core.datasource import kline_store

    store = {}
    for sym in ("600001", "600002", "300001", "688001"):
        flat = [100.0 + np.sin(i / 3) * 3 + np.sin(i / 11) * 1.5 for i in range(100)]
        store[sym] = _kline_rows(flat + [101, 102.2, 103.6, 105.2] + [round(105.2 + i * 0.5, 2) for i in range(1, 35)])
    monkeypatch.setattr(kline_store, "load", lambda symbol, period="daily": store.get(symbol))
    monkeypatch.setattr(kline_store, "list_symbols", lambda period="daily": list(store.keys()))


def test_build_pool_board_filter(tmp_engine, monkeypatch):
    """板块过滤: 只留创业板前缀; 空池时报错."""
    _fake_board_store(monkeypatch)
    syms, note = build_pool(0, seed=42, board="chinext")
    assert syms == ["300001"]
    assert "板块" in note
    # 多值板块
    syms2, _ = build_pool(0, seed=42, board="main,star")
    assert syms2 == ["600001", "600002", "688001"]
    # 过滤到空 -> RuntimeError
    try:
        build_pool(0, seed=42, board="bj")
        assert False, "北交所无票应报错"
    except RuntimeError as e:
        assert "无可用日线数据" in str(e)


def test_build_pool_universe_filter(tmp_engine, monkeypatch):
    """指数成分股预筛: 同步读缓存, 命中则缩池, 缓存不可用时降级为全量(不报错)."""
    from app import db
    from app.models.models import IndexConstituent

    _fake_board_store(monkeypatch)
    with db.session_scope() as s:
        s.add(IndexConstituent(index_key="hs300", symbol="600001", name="测试", updated_at="2026-08-22 10:00:00"))
        s.commit()
    syms, note = build_pool(0, seed=42, universe="hs300")
    assert syms == ["600001"]
    assert "沪深300" in note
    # 缓存中不存在的指数 -> 空集合 -> 降级全量
    syms2, note2 = build_pool(0, seed=42, universe="zz500")
    assert len(syms2) == 4
    assert "不可用" in note2


def test_build_pool_zero_or_oversize_takes_all(tmp_engine, monkeypatch):
    """n=0 或 n>=池大小: 取全部(所有股票随机选 x 个的 0=全部语义)."""
    _fake_board_store(monkeypatch)
    syms0, _ = build_pool(0, seed=42)
    syms_big, _ = build_pool(999, seed=42)
    assert syms0 == syms_big == ["300001", "600001", "600002", "688001"]


def test_default_variants():
    """默认三变体: 裸奔 / 仅冷却 / 防守+冷却(讲冷却闸门贡献的故事)."""
    vs = default_variants()
    assert len(vs) == 3
    assert vs[0]["cooldown_days"] == 0 and vs[0]["defense"] == "off"
    assert vs[1]["cooldown_days"] == 10 and vs[1]["defense"] == "off"
    assert vs[2]["cooldown_days"] == 10 and vs[2]["defense"] == "soft"


# ---------------------------------------------------------------- 防守模式消融
def test_defense_off_never_triggers():
    """off: 回撤再深也不触发防守."""
    bt = StrategyBacktest(initial_capital=1_000_000, defense="off")
    bt._update_defense(500_000)  # 回撤 50%
    assert bt.defense_mode is False


def test_defense_hard_never_recovers():
    """hard: 触发后即使净值创新高也不解除(旧实盘口径: 永久禁开仓)."""
    bt = StrategyBacktest(initial_capital=1_000_000, defense="hard")
    bt._update_defense(890_000)  # 回撤 11%: 触发
    assert bt.defense_mode is True
    bt._update_defense(1_200_000)  # 净值远超前高: 仍不解除
    assert bt.defense_mode is True


def test_defense_soft_recovers():
    """soft: 修复至阈值一半以下解除(与旧软防守行为一致, 回归保护)."""
    bt = StrategyBacktest(initial_capital=1_000_000, defense="soft")
    bt._update_defense(890_000)
    assert bt.defense_mode is True
    bt._update_defense(960_000)  # 回撤 4% < 5%: 解除
    assert bt.defense_mode is False


# ---------------------------------------------------------------- 编排
def test_run_compare_structure_and_progress(monkeypatch):
    """同池跑两变体: 摘要结构完整 / 进度单调到 100 / 冷却变体首仓不多于裸奔."""
    syms = _fake_store(monkeypatch)
    variants = [
        {"label": "裸奔", "cooldown_days": 0, "defense": "off"},
        {"label": "冷却10日", "cooldown_days": 10, "defense": "off"},
    ]
    progress: list[float] = []
    r = run_compare(variants, symbols=syms, progress_cb=progress.append)
    assert "pool" in r and len(r["variants"]) == 2
    assert progress and progress[-1] == 100  # 最终进度 100
    assert all(p >= 0 for p in progress)
    for v in r["variants"]:
        assert v["label"] and v["total_return_pct"] is not None
        assert "equity_curve" in v and v["equity_curve"]
        assert set(v["by_action"]) >= {"buy_first", "sell_stop", "sell_reduce"}
    n_raw = r["variants"][0]["by_action"]["buy_first"]["n"]
    n_cool = r["variants"][1]["by_action"]["buy_first"]["n"]
    assert n_cool <= n_raw  # 冷却只可能减少再入场


def test_run_compare_single_variant_error_isolated(monkeypatch):
    """单变体异常(非法防守值回退 soft 不炸)不中断整体."""
    syms = _fake_store(monkeypatch, n_syms=4)
    variants = [
        {"label": "正常", "cooldown_days": 0, "defense": "off"},
    ]
    r = run_compare(variants, symbols=syms)
    assert len(r["variants"]) == 1
    assert "error" not in r["variants"][0]


# ---------------------------------------------------------------- 时间范围
def test_run_compare_date_range(tmp_engine, monkeypatch):
    """时间窗口: 净值曲线与交易都落在窗口内; 窗口首日即可出信号(指标全量预热); 空窗口报错."""
    syms = _fake_store(monkeypatch, n_syms=6)
    r = run_compare(
        [{"label": "窗口", "cooldown_days": 10, "defense": "soft"}],
        symbols=syms, start="2025-03-01", end="2025-04-30",
    )
    v = r["variants"][0]
    assert "error" not in v, v.get("error")
    assert v["date_from"] >= "2025-03-01"
    assert v["date_to"] <= "2025-04-30"
    assert all(p["date"] >= "2025-03-01" and p["date"] <= "2025-04-30" for p in v["equity_curve"])

    # 窗口外(未来日期) -> error 条目, 不抛异常
    r2 = run_compare(
        [{"label": "空窗", "cooldown_days": 0, "defense": "off"}],
        symbols=syms, start="2030-01-01", end="2030-12-31",
    )
    assert "error" in r2["variants"][0]


def test_strategy_backtest_date_range(tmp_engine, monkeypatch):
    """StrategyBacktest.run(start/end): 裁剪交易循环, 指标用全量数据(窗口首日有信号能力)."""
    bt = StrategyBacktest(initial_capital=1_000_000)
    r_full = bt.run(symbols=_fake_store(monkeypatch, n_syms=4))
    assert "error" not in r_full
    bt2 = StrategyBacktest(initial_capital=1_000_000)
    r_win = bt2.run(symbols=_fake_store(monkeypatch, n_syms=4), start="2025-03-03", end="2025-03-31")
    assert "error" not in r_win
    dates = [p["date"] for p in r_win["equity_curve"]]
    assert dates and dates[0] >= "2025-03-03" and dates[-1] <= "2025-03-31"
    assert len(dates) < len(r_full["equity_curve"])


# ---------------------------------------------------------------- K线数据统计 API
def test_kline_stats_api(tmp_engine):
    """GET /api/kline/stats: ok/stale/missing 聚合 + 日期范围."""
    import json as _json

    from fastapi.testclient import TestClient

    from app import db
    from app.models.models import KlineCache

    # 造两只缓存: 一只新鲜达标, 一只陈旧
    fresh_bars = [{"date": f"2026-08-{d:02d}", "close": 10.0} for d in range(1, 21)]
    stale_bars = [{"date": f"2025-01-{d:02d}", "close": 10.0} for d in range(1, 21)]
    with db.session_scope() as s:
        s.add(KlineCache(symbol="600001", period="daily", ohlcv_json=_json.dumps(fresh_bars)))
        s.add(KlineCache(symbol="600002", period="daily", ohlcv_json=_json.dumps(stale_bars)))
        s.commit()

    from app.main import app

    c = TestClient(app)
    r = c.get("/api/kline-cache/stats")
    d = r.json()
    assert d["code"] == 0, d
    data = d["data"]
    assert data["cached"] == 2
    assert data["stale"] >= 1  # 600002 数据陈旧
    assert data["date_to"] == "2026-08-20"
    assert data["date_from"] == "2025-01-01"
    assert data["days_behind"] is not None


# ---------------------------------------------------------------- API 端点(单任务守卫)
def test_compare_api_single_task_guard(tmp_engine, monkeypatch):
    """单任务守卫: 已有 running 任务时拒绝新任务; 僵死任务(>10min 无心跳)自动放行."""
    from fastapi.testclient import TestClient

    from app.api.backtest import _tasks, _tasks_lock
    from app.main import app

    _fake_store(monkeypatch, n_syms=6)
    c = TestClient(app)
    tid: str | None = None
    try:
        # 1. 注入一个"活着"的运行中任务 -> 新任务被拒
        with _tasks_lock:
            _tasks["fakealive"] = {"status": "running", "progress": 45, "result": None,
                                   "error": "", "last_active": time.time()}
        r = c.post("/api/backtest/strategy-compare", json={"pool_size": 10, "seed": 1})
        d = r.json()
        assert d["code"] == 1 and "已有回测任务" in d["msg"]

        # 2. 任务心跳超 10 分钟(僵死) -> 放行新任务, 旧任务标记 error
        with _tasks_lock:
            _tasks["fakealive"]["last_active"] = time.time() - 700
        r2 = c.post("/api/backtest/strategy-compare", json={"pool_size": 10, "seed": 1})
        d2 = r2.json()
        assert d2["code"] == 0, d2
        with _tasks_lock:
            assert _tasks["fakealive"]["status"] == "error"
        # 清理: 轮询至新任务完成, 避免污染其他测试
        tid = d2["data"]["task_id"]
        for _ in range(120):
            t = c.get(f"/api/backtest/tasks/{tid}").json()["data"]
            if t["status"] in ("done", "error"):
                break
            time.sleep(0.5)
    finally:
        with _tasks_lock:
            _tasks.pop("fakealive", None)
            if tid:
                _tasks.pop(tid, None)



def test_compare_api(tmp_engine, monkeypatch):
    """API 层: POST /api/backtest/strategy-compare -> task_id -> 轮询至 done."""
    from fastapi.testclient import TestClient

    from app.main import app

    _fake_store(monkeypatch, n_syms=6)
    c = TestClient(app)
    r = c.post("/api/backtest/strategy-compare", json={
        "pool_size": 10,  # 池只有 6 只 -> min(10, 6)
        "seed": 1,
        "variants": [
            {"label": "裸奔", "cooldown_days": 0, "defense": "off"},
            {"label": "默认形态", "cooldown_days": 10, "defense": "soft"},
        ],
    })
    d = r.json()
    assert d["code"] == 0, d
    task_id = d["data"]["task_id"]
    assert d["data"]["variants"] == 2

    # 轮询直到终态(后台线程 + 小池子, 秒级完成)
    for _ in range(120):
        t = c.get(f"/api/backtest/tasks/{task_id}").json()["data"]
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert t["status"] == "done", t.get("error")
    rep = t["result"]
    assert rep["pool"]["symbols"] == 6 and rep["pool"]["seed"] == 1
    assert len(rep["variants"]) == 2
    assert rep["variants"][0]["label"] == "裸奔"
    assert rep["variants"][0]["equity_curve"]
