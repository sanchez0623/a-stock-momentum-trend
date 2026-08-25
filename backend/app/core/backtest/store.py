"""对比回测结果落库: 运行摘要 + 逐笔交易明细持久化, 供前端回看历史与复盘.

内存任务表(_tasks)重启即清; 本模块把每次对比回测的变体摘要(指标/净值曲线/动作分解)
与逐笔明细写入 SQLite, 支持: 历史运行列表 / 详情调出 / 按变体或股票筛选明细 / 删除.
保留策略: 仅保留最近 KEEP_RUNS 次运行(含明细), 旧的自动清理, 防止无限膨胀.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.models.models import BacktestRun, BacktestTrade

logger = logging.getLogger(__name__)

# 历史运行保留条数(超出自动清理, 含关联明细)
KEEP_RUNS = 20


def save_compare_run(params: dict[str, Any], result: dict[str, Any],
                     symbols: list[str]) -> int | None:
    """把一次对比回测结果落库, 返回 run_id(失败返回 None, 不影响任务结果本身).

    params: 触发参数(pool_size/seed/universe/board/industry/start/end/initial_capital).
    result: run_compare 的输出 {pool, variants[]}, 变体含 trade_details 逐笔明细.
    """
    variants = result.get("variants", [])
    summaries: list[dict[str, Any]] = []
    trades: list[BacktestTrade] = []
    try:
        with db.session_scope() as s:
            run = BacktestRun(
                mode="compare",
                pool_size=int(params.get("pool_size", 0) or 0),
                seed=int(params.get("seed", 0) or 0),
                universe=str(params.get("universe", "") or ""),
                board=str(params.get("board", "") or ""),
                industry=str(params.get("industry", "") or ""),
                start=str(params.get("start", "") or ""),
                end=str(params.get("end", "") or ""),
                initial_capital=float(params.get("initial_capital", 1_000_000.0) or 0.0),
                symbols_json=json.dumps(symbols, ensure_ascii=False),
            )
            for v in variants:
                summary = {k: w for k, w in v.items() if k != "trade_details"}
                summaries.append(summary)
                label = str(v.get("label", ""))
                for t in v.get("trade_details", []):
                    trades.append(BacktestTrade(
                        run_id=0,  # 占位, run.id 就绪后回填
                        variant=label,
                        date=str(t.get("date", "")),
                        symbol=str(t.get("symbol", "")),
                        name=str(t.get("name", "")),
                        action=str(t.get("action", "")),
                        price=round(float(t.get("price", 0.0) or 0.0), 4),
                        qty=int(t.get("qty", 0) or 0),
                        fee=round(float(t.get("fee", 0.0) or 0.0), 2),
                        pnl=round(float(t.get("pnl", 0.0) or 0.0), 2),
                        reason=str(t.get("reason", "")),
                    ))
            run.variants_json = json.dumps(summaries, ensure_ascii=False)
            s.add(run)
            s.commit()
            for t in trades:
                t.run_id = run.id  # type: ignore[attr-defined]
            if trades:
                s.add_all(trades)
                s.commit()
            prune_runs(s)
            return run.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("对比回测结果落库失败(不影响任务结果): %s", exc)
        return None


def prune_runs(session: Any, keep: int = KEEP_RUNS) -> int:
    """只保留最近 keep 次运行(含明细), 返回清理的运行数."""
    rows = list(session.exec(select(BacktestRun.id).order_by(BacktestRun.id.desc())))
    ids = [r for r in rows[keep:]]
    if not ids:
        return 0
    for t in session.exec(select(BacktestTrade).where(BacktestTrade.run_id.in_(ids))):
        session.delete(t)
    for rid in ids:
        row = session.get(BacktestRun, rid)
        if row is not None:
            session.delete(row)
    session.commit()
    logger.info("对比回测历史清理: 删除 %d 次旧运行", len(ids))
    return len(ids)


def list_runs(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """历史运行列表(倒序, 不含明细): 每条含变体标签与收益, 供列表页展示."""
    with db.session_scope() as s:
        rows = list(s.exec(
            select(BacktestRun).order_by(BacktestRun.id.desc()).offset(offset).limit(limit)
        ))
        items: list[dict[str, Any]] = []
        for r in rows:
            try:
                variants = json.loads(r.variants_json or "[]")
            except (ValueError, TypeError):
                variants = []
            items.append({
                "id": r.id,
                "time": r.time,
                "mode": r.mode,
                "pool_size": r.pool_size,
                "seed": r.seed,
                "universe": r.universe,
                "board": r.board,
                "industry": r.industry,
                "start": r.start,
                "end": r.end,
                "initial_capital": r.initial_capital,
                "symbols": len(json.loads(r.symbols_json or "[]")) if r.symbols_json else 0,
                "variants": [
                    {
                        "label": v.get("label", ""),
                        "total_return_pct": v.get("total_return_pct"),
                        "trades": v.get("trades"),
                        "error": v.get("error"),
                    }
                    for v in variants
                ],
            })
        return items


def get_run(run_id: int) -> dict[str, Any] | None:
    """单次运行详情: 触发参数 + 变体摘要(指标/净值曲线/动作分解), 可直接渲染对比页."""
    with db.session_scope() as s:
        r = s.get(BacktestRun, run_id)
        if r is None:
            return None
        try:
            variants = json.loads(r.variants_json or "[]")
        except (ValueError, TypeError):
            variants = []
        try:
            symbols = json.loads(r.symbols_json or "[]")
        except (ValueError, TypeError):
            symbols = []
        return {
            "id": r.id,
            "time": r.time,
            "mode": r.mode,
            "pool_size": r.pool_size,
            "seed": r.seed,
            "universe": r.universe,
            "board": r.board,
            "industry": r.industry,
            "start": r.start,
            "end": r.end,
            "initial_capital": r.initial_capital,
            "symbols": len(symbols),
            "report": {"pool": {"size": r.pool_size, "seed": r.seed,
                                "symbols": len(symbols)}, "variants": variants},
        }


def get_run_trades(run_id: int, variant: str = "", symbol: str = "",
                   limit: int = 3000) -> list[dict[str, Any]]:
    """单次运行的逐笔明细(可按变体/股票过滤, 日期升序)."""
    with db.session_scope() as s:
        q = select(BacktestTrade).where(BacktestTrade.run_id == run_id)
        if variant:
            q = q.where(BacktestTrade.variant == variant)
        if symbol:
            q = q.where(BacktestTrade.symbol == symbol)
        rows = list(s.exec(q.order_by(BacktestTrade.date, BacktestTrade.id).limit(limit)))
        return [
            {"variant": t.variant, "date": t.date, "symbol": t.symbol, "name": t.name,
             "action": t.action, "price": t.price, "qty": t.qty, "fee": t.fee,
             "pnl": t.pnl, "reason": t.reason}
            for t in rows
        ]


def delete_run(run_id: int) -> bool:
    """删除一次运行及其全部明细."""
    with db.session_scope() as s:
        r = s.get(BacktestRun, run_id)
        if r is None:
            return False
        for t in s.exec(select(BacktestTrade).where(BacktestTrade.run_id == run_id)):
            s.delete(t)
        s.delete(r)
        s.commit()
        return True
