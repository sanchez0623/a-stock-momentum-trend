"""选股 API(方案 §6.3) + 自选股 + 分类映射."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app import db
from app.api.deps import get_session
from app.core.screener import scan_tasks, screener
from app.models.models import Watchlist

router = APIRouter(prefix="/api", tags=["screener"])


class WatchlistBody(BaseModel):
    symbol: str
    name: str = ""


@router.post("/screener/run")
async def run_screener(
    market: str = Query("all", pattern="^(all|sh|sz|bj)$"),
    board: str | None = Query(None, description="板块, 逗号分隔可多值: main主板/chinext创业板/star科创板/bj北交所(如 main,chinext)"),
    industry: str | None = Query(None, description="行业名(包含匹配), 逗号分隔可多值, 需本地行业数据(如 半导体,电力设备)"),
    top_n: int = Query(30, ge=5, le=200),
    per_industry: int = Query(0, ge=0, le=50, description="每行业限配 N 只(0=用配置/不限)"),
    industry_level: str = Query("sw_l1", pattern="^(sw_l1|sw_l2|sw_l3)$", description="分组用申万级别"),
    apply_gate: bool = Query(True, description="是否应用大盘择时闸门"),
    universe: str | None = Query(None, description="选股池预筛: all/hs300/zz500/sz50/hs300+zz500/zz800(空=用配置)"),
    apply_factors: bool = Query(True, description="是否叠加基本面质量 + 业绩事件因子"),
) -> dict:
    """触发扫描(异步, 返回 task_id). 支持 market + 板块多值 + 行业多值 + 每行业限配 + 闸门 + 选股池预筛 + 因子."""
    task_id = scan_tasks.create(market, top_n)
    scan_tasks.update(task_id, status="running")
    params = {
        "market": market, "board": board, "industry": industry, "top_n": top_n,
        "per_industry": per_industry, "industry_level": industry_level,
        "apply_gate": apply_gate, "universe": universe, "apply_factors": apply_factors,
    }

    async def _run() -> None:
        try:
            result = await screener.scan(
                market=market,
                board=board,
                industry=industry,
                top_n=top_n,
                per_industry=per_industry,
                industry_level=industry_level,
                apply_gate=apply_gate,
                universe=universe,
                apply_factors=apply_factors,
                progress_cb=lambda done, total: scan_tasks.progress(task_id, done, total),
            )
            scan_tasks.update(task_id, status="done", progress=100, result=result)
            # 扫描完成 -> 结果持久化到历史表(前端可回看, 内存任务重启即清)
            from app.core.screener.history import save_scan_history

            save_scan_history(scan_tasks.get(task_id) or {}, params)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "扫描已启动", "data": {"task_id": task_id}}


# ---------------------------------------------------------------- 分类映射
@router.post("/screener/classification/refresh")
async def refresh_classification() -> dict:
    """刷新全量分类映射(申万 L1/L2/L3 + 行业/概念板块). 异步, 返回 task_id."""
    from app.core import classification as clf_mod

    task_id = scan_tasks.create("classification", 0)
    scan_tasks.update(task_id, status="running")

    async def _run() -> None:
        try:
            stats = await clf_mod.refresh_classification(
                progress_cb=lambda msg, p: scan_tasks.progress(task_id, int(p * 100), 100)
            )
            scan_tasks.update(task_id, status="done", progress=100, result=stats)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "分类映射刷新已启动", "data": {"task_id": task_id}}


@router.get("/screener/classification/stats")
async def classification_stats() -> dict:
    """分类映射覆盖统计."""
    from sqlmodel import func

    from app.models.models import StockClassification

    with db.session_scope() as s:
        total = s.exec(select(func.count()).select_from(StockClassification)).one()
        l1 = s.exec(select(func.count()).select_from(StockClassification).where(StockClassification.sw_l1 != "")).one()
        l2 = s.exec(select(func.count()).select_from(StockClassification).where(StockClassification.sw_l2 != "")).one()
        l3 = s.exec(select(func.count()).select_from(StockClassification).where(StockClassification.sw_l3 != "")).one()
        bi = s.exec(select(func.count()).select_from(StockClassification).where(StockClassification.boards_industry != "[]")).one()
        bc = s.exec(select(func.count()).select_from(StockClassification).where(StockClassification.boards_concept != "[]")).one()
    return {"code": 0, "msg": "ok", "data": {
        "total": total, "sw_l1": l1, "sw_l2": l2, "sw_l3": l3,
        "board_industry": bi, "board_concept": bc,
    }}


@router.get("/screener/last-scan-summary")
async def last_scan_summary() -> dict:
    """最近一次扫描的择时闸门 / 行业限配汇总(无需翻日志即可确认两个特性是否生效且效果).

    重启服务后该汇总会丢失(内存态), 此时返回 scanned_at=None 的占位.
    """
    summary = screener.last_scan_summary
    if summary is None:
        return {"code": 0, "msg": "ok", "data": {
            "scanned_at": None,
            "note": "尚未执行过扫描(或在服务重启后丢失)",
        }}
    return {"code": 0, "msg": "ok", "data": summary}


# ---------------------------------------------------------------- 选股池(universe)
@router.post("/screener/universe/refresh")
async def refresh_universe_api(
    universe: str = Query("all", description="要刷新的选股池: all(全三指数)/hs300/zz500/sz50/hs300+zz500/zz800"),
) -> dict:
    """刷新指数成分股缓存(数据源 baostock). 异步, 返回 task_id."""
    from app.core import universe as uni_mod

    index_keys = uni_mod.parse_universe(universe) or list(uni_mod.INDEX_LABELS)
    task_id = scan_tasks.create("universe", 0)
    scan_tasks.update(task_id, status="running")

    async def _run() -> None:
        try:
            stats = await uni_mod.refresh_universe(
                index_keys,
                progress_cb=lambda msg, p: scan_tasks.progress(task_id, int(p * 100), 100),
            )
            scan_tasks.update(task_id, status="done", progress=100, result=stats)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "选股池刷新已启动", "data": {"task_id": task_id}}


@router.get("/screener/universe/stats")
async def universe_stats_api() -> dict:
    """各指数成分股缓存概况."""
    from app.core import universe as uni_mod

    return {"code": 0, "msg": "ok", "data": uni_mod.universe_stats()}


@router.get("/screener/industries")
async def screener_industries() -> dict:
    """可选行业列表(供选股下拉): 合并 东财行业(Stock.industry) 与 申万一级(sw_l1), 按股票数降序."""
    from sqlmodel import func

    from app.models.models import Stock, StockClassification

    counts: dict[str, int] = {}
    with db.session_scope() as s:
        for row in s.exec(
            select(Stock.industry, func.count()).where(Stock.industry != "").group_by(Stock.industry)
        ).all():
            name = (row[0] or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + int(row[1])
        for row in s.exec(
            select(StockClassification.sw_l1, func.count())
            .where(StockClassification.sw_l1 != "")
            .group_by(StockClassification.sw_l1)
        ).all():
            name = (row[0] or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + int(row[1])
    items = [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}


@router.get("/screener/industries/tree")
async def screener_industries_tree() -> dict:
    """申万三级行业树(一级->二级->三级, 每级带股票数), 供选股页树形多选."""
    from app.core.classification import industry_tree

    items = industry_tree()
    return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}


# ---------------------------------------------------------------- 基本面 + 业绩事件
async def _resolve_refresh_symbols(universe: str) -> list[str]:
    """刷新任务用的 symbol 解析: 指定 universe 取其成分股, 否则用本地全部股票列表."""
    from sqlmodel import select

    from app.models.models import Stock

    if universe and universe.strip().lower() != "all":
        from app.core import universe as uni_mod

        syms, _note = await uni_mod.ensure_universe(universe.strip(), max_age_days=7)
        if syms:
            return sorted(syms)
    with db.session_scope() as s:
        return sorted(r[0] for r in s.exec(select(Stock.symbol)).all())


@router.post("/screener/fundamentals/refresh")
async def refresh_fundamentals_api(
    universe: str = Query("all", description="刷新范围: all(全部股票)/hs300/zz500/sz50/hs300+zz500/zz800"),
    full: bool = Query(False, description="是否拉取更全的季度报告期(更慢)"),
) -> dict:
    """批量刷新基本面质量数据并落库(数据源 baostock, 扫描时只读表). 异步, 返回 task_id."""
    from app.core import fundamentals as fund_mod

    task_id = scan_tasks.create("fundamentals", 0)
    scan_tasks.update(task_id, status="running")

    async def _run() -> None:
        try:
            symbols = await _resolve_refresh_symbols(universe)
            stats = await fund_mod.refresh_fundamentals(
                symbols, full=full,
                progress_cb=lambda msg, p: scan_tasks.progress(task_id, int(p * 100), 100),
            )
            scan_tasks.update(task_id, status="done", progress=100, result=stats)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "基本面刷新已启动", "data": {"task_id": task_id, "scope": universe}}


@router.post("/screener/earnings/refresh")
async def refresh_earnings_api(
    universe: str = Query("all", description="刷新范围: all(全部股票)/hs300/zz500/sz50/hs300+zz500/zz800"),
    days: int = Query(90, ge=7, le=365, description="回看天数"),
) -> dict:
    """批量刷新近 N 天业绩预告/快报并落库(数据源 baostock). 异步, 返回 task_id."""
    from app.core import fundamentals as fund_mod

    task_id = scan_tasks.create("earnings", 0)
    scan_tasks.update(task_id, status="running")

    async def _run() -> None:
        try:
            symbols = await _resolve_refresh_symbols(universe)
            stats = await fund_mod.refresh_earnings_events(
                symbols, days=days,
                progress_cb=lambda msg, p: scan_tasks.progress(task_id, int(p * 100), 100),
            )
            scan_tasks.update(task_id, status="done", progress=100, result=stats)
        except Exception as exc:  # noqa: BLE001
            scan_tasks.update(task_id, status="failed", error=str(exc))

    asyncio.get_running_loop().create_task(_run())
    return {"code": 0, "msg": "业绩事件刷新已启动", "data": {"task_id": task_id, "scope": universe, "days": days}}


@router.get("/screener/fundamentals/stats")
async def fundamentals_stats_api() -> dict:
    """基本面 + 业绩事件表覆盖概况."""
    from app.core import fundamentals as fund_mod

    return {"code": 0, "msg": "ok", "data": fund_mod.fundamentals_stats()}


@router.get("/screener/result")
async def screener_result(task_id: str) -> dict:
    task = scan_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 0, "msg": "ok", "data": task}


@router.get("/screener/result/latest")
async def screener_latest() -> dict:
    task = scan_tasks.latest()
    if task is None:
        return {"code": 0, "msg": "ok", "data": None}
    return {"code": 0, "msg": "ok", "data": task}


# ---------------------------------------------------------------- 扫描历史(持久化回看)
@router.get("/screener/history")
async def screener_history_list(limit: int = Query(50, ge=1, le=200)) -> dict:
    """选股扫描历史列表(不含结果 JSON, 点击某条再取详情)."""
    from app.core.screener.history import list_scan_history

    return {"code": 0, "msg": "ok", "data": {"items": list_scan_history(limit)}}


@router.get("/screener/history/{history_id}")
async def screener_history_detail(history_id: int) -> dict:
    """历史详情(含完整结果列表, 供前端回看)."""
    from app.core.screener.history import get_scan_history

    item = get_scan_history(history_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"code": 0, "msg": "ok", "data": item}


@router.delete("/screener/history/{history_id}")
async def screener_history_delete(history_id: int) -> dict:
    """删除单条扫描历史."""
    from app.core.screener.history import delete_scan_history

    if not delete_scan_history(history_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"code": 0, "msg": "ok", "data": {"id": history_id}}


# ---------------------------------------------------------------- 自选股
@router.get("/watchlist")
async def watchlist(session: Session = Depends(get_session)) -> dict:
    stmt = select(Watchlist).order_by(Watchlist.added_at.desc())
    rows = session.exec(stmt).all()
    return {"code": 0, "msg": "ok", "data": [r.model_dump() for r in rows]}


@router.post("/watchlist")
async def add_watch(body: WatchlistBody, session: Session = Depends(get_session)) -> dict:
    row = session.get(Watchlist, body.symbol)
    if row is None:
        row = Watchlist(symbol=body.symbol, name=body.name)
        session.add(row)
    else:
        row.name = body.name or row.name
    session.commit()
    return {"code": 0, "msg": "ok", "data": {"symbol": body.symbol}}


@router.delete("/watchlist/{symbol}")
async def remove_watch(symbol: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(Watchlist, symbol)
    if row:
        session.delete(row)
        session.commit()
    return {"code": 0, "msg": "ok", "data": {"symbol": symbol}}
