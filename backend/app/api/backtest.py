"""回测中心 API(方案C: 阶段分桶因子回测 + 持仓回测 + 变体对比回测)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.backtest.audit import run_signal_audit
from app.core.backtest.data import backtest_data
from app.core.backtest.factor import backtest_factors
from app.core.backtest.portfolio import (
    MANAGE_HOLD,
    MANAGE_SIGNAL,
    MANAGE_STOP,
    Leg,
    PortfolioBacktest,
    run_portfolio_backtest,
)

router = APIRouter(prefix="/api", tags=["backtest"])


class BacktestDataWarmupBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 自选 + 持仓
    start: str = ""                    # 空 = end 前推 3 年(含指标预热回退)
    end: str = ""                      # 空 = 今天
    force: bool = False                # 显式重拉(常规路径不需要)


@router.post("/backtest/data/warmup")
async def warmup_backtest_data(body: BacktestDataWarmupBody) -> dict:
    """回测数据预热: 把目标股票池区间数据拉成前复权冻结快照(baostock).

    冻结语义: 已拉取日期不再覆盖; 同区间重复调用命中快照不重复拉取.
    """
    try:
        symbols = body.symbols or _default_backtest_pool()
        if not symbols:
            return {"code": 0, "msg": "ok(空股票池, 无需预热)", "data": {"meta": {"symbols": 0}, "results": {}}}
        report = await asyncio.to_thread(
            backtest_data.warmup,
            symbols=symbols,
            start=body.start,
            end=body.end,
            force=bool(body.force),
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"回测数据预热失败: {exc}", "data": None}


@router.get("/backtest/data/status")
async def backtest_data_status() -> dict:
    """回测冻结快照状态(诊断用)."""
    try:
        return {"code": 0, "msg": "ok", "data": backtest_data.status()}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"查询失败: {exc}", "data": None}


def _default_backtest_pool() -> list[str]:
    """默认预热池: 自选 + 持仓(与策略回测默认池口径一致)."""
    from sqlmodel import select

    from app import db
    from app.models.models import Position, Watchlist

    with db.session_scope() as s:
        wl = [r.symbol for r in s.exec(select(Watchlist)).all()]
        pos = [r.symbol for r in s.exec(select(Position).where(Position.status == "holding")).all()]
    return list(dict.fromkeys(wl + pos))


class PortfolioLegIn(BaseModel):
    symbol: str
    name: str = ""
    entry_date: str = ""   # YYYY-MM-DD(空=回测区间起点入场)
    cost: float = 0.0      # 含费成本(与实盘 Position.cost 口径一致)
    qty: int = 0
    pyramid_stage: int = 0


class PresetBody(BaseModel):
    name: str = ""
    legs: list[PortfolioLegIn] = []


class AuditBody(BaseModel):
    symbols: list[str] | None = None   # 空 = 全部有成交的票
    start: str = ""
    end: str = ""


@router.post("/backtest/audit")
async def run_audit(body: AuditBody) -> dict:
    """信号审计(方案 v2 §6): 真实成交 vs 纪律曲线, 逐笔标注 违背/滞后/提前."""
    try:
        report = await asyncio.to_thread(
            run_signal_audit, symbols=body.symbols, start=body.start, end=body.end,
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"信号审计失败: {exc}", "data": None}


@router.get("/backtest/presets")
async def list_presets() -> dict:
    """建仓腿模板列表(快捷复用)."""
    try:
        from sqlmodel import select

        from app import db
        from app.models.models import BacktestPreset

        with db.session_scope() as s:
            rows = s.exec(select(BacktestPreset).order_by(BacktestPreset.created_at.desc())).all()
        return {"code": 0, "msg": "ok", "data": [{
            "id": r.id, "name": r.name, "created_at": r.created_at,
            "legs": json.loads(r.legs_json or "[]"),
        } for r in rows]}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"查询失败: {exc}", "data": None}


@router.post("/backtest/presets")
async def save_preset(body: PresetBody) -> dict:
    """保存建仓腿模板(命名组合, 一键复用)."""
    try:
        from app import db
        from app.models.models import BacktestPreset

        legs = [leg for leg in body.legs if leg.symbol and leg.qty > 0 and leg.cost > 0]
        if not body.name.strip() or not legs:
            return {"code": 1, "msg": "模板名与有效建仓腿均不能为空", "data": None}
        with db.session_scope() as s:
            row = BacktestPreset(name=body.name.strip(),
                                 legs_json=json.dumps([leg.model_dump() for leg in legs], ensure_ascii=False))
            s.add(row)
            s.commit()
            s.refresh(row)
        return {"code": 0, "msg": "ok", "data": {"id": row.id, "name": row.name}}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"保存失败: {exc}", "data": None}


@router.delete("/backtest/presets/{preset_id}")
async def delete_preset(preset_id: int) -> dict:
    """删除建仓腿模板."""
    try:
        from app import db
        from app.models.models import BacktestPreset

        with db.session_scope() as s:
            row = s.get(BacktestPreset, preset_id)
            if row is None:
                return {"code": 1, "msg": "模板不存在", "data": None}
            s.delete(row)
            s.commit()
        return {"code": 0, "msg": "ok", "data": {"id": preset_id}}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"删除失败: {exc}", "data": None}


class PortfolioBody(BaseModel):
    mode: str = "real"                 # real=当前持仓 / import=显式腿 / trades=从成交导入
    legs: list[PortfolioLegIn] = []
    manage: str = MANAGE_SIGNAL        # hold / stop / signal
    symbols: list[str] | None = None   # trades 模式: 只导入这些股票的成交(空=全部)
    start: str = ""
    end: str = ""
    initial_capital: float = 0.0       # 0=自动(覆盖全部期初投入)
    intraday_minutes: int = 10         # 盘中路径模拟粒度(5/10/15/30, 默认 10)


@router.post("/backtest/portfolio")
async def run_portfolio(body: PortfolioBody) -> dict:
    """持仓回测(方案 v2 §5): 真实持仓/导入建仓腿 -> 躺平/纪律/系统 三线对照 + 差异归因."""
    try:
        legs = _build_legs(body)
        if not legs:
            return {"code": 0, "msg": "无有效建仓腿(持仓为空或参数不完整)", "data": None}
        report = await asyncio.to_thread(
            run_portfolio_backtest,
            legs=legs, manage=body.manage,
            initial_capital=float(body.initial_capital),
            start=body.start, end=body.end,
            intraday_minutes=int(body.intraday_minutes),
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"持仓回测失败: {exc}", "data": None}


@router.get("/backtest/portfolio/preview")
async def portfolio_preview() -> dict:
    """持仓回测候选: 当前持仓(模式 A) + 成交导入候选(模式 B)."""
    try:
        pos_legs = PortfolioBacktest.load_position_legs()
        trade_legs = PortfolioBacktest.legs_from_trades()
        return {"code": 0, "msg": "ok", "data": {
            "positions": [
                {"symbol": leg.symbol, "name": leg.name, "entry_date": leg.entry_date[:10],
                 "cost": round(leg.cost, 3), "qty": leg.qty, "pyramid_stage": leg.pyramid_stage}
                for leg in pos_legs
            ],
            "trades": [
                {"symbol": leg.symbol, "name": leg.name, "entry_date": leg.entry_date[:10],
                 "cost": round(leg.cost, 3), "qty": leg.qty}
                for leg in trade_legs
            ],
            "manage_options": [
                {"key": MANAGE_HOLD, "label": "躺平(买入持有)"},
                {"key": MANAGE_STOP, "label": "纪律(仅止损)"},
                {"key": MANAGE_SIGNAL, "label": "系统(信号全开)"},
            ],
        }}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"查询失败: {exc}", "data": None}


def _build_legs(body: PortfolioBody) -> list[Leg]:
    """按模式构建建仓腿列表."""
    if body.mode == "real":
        return PortfolioBacktest.load_position_legs()
    if body.mode == "trades":
        return PortfolioBacktest.legs_from_trades(symbols=body.symbols)
    return [
        Leg(symbol=leg.symbol, name=leg.name, entry_date=leg.entry_date, cost=leg.cost,
            qty=leg.qty, pyramid_stage=leg.pyramid_stage)
        for leg in body.legs
    ]


class BacktestFactorBody(BaseModel):
    symbols: list[str] | None = None   # 为空 = 缓存全市场
    hold_days: list[int] = [5, 10, 20]
    min_bars: int = 60
    cost: bool = True                  # 是否扣除双边手续费


@router.post("/backtest/factor")
async def run_factor(body: BacktestFactorBody) -> dict:
    """阶段分桶因子回测(同步; 全市场缓存约 60s, 走线程池避免阻塞事件循环)."""
    try:
        hold = tuple(sorted({int(h) for h in body.hold_days}))
        report = await asyncio.to_thread(
            backtest_factors,
            symbols=body.symbols,
            hold_days=hold,
            min_bars=max(30, int(body.min_bars)),
            cost=bool(body.cost),
        )
        return {"code": 0, "msg": "ok", "data": report}
    except Exception as exc:  # noqa: BLE001
        return {"code": 1, "msg": f"回测失败: {exc}", "data": None}


# ---------------------------------------------------------------- 异步任务基建(对比回测共用)
# 内存任务表(进程内有效, 重启丢失; MVP 够用). 结构: {task_id: {status, progress, result, error}}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


@router.get("/backtest/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    """查询异步回测任务进度/结果(对比回测等)."""
    with _tasks_lock:
        t = _tasks.get(task_id)
    if t is None:
        return {"code": 1, "msg": "任务不存在(进程重启后任务丢失)", "data": None}
    return {"code": 0, "msg": "ok", "data": dict(t)}


# ---------------------------------------------------------------- 变体对比回测(消融实验)
class CompareVariantIn(BaseModel):
    label: str = ""                    # 空 = 自动命名
    cooldown_days: int = 10            # 止损冷却(交易日, 0=关, 上限 30)
    defense: str = "soft"              # soft=软防守 / hard=硬防守(旧口径) / off=关


class StrategyCompareBody(BaseModel):
    pool_size: int = 60                # 随机抽样股票数(0=筛选后全部, >0 抽样, 上限 500)
    seed: int = 42                     # 抽样种子(固定可复现)
    board: str = ""                    # 板块过滤(与选股中心同源): main/chinext/star/bj, 逗号分隔多值
    industry: str = ""                 # 行业过滤(申万/东财行业名), 逗号分隔多值
    universe: str = "all"              # 选股池: all/hs300/zz500/sz50/hs300+zz500/zz800
    start: str = ""                    # 回测区间起(YYYY-MM-DD, 空=不限; 指标仍用全量数据预热)
    end: str = ""                      # 回测区间止(含当日, 空=不限)
    initial_capital: float = 1_000_000.0
    variants: list[CompareVariantIn] = []   # 空 = 默认三变体(裸奔/仅冷却/防守+冷却)


@router.post("/backtest/strategy-compare")
async def run_strategy_compare(body: StrategyCompareBody) -> dict:
    """变体对比回测(异步): 同池同种子跑多变体, 量化各风控开关的贡献. 返回 task_id."""
    from app.core.backtest.compare import build_pool, default_variants, run_compare

    pool_size = 0 if int(body.pool_size) <= 0 else max(10, min(int(body.pool_size), 500))
    variants_in = body.variants[:6] or [
        CompareVariantIn(**v) for v in default_variants()
    ]
    variants = [{
        "label": (v.label.strip() or f"冷却{v.cooldown_days}日·{v.defense}防守"),
        "cooldown_days": max(0, min(int(v.cooldown_days), 30)),
        "defense": v.defense if v.defense in ("soft", "hard", "off") else "soft",
    } for v in variants_in]

    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        # 单任务守卫: 同一进程内只允许一个回测在跑(多线程抢 GIL 会互相拖慢到"假死"级).
        # 僵死任务(>10 分钟无心跳)自动标记 error 放行, 不阻塞新任务.
        now = time.time()
        for tid, t in _tasks.items():
            if t.get("status") != "running":
                continue
            if now - t.get("last_active", now) > 600:
                t["status"] = "error"
                t["error"] = "任务超时无活动(>10分钟), 已自动放弃"
                continue
            return {"code": 1, "msg": f"已有回测任务在运行中({t.get('progress', 0)}%), 请等待完成后再启动", "data": None}
        _tasks[task_id] = {"status": "running", "progress": 0, "result": None,
                           "error": "", "last_active": time.time()}

    def _run() -> None:
        try:
            symbols, pool_note = build_pool(
                pool_size, int(body.seed),
                board=body.board.strip(), industry=body.industry.strip(),
                universe=body.universe.strip() or "all",
            )
            if not symbols:
                raise RuntimeError("本地无可用日线数据, 请先盘后预热")

            def cb(pct: float) -> None:
                with _tasks_lock:
                    _tasks[task_id]["progress"] = round(pct)
                    _tasks[task_id]["last_active"] = time.time()

            result = run_compare(
                variants=variants,
                symbols=symbols,
                initial_capital=max(10_000.0, float(body.initial_capital)),
                progress_cb=cb,
                start=body.start.strip()[:10],
                end=body.end.strip()[:10],
            )
            result["pool"] = {"size": pool_size, "seed": int(body.seed),
                              "note": pool_note, **result["pool"]}
            with _tasks_lock:
                _tasks[task_id]["result"] = result
                _tasks[task_id]["status"] = "done"
                _tasks[task_id]["progress"] = 100
                _tasks[task_id]["last_active"] = time.time()
        except Exception as exc:  # noqa: BLE001
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = str(exc)
                _tasks[task_id]["last_active"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return {"code": 0, "msg": "ok", "data": {"task_id": task_id, "variants": len(variants)}}
