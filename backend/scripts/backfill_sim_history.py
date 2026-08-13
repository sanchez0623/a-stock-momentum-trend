# -*- coding: utf-8 -*-
"""一次性: 回填历史采样点的模拟交易轨迹(状态机回放, 与未来采样同口径).

对每只追踪票: 按时间升序回放 ScorePoint 的信号/价格 -> 重建 sim_qty/sim_cost/sim_pnl/sim_action,
并同步 TrackedStock 当前模拟状态(股数改为 10 万元/笔口径).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import select  # noqa: E402

from app import db  # noqa: E402
from app.core.tracking.score_tracker import _sim_transition  # noqa: E402
from app.models.models import ScorePoint, TrackedStock  # noqa: E402

with db.session_scope() as s:
    # 注意: SQLModel 单列查询返回标量 str, 不是 Row 元组
    symbols = sorted({str(r) for r in s.exec(select(ScorePoint.symbol).distinct()).all()})

total_points = 0
for symbol in sorted(symbols):
    with db.session_scope() as s:
        pts = s.exec(select(ScorePoint).where(ScorePoint.symbol == symbol)
                     .order_by(ScorePoint.time)).all()
        stock = s.exec(select(TrackedStock).where(TrackedStock.symbol == symbol)).first()
        qty, cost, realized, last_date = 0, 0.0, 0.0, ""
        for p in pts:
            st = _sim_transition(qty, cost, realized, last_date,
                                 p.signal_type, p.price, symbol, p.time[:10])
            p.sim_qty = st["qty"]
            p.sim_cost = round(st["cost"], 4)
            p.sim_pnl = st["pnl"]
            p.sim_action = st["action"]
            qty, cost, realized, last_date = st["qty"], st["cost"], st["realized"], st["last_action_date"]
            total_points += 1
            s.add(p)
        if stock is not None:
            stock.sim_qty = qty
            stock.sim_cost = round(cost, 4)
            stock.sim_realized_pnl = realized
            stock.sim_last_action_date = last_date
            s.add(stock)
        s.commit()
    print(f"{symbol}: {len(pts)} 个采样点回放完成, 当前模拟 qty={qty} cost={cost:.2f} realized={realized:.2f}%")

print(f"回填完成: {len(symbols)} 只票, {total_points} 个采样点")
