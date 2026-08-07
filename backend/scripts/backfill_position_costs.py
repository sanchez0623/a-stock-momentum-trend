"""一次性维护脚本: 把持仓成本从「纯均价」重算为「含费摊薄成本」(券商 APP 口径).

背景: Position.cost 早期只按成交价加权, 未摊入买入手续费, 导致浮盈偏乐观、
止损/止盈线也比真实保本位偏低。本脚本按 trade 表流水回放重算:
    buy  -> 含费市值 += 成交额 + 手续费; 纯市值 += 成交额
    sell -> 按当时每股成本等比扣减两个市值
并填充 cost_raw(纯成交均价, 仅用于顺向加仓判断)。

前置: trade 表的 fee 必须已回填(先跑 scripts/backfill_fees.py)。

用法(在 backend 目录下执行):
    .venv/Scripts/python.exe scripts/backfill_position_costs.py

脚本会先备份数据库到 data/trading.db.bak-<时间戳>, 仅处理 cost_raw 为 0 的持仓,
可安全重复运行。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402


def _snapshot() -> list[tuple]:
    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(position)"))}
        if "cost_raw" not in cols:
            return []
        return list(conn.execute(text(
            "SELECT symbol, name, qty, cost, cost_raw FROM position "
            "WHERE status = 'holding' ORDER BY symbol"
        )))


def main() -> int:
    print(f"数据库: {db.DB_PATH}")
    if not db.DB_PATH.exists():
        print("数据库不存在, 无需处理")
        return 0

    backup = db.DB_PATH.with_suffix(f".db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db.DB_PATH, backup)
    print(f"已备份: {backup.name}\n")

    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(position)"))}
    print(f"迁移前 position 列是否含 cost_raw: {'cost_raw' in cols}")

    before = {r[0]: r for r in _snapshot()}

    # 建表(幂等) + 补列 + 自动回放
    db.init_db()
    # 兜底再跑一次(列本就存在但 cost_raw=0 的情况, 如列已补但回放未执行)
    n = db.backfill_position_costs()
    print(f"本次重算持仓数: {n}\n")

    after = _snapshot()
    if not after:
        print("无持仓")
        return 0

    print("持仓成本(重算后):")
    print(f"  {'代码':<8}{'名称':<10}{'股数':>7}  {'纯均价':>10}  {'含费成本':>10}  "
          f"{'摊入费用':>10}  {'原成本':>10}")
    total_fee = 0.0
    for symbol, name, qty, cost, cost_raw in after:
        fee_cost = (cost - cost_raw) * qty
        total_fee += fee_cost
        old = before.get(symbol)
        old_cost = f"{old[3]:.4f}" if old else "-"
        print(f"  {symbol:<8}{name or '':<10}{qty:>7}  {cost_raw:>10.4f}  {cost:>10.4f}  "
              f"{fee_cost:>10,.2f}  {old_cost:>10}")
    print(f"\n摊入持仓的买入手续费合计: ¥{total_fee:,.2f}")
    print("提示: 浮盈将相应减少同等金额, 止损/止盈线以含费成本为基准(更贴近真实保本位)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
