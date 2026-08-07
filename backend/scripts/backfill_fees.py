"""一次性维护脚本: 补齐 trade.fee 列并回填历史成交手续费, 同步重建 trades.csv.

背景: app/db.py 早期迁移表名误写为 "trades"(复数), 实际表名为 "trade"(单数),
导致 ALTER TABLE 被静默跳过, 旧库始终缺 fee 列, /api/trades 直接 500。

用法(在 backend 目录下执行):
    .venv/Scripts/python.exe scripts/backfill_fees.py

脚本会先备份数据库到 data/trading.db.bak-<时间戳>, 再执行迁移与回填, 可安全重复运行。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.core.config import config_manager  # noqa: E402
from app.core.fees import compute_trade_fee  # noqa: E402


def main() -> int:
    print(f"数据库: {db.DB_PATH}")
    if not db.DB_PATH.exists():
        print("数据库不存在, 无需处理")
        return 0

    backup = db.DB_PATH.with_suffix(f".db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db.DB_PATH, backup)
    print(f"已备份: {backup.name}")

    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(trade)"))}
    print(f"迁移前 trade 列是否含 fee: {'fee' in cols}")

    # 建表(幂等) + 补列 + 自动回填
    db.init_db()

    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(trade)"))}
    print(f"迁移后 trade 列是否含 fee: {'fee' in cols}")

    # 兜底再跑一次回填(列本就存在但历史行 fee=0 的情况)
    n = db.backfill_trade_fees()
    print(f"本次回填行数: {n}")

    fee_cfg = config_manager.get().get("手续费")
    print("\n当前费率配置:")
    for k, v in (fee_cfg or {}).items():
        print(f"  {k}: {v}")

    print("\n成交明细(回填后):")
    total_fee = 0.0
    with db.engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT id, time, symbol, name, action, amount, fee, pnl FROM trade ORDER BY id"
        )))
    for tid, t, sym, name, action, amount, fee, pnl in rows:
        fee = float(fee or 0.0)
        total_fee += fee
        expect = compute_trade_fee(action or "buy", float(amount or 0), fee_cfg)
        flag = "" if abs(expect - fee) < 0.01 else f"  <-- 期望 {expect}"
        print(f"  #{tid} {t} {sym} {name or '':6} {action:4} 金额={amount:>12,.2f} "
              f"手续费={fee:>8,.2f} pnl={pnl}{flag}")
    print(f"\n累计手续费: ¥{total_fee:,.2f}")

    # 重建 CSV(旧表头缺 fee 列)
    from app.core.logger import trade_logger

    trade_logger._rebuild_csv_from_db()
    print(f"已重建 CSV: {trade_logger.csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
