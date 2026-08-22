"""选股得分追踪(验证动量筛选可操作性).

从选股结果一键追踪 -> 每日 3 次采样(盘前 8:50 / 午间 12:30 / 盘后 16:00) ->
记录 总分/三因子/阶段/价格/量比/信号 -> 图表观察 得分与涨跌的关系.

- 评分复用引擎 score_symbol(与扫描同口径, 保证可比)
- 采样走 K 线缓存规则(三个采样点恰在复用窗口: 盘前/午休/盘后)
- 信号评估同步复用(SignalEngine.evaluate, quote 可空用收盘价, 零额外网络)
- 观察期 30 天自动归档; 衰竭期(趋势终结=观察完成)自动归档, 过热期仅预警不退出
- 归档统一结算模拟持仓 -> 写入 final_pnl/final_stage, 历史档可查看本次效果
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.core.config import config_manager
from app.core.signals.engine import PositionInfo, SignalEngine
from app.models.models import ScorePoint, TrackedStock

logger = logging.getLogger(__name__)

OBSERVE_DAYS = 30  # 观察期: 超过自动归档
SIM_AMOUNT = 100_000  # 模拟交易每笔金额(元): 按一个股票 10w 买入更真实


def _sim_lot_qty(price: float, symbol: str) -> int:
    """模拟每笔按 SIM_AMOUNT 金额买入, 按板块申报规则取整(至少一手)."""
    from app.core.lot_rules import min_buy_unit, round_buy_qty

    qty = round_buy_qty(int(SIM_AMOUNT / price) if price > 0 else 0, symbol)
    return max(qty, min_buy_unit(symbol))


def _sim_transition(qty: int, cost: float, realized: float, last_action_date: str,
                    signal_type: str, price: float, symbol: str, today: str) -> dict[str, Any]:
    """模拟交易状态机纯函数(采样与历史回放共用): 输入当前状态+信号 -> 新状态+动作+盈亏.

    当日去重: 同一天只执行第一个动作(日线信号粒度); 浮动盈亏随价格更新.
    """
    out: dict[str, Any] = {"qty": qty, "cost": cost, "realized": realized,
                           "last_action_date": last_action_date, "action": "hold", "pnl": 0.0}
    acted = last_action_date == today
    if signal_type and price > 0 and not acted:
        if signal_type == "BUY_FIRST" and qty <= 0:
            lot = _sim_lot_qty(price, symbol)
            out.update(qty=lot, cost=price, last_action_date=today, action="open")
        elif signal_type == "BUY_ADD" and qty > 0:
            lot = _sim_lot_qty(price, symbol)
            out.update(cost=(cost * qty + price * lot) / (qty + lot),  # 摊薄
                       qty=qty + lot, last_action_date=today, action="add")
        elif signal_type == "SELL_STOP" and qty > 0 and cost > 0:
            pnl_pct = (price / cost - 1) * 100
            out.update(qty=0, cost=0.0, realized=round(realized + pnl_pct, 2),
                       last_action_date=today, action="close", pnl=round(pnl_pct, 2))
        elif signal_type == "SELL_REDUCE" and qty > 0 and cost > 0:
            from app.core.lot_rules import min_buy_unit

            reduce_qty = qty // 2
            if reduce_qty >= min_buy_unit(symbol):  # 减半后至少保留一手
                pnl_pct = (price / cost - 1) * 100 * (reduce_qty / qty)
                out.update(qty=qty - reduce_qty, realized=round(realized + pnl_pct, 2),
                           last_action_date=today, action="reduce")
        # T_BUY/T_SELL: 做T不改变模拟仓位, 忽略
    if out["qty"] > 0 and out["cost"] > 0 and price > 0:
        out["pnl"] = round((price / out["cost"] - 1) * 100, 2)  # 浮动盈亏%
    return out


def _now() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _kind_by_time() -> str:
    """按当前时刻返回采样类型: pre(盘前) / noon(午间) / after(盘后) / manual."""
    t = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).time()
    if dt.time(9, 15) <= t < dt.time(11, 30) or dt.time(13, 0) <= t <= dt.time(15, 30):
        return "manual"  # 盘中手动采样按 manual 记录
    if dt.time(11, 30) <= t < dt.time(13, 0):
        return "noon"
    if t >= dt.time(15, 30):
        return "after"
    return "pre"


def track(symbol: str, name: str = "", score: float = 0.0, stage: str = "",
          stage_sub: str = "") -> dict[str, Any]:
    """从选股结果添加追踪(已存在且活跃则返回现有, 不重复).

    stage_sub: 加速期子阶段(early/mid/late), 与选股结果的加速期细分同源.
    """
    with db.session_scope() as s:
        row = s.exec(select(TrackedStock).where(
            TrackedStock.symbol == symbol, TrackedStock.status == "active"
        )).first()
        if row is not None:
            return _stock_to_dict(row)
        stock = TrackedStock(
            symbol=symbol, name=name, track_time=_now(),
            score_at_track=float(score), stage_at_track=stage,
            stage_sub_at_track=stage_sub,
        )
        s.add(stock)
        s.commit()
        s.refresh(stock)
        return _stock_to_dict(stock)


def delete_point(point_id: int) -> bool:
    """删除单条采样点(误采/异常数据清理). 返回是否删除."""
    with db.session_scope() as s:
        row = s.get(ScorePoint, point_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def _settle_and_archive(stock: TrackedStock, price: float, reason: str, stage: str = "",
                        stage_sub: str = "") -> None:
    """结算并归档(三条归档路径统一入口): 持仓按结算价模拟平仓, 写入成绩字段.

    price <= 0 时无法结算, 仅归档(final_pnl 按已实现盈亏兜底)。
    """
    if stock.sim_qty > 0 and stock.sim_cost > 0 and price > 0:
        pnl_pct = (price / stock.sim_cost - 1) * 100
        stock.sim_realized_pnl = round(stock.sim_realized_pnl + pnl_pct, 2)
        stock.sim_qty = 0
        stock.sim_cost = 0.0
    stock.final_pnl = stock.sim_realized_pnl
    stock.final_stage = stage
    stock.final_stage_sub = stage_sub
    stock.status = "archived"
    stock.archived_at = _now()
    stock.archive_reason = reason


def stop(symbol: str, reason: str = "manual") -> bool:
    """停止追踪(手动归档, 按最近采样价结算持仓)."""
    with db.session_scope() as s:
        row = s.exec(select(TrackedStock).where(
            TrackedStock.symbol == symbol, TrackedStock.status == "active"
        )).first()
        if row is None:
            return False
        latest = s.exec(select(ScorePoint).where(ScorePoint.symbol == symbol)
                        .order_by(ScorePoint.time.desc()).limit(1)).first()
        _settle_and_archive(row, float(latest.price) if latest is not None else 0.0,
                            reason,
                            stage=latest.stage if latest is not None else "",
                            stage_sub=latest.stage_sub if latest is not None else "")
        s.add(row)
        s.commit()
        return True


def _stock_to_dict(t: TrackedStock) -> dict[str, Any]:
    return {
        "symbol": t.symbol, "name": t.name, "track_time": t.track_time,
        "score_at_track": t.score_at_track, "stage_at_track": t.stage_at_track,
        "stage_sub_at_track": t.stage_sub_at_track,
        "status": t.status, "archived_at": t.archived_at, "archive_reason": t.archive_reason,
        "sim_qty": t.sim_qty, "sim_cost": t.sim_cost, "sim_open_at": t.sim_open_at,
        "sim_realized_pnl": t.sim_realized_pnl,
        "final_pnl": t.final_pnl, "final_stage": t.final_stage,
        "final_stage_sub": t.final_stage_sub,
    }


def list_active() -> list[dict[str, Any]]:
    """活跃追踪列表(附最近一次采样)."""
    with db.session_scope() as s:
        stocks = s.exec(select(TrackedStock).where(
            TrackedStock.status == "active"
        ).order_by(TrackedStock.track_time)).all()
        out: list[dict[str, Any]] = []
        for t in stocks:
            d = _stock_to_dict(t)
            latest = s.exec(select(ScorePoint).where(ScorePoint.symbol == t.symbol)
                            .order_by(ScorePoint.time.desc()).limit(1)).first()
            if latest is not None:
                d["latest"] = {
                    "time": latest.time, "score": latest.score, "price": latest.price,
                    "stage": latest.stage, "stage_sub": latest.stage_sub,
                    "trend_age": latest.trend_age, "signal_type": latest.signal_type,
                    "sample_kind": latest.sample_kind,
                    "sim_qty": latest.sim_qty, "sim_cost": latest.sim_cost,
                    "sim_pnl": latest.sim_pnl, "sim_action": latest.sim_action,
                }
            out.append(d)
        return out


def list_history() -> list[dict[str, Any]]:
    """历史档(已归档追踪)成绩单: 聚合采样点得到追踪区间价格/模拟交易对比/得分轨迹/动作统计.

    成绩口径:
    - hold_pnl    纯持有收益%(首采样价 -> 末采样价), 作为基准
    - final_pnl   模拟交易总收益%(归档结算值: 已实现 + 平仓时浮盈)
    - 动作统计    open/add/reduce/close 各次数(按天去重前的采样动作计数)
    """
    with db.session_scope() as s:
        stocks = s.exec(select(TrackedStock).where(
            TrackedStock.status == "archived"
        ).order_by(TrackedStock.archived_at.desc())).all()
        out: list[dict[str, Any]] = []
        for t in stocks:
            d = _stock_to_dict(t)
            pts = s.exec(select(ScorePoint).where(ScorePoint.symbol == t.symbol)
                         .order_by(ScorePoint.time)).all()
            if pts:
                first, last = pts[0], pts[-1]
                d["first_time"] = first.time
                d["last_time"] = last.time
                d["days"] = len({p.time[:10] for p in pts})
                d["first_price"] = first.price
                d["last_price"] = last.price
                d["hold_pnl"] = round((last.price / first.price - 1) * 100, 2) \
                    if first.price > 0 else None
                d["first_score"] = first.score
                d["last_score"] = last.score
                d["max_score"] = max(p.score for p in pts)
                actions = [p.sim_action for p in pts if p.sim_action in ("open", "add", "reduce", "close")]
                d["action_counts"] = {
                    "open": actions.count("open"), "add": actions.count("add"),
                    "reduce": actions.count("reduce"), "close": actions.count("close"),
                }
            else:
                d["days"] = 0
                d["hold_pnl"] = None
                d["action_counts"] = {"open": 0, "add": 0, "reduce": 0, "close": 0}
            out.append(d)
        return out


def points(symbol: str, limit: int = 200) -> list[dict[str, Any]]:
    """采样时间序列(升序, 供图表)."""
    with db.session_scope() as s:
        rows = s.exec(select(ScorePoint).where(ScorePoint.symbol == symbol)
                      .order_by(ScorePoint.time).limit(limit)).all()
        return [{
            "id": r.id, "time": r.time, "score": r.score, "trend_score": r.trend_score,
            "momentum_score": r.momentum_score, "volume_score": r.volume_score,
            "stage": r.stage, "stage_sub": r.stage_sub, "trend_age": r.trend_age,
            "price": r.price, "volume_ratio": r.volume_ratio,
            "signal_type": r.signal_type, "sample_kind": r.sample_kind,
            "sim_qty": r.sim_qty, "sim_cost": r.sim_cost, "sim_pnl": r.sim_pnl,
            "sim_action": r.sim_action,
        } for r in rows]


async def sample_one(symbol: str, kind: str | None = None) -> dict[str, Any] | None:
    """单票采样: 评分 + 按模拟持仓视角评估信号 + 状态机更新. 失败返回 None(不阻塞整体).

    模拟交易状态机: 空仓遇 BUY_FIRST -> 建仓; 持有遇 BUY_ADD -> 顺向加仓;
    SELL_STOP -> 全平结算; SELL_REDUCE -> 减半; T_BUY/T_SELL 不改变仓位.
    已持仓时引擎不再报 BUY_FIRST(持仓视角), 避免重复首仓信号.
    """
    cfg = config_manager.get()
    score = await _score_symbol(symbol, cfg)
    if score is None:
        logger.debug("追踪采样 %s: 评分失败/数据不足, 跳过", symbol)
        return None
    price = float(score.get("close", 0.0))

    with db.session_scope() as s:
        stock = s.exec(select(TrackedStock).where(
            TrackedStock.symbol == symbol, TrackedStock.status == "active"
        )).first()
    if stock is None:
        return None
    sim = PositionInfo(symbol=symbol, cost=stock.sim_cost, qty=stock.sim_qty)
    # 按持仓视角评估(无持仓传 None 等价空仓; 有持仓传模拟持仓, 引擎不再报 BUY_FIRST)
    signal = SignalEngine().evaluate(
        symbol, kline_df=score.get("_kline"),
        position=sim if sim.has_position else None,
    )
    # 状态机(纯函数): 含当日去重, 一天最多一个动作
    today = _now()[:10]
    st = _sim_transition(stock.sim_qty, stock.sim_cost, stock.sim_realized_pnl,
                         stock.sim_last_action_date, signal.type if signal else "",
                         price, symbol, today)
    stock.sim_qty = st["qty"]
    stock.sim_cost = round(st["cost"], 4)
    stock.sim_realized_pnl = st["realized"]
    stock.sim_last_action_date = st["last_action_date"]
    if st["action"] == "open":
        stock.sim_open_at = _now()
    sim_pnl = st["pnl"]
    sim_action = st["action"]

    with db.session_scope() as s:
        s.add(stock)
        s.commit()
        s.refresh(stock)
    # 字段名与 score_indicators 返回对齐(trend_score/momentum_score/volume_score/close)
    rec = ScorePoint(
        symbol=symbol,
        time=_now(),
        score=float(score.get("total", 0.0)),
        trend_score=float(score.get("trend_score", 0.0)),
        momentum_score=float(score.get("momentum_score", 0.0)),
        volume_score=float(score.get("volume_score", 0.0)),
        stage=str(score.get("stage", "")),
        stage_sub=str(score.get("stage_sub", "") or ""),
        trend_age=score.get("trend_age") if score.get("trend_age") is not None else None,
        price=price,
        volume_ratio=float(score.get("volume_ratio", 0.0)),
        signal_type=signal.type if signal else "",
        sample_kind=kind or _kind_by_time(),
        sim_qty=stock.sim_qty, sim_cost=round(stock.sim_cost, 4), sim_pnl=sim_pnl,
        sim_action=sim_action,
    )
    with db.session_scope() as s:
        s.add(rec)
        s.commit()
        s.refresh(rec)
        s.expunge(rec)  # 脱离 session, 避免关闭后访问 rec 触发 DetachedInstanceError
    # 衰竭期自动结束追踪(趋势终结=观察完成): 按本次采样价结算并归档
    # (过热期仅预警不退出, 留人工裁量; 开关见 config "追踪".auto_archive_on_exhaust)
    archived = False
    if str(rec.stage) == "exhaust" and cfg.get("追踪", {}).get("auto_archive_on_exhaust", True):
        with db.session_scope() as s:
            stock = s.exec(select(TrackedStock).where(
                TrackedStock.symbol == symbol, TrackedStock.status == "active"
            )).first()
            if stock is not None:
                # 本次采样价即结算价(衰竭日收盘口径), 采样点的 sim_pnl 已按该价更新
                _settle_and_archive(stock, price, "exhaust", stage=rec.stage,
                                    stage_sub=rec.stage_sub)
                s.add(stock)
                s.commit()
                archived = True
                logger.info("得分追踪 %s 进入衰竭期, 自动归档(结算收益 %.2f%%)",
                            symbol, stock.final_pnl)
    return {
        "time": rec.time, "score": rec.score, "price": rec.price,
        "stage": rec.stage, "signal_type": rec.signal_type,
        "sim_qty": rec.sim_qty, "sim_cost": rec.sim_cost, "sim_pnl": rec.sim_pnl,
        "sim_action": rec.sim_action, "archived": archived,
    }


async def _score_symbol(symbol: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """评分 + 附带 K 线(供信号评估复用, 避免二次拉取)."""
    from app.core.screener.engine import score_symbol

    score = await score_symbol(symbol, cfg)
    if score is None:
        return None
    # score_symbol 内部已拉 K 线, 但未透出; 信号评估需 K 线, 这里补一次(命中缓存, 无网络)
    from app.core.datasource import data_source_manager

    df = await data_source_manager.get_kline(symbol, "daily", 120)
    score["_kline"] = df
    return score


async def sample_all(kind: str | None = None) -> dict[str, Any]:
    """对全部活跃追踪采样(定时任务/手动调用). 单票失败跳过."""
    stocks = list_active()
    if not stocks:
        return {"total": 0, "ok": 0, "failed": 0}
    ok = failed = 0
    for st in stocks:
        try:
            if await sample_one(st["symbol"], kind) is not None:
                ok += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("追踪采样 %s 异常: %s", st["symbol"], exc)
            failed += 1
    logger.info("得分追踪采样完成: %d/%d 成功", ok, len(stocks))
    return {"total": len(stocks), "ok": ok, "failed": failed}


def archive_expired() -> int:
    """观察期(30 天)到期的活跃追踪自动归档(按最近采样价结算持仓)."""
    limit = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
             - dt.timedelta(days=OBSERVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with db.session_scope() as s:
        rows = s.exec(select(TrackedStock).where(
            TrackedStock.status == "active", TrackedStock.track_time < limit
        )).all()
        for t in rows:
            latest = s.exec(select(ScorePoint).where(ScorePoint.symbol == t.symbol)
                            .order_by(ScorePoint.time.desc()).limit(1)).first()
            _settle_and_archive(t, float(latest.price) if latest is not None else 0.0,
                                "expired",
                                stage=latest.stage if latest is not None else "",
                                stage_sub=latest.stage_sub if latest is not None else "")
            s.add(t)
            n += 1
        if n:
            s.commit()
    if n:
        logger.info("得分追踪自动归档: %d 只到期", n)
    return n


__all__ = [
    "track", "stop", "list_active", "list_history", "points", "sample_one", "sample_all",
    "archive_expired", "delete_point", "OBSERVE_DAYS",
]
