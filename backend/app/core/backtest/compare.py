"""变体对比回测(消融实验): 同池同种子跑多变体, 量化各风控开关的贡献.

方法(与 2026-08-22 修复验证脚本同源):
- 股票池: 全市场随机抽样(固定 seed 可复现), 各变体共用同一池 -> 差异只来自开关本身
- 变体轴(均为配置/参数级, 不改引擎代码): 止损冷却天数(0=关) x 回撤防守模式(soft/hard/off)
- 顺序跑各变体, 进度聚合为 (已完成变体 + 当前变体进度) / 总变体数

用法(API): POST /backtest/strategy-compare { pool_size, seed, variants[] }
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any, Callable

from app.core.backtest.strategy import StrategyBacktest
from app.core.datasource import kline_store

# 预设变体(前端同源展示; key 供前端勾选, 后端按 label/cooldown/defense 执行)
PRESETS: list[dict[str, Any]] = [
    {"key": "raw", "label": "裸奔基线(无冷却·无防守)", "cooldown_days": 0, "defense": "off"},
    {"key": "cool", "label": "仅冷却10日", "cooldown_days": 10, "defense": "off"},
    {"key": "soft", "label": "仅软防守", "cooldown_days": 0, "defense": "soft"},
    {"key": "full", "label": "软防守+冷却10日(当前默认)", "cooldown_days": 10, "defense": "soft"},
]

# 请求未指定变体时的默认组合(讲"冷却闸门值多少钱"的故事)
DEFAULT_KEYS = ("raw", "cool", "full")

ACTION_ORDER = ("buy_first", "buy_add", "sell_reduce", "sell_stop", "t_buy", "t_sell")


def default_variants() -> list[dict[str, Any]]:
    """默认变体组合(裸奔 / 仅冷却 / 防守+冷却)."""
    by_key = {p["key"]: p for p in PRESETS}
    return [dict(by_key[k]) for k in DEFAULT_KEYS if k in by_key]


def build_pool(n: int, seed: int, board: str = "", industry: str = "",
               universe: str = "all") -> tuple[list[str], str]:
    """按筛选条件构建回测池(与选股中心同源过滤), 再随机抽 n 只.

    过滤链: Stock 本地缓存(候选) -> 指数成分股(universe, 同步读缓存) ->
    板块(board) -> 行业(industry, 申万映射优先) -> 与本地日线缓存取交集(回测必须有数据)
    -> 随机抽样 n 只(固定 seed 可复现; n<=0 或 >= 池大小时取全部).

    返回 (symbols, note): note 描述池来源与每步筛选结果, 供前端展示.
    """
    from sqlmodel import select

    from app import db
    from app.models.models import Stock

    # 1. 候选池: Stock 本地缓存(与选股中心 _resolve_symbols 同源), 回退 kline 缓存
    with db.session_scope() as s:
        rows = s.exec(select(Stock.symbol, Stock.name, Stock.industry).order_by(Stock.symbol)).all()
    if rows:
        pool: list[tuple[str, str, str]] = [(r[0], r[1], r[2] or "") for r in rows]
        source = f"全A缓存 {len(pool)} 只"
    else:
        pool = [(sym, "", "") for sym in kline_store.list_symbols(period="daily")]
        source = f"kline缓存 {len(pool)} 只"
    notes: list[str] = []

    # 2. 指数成分股预筛(同步读 IndexConstituent 缓存, 不在线刷新; 空缓存降级为不过滤)
    from app.core.universe import load_universe_symbols, parse_universe, universe_label

    keys = parse_universe(universe)
    if keys:
        allowed, _ = load_universe_symbols(keys)
        if allowed:
            before = len(pool)
            pool = [it for it in pool if it[0] in allowed]
            notes.append(f"{universe_label(universe)}: {before}→{len(pool)}")
        else:
            notes.append(f"{universe_label(universe)}成分股缓存不可用,已忽略")

    # 3. 板块过滤(前缀匹配, 与选股中心 BOARD_PREFIXES 同源)
    if board:
        from app.core.screener.engine import StockScreener

        boards = StockScreener._split_multi(board)
        pool = [it for it in pool if any(StockScreener._match_board(it[0], b) for b in boards)]
        notes.append(f"板块[{board}]: {len(pool)}")

    # 4. 行业过滤(申万映射优先, 回退东财行业包含匹配)
    if industry:
        from app.core.classification import load_classification_map
        from app.core.screener.engine import StockScreener

        kws = StockScreener._split_multi(industry)
        class_map = load_classification_map([it[0] for it in pool])
        pool = StockScreener._filter_by_industry(pool, kws, class_map)
        notes.append(f"行业[{industry}]: {len(pool)}")

    # 5. 与本地日线缓存取交集(无数据的票回测也会被跳过, 提前过滤保证池子有效)
    available = set(kline_store.list_symbols(period="daily"))
    before = len(pool)
    pool = [it for it in pool if it[0] in available]
    if len(pool) != before:
        notes.append(f"有日线数据: {before}→{len(pool)}")
    if not pool:
        raise RuntimeError("筛选条件下无可用日线数据(范围过窄或未盘后预热)")

    # 6. 随机抽样(固定 seed 可复现)
    syms = sorted(it[0] for it in pool)
    total = len(syms)
    if 0 < n < total:
        syms = sorted(random.Random(seed).sample(syms, n))
        notes.append(f"随机抽取 {len(syms)} 只(种子{seed}, 池{total}只)")
    else:
        notes.append(f"取全部 {total} 只")
    return syms, f"{source} | " + "; ".join(notes)


def sample_pool(n: int, seed: int) -> list[str]:
    """全市场随机抽样(固定 seed 可复现, 与修复验证脚本同法). build_pool 的薄壳(无筛选)."""
    syms, _ = build_pool(n, seed)
    return syms


def summarize(report: dict[str, Any], label: str, cooldown_days: int,
              defense: str) -> dict[str, Any]:
    """从单变体完整报告提取对比摘要(丢弃逐笔交易, 保留动作分解与净值曲线)."""
    m, s = report["meta"], report["stats"]
    by = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in report["trades"]:
        by[t["action"]]["n"] += 1
        by[t["action"]]["pnl"] += t["pnl"]
    return {
        "label": label,
        "cooldown_days": cooldown_days,
        "defense": defense,
        "date_from": report["equity_curve"][0]["date"] if report["equity_curve"] else "",
        "date_to": report["equity_curve"][-1]["date"] if report["equity_curve"] else "",
        "total_return_pct": m["total_return_pct"],
        "annual_return_pct": m["annual_return_pct"],
        "max_drawdown_pct": m["max_drawdown_pct"],
        "sharpe": m["sharpe"],
        "days": m["days"],
        "win_rate": s["win_rate"],
        "profit_factor": s["profit_factor"],
        "expectancy": s["expectancy"],
        "trades": s["trades"],
        "cooldown_blocks": s["cooldown_blocks"],
        "final_defense": bool(s["defense_mode"]),
        "by_action": {a: {"n": by[a]["n"], "pnl": round(by[a]["pnl"], 2)} for a in ACTION_ORDER},
        "equity_curve": report["equity_curve"],
    }


def run_compare(
    variants: list[dict[str, Any]],
    symbols: list[str],
    initial_capital: float = 1_000_000.0,
    progress_cb: Callable[[float], None] | None = None,
    start: str = "",
    end: str = "",
    cancel: "threading.Event | None" = None,
) -> dict[str, Any]:
    """顺序跑各变体(同池同种子), 返回 {pool, variants[]}.

    progress_cb 收到 0~100 的总体进度. 单变体失败不中断整体, 记为 error 条目.
    start/end: 回测区间(YYYY-MM-DD 含端点, 空=全部), 各变体共用同一窗口.
    cancel: 取消事件, 置位后在变体边界/日循环粒度停止, 已完成变体的结果保留.
    """
    import threading  # noqa: F401  (类型注解用)

    results: list[dict[str, Any]] = []
    n = len(variants)
    for i, v in enumerate(variants):
        if cancel is not None and cancel.is_set():
            results.append({"label": str(v.get("label") or f"变体{i + 1}"),
                            "cooldown_days": v.get("cooldown_days", 10),
                            "defense": v.get("defense", "soft"), "error": "已取消"})
            continue
        label = str(v.get("label") or f"变体{i + 1}")
        cooldown = int(v.get("cooldown_days", 10))
        defense = v.get("defense", "soft")
        if progress_cb:  # 变体启动即报心跳(预计算阶段无逐日回调)
            progress_cb(i / n * 100)

        def _cb(done: int, total: int, _i: int = i) -> None:
            if progress_cb and n:
                frac = (done / total) if total else 0.0
                progress_cb((_i + frac) / n * 100)

        try:
            bt = StrategyBacktest(initial_capital=initial_capital, defense=defense)
            bt.stop_cooldown_days = cooldown
            r = bt.run(symbols=symbols, progress_cb=_cb, start=start, end=end, cancel=cancel)
            if "error" in r:
                results.append({"label": label, "cooldown_days": cooldown,
                                "defense": defense, "error": r["error"]})
            else:
                results.append(summarize(r, label, cooldown, defense))
        except Exception as exc:  # noqa: BLE001
            results.append({"label": label, "cooldown_days": cooldown,
                            "defense": defense, "error": str(exc)})
        if progress_cb:
            progress_cb((i + 1) / n * 100)
    return {"pool": {"symbols": len(symbols)}, "variants": results}
