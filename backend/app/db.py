"""SQLite 数据库引擎与会话."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

# 数据目录: env DATA_DIR > 默认 ./data
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trading.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """SQLite 连接级优化: WAL(读写互不阻塞) + busy_timeout(锁冲突等待而非立即报错).

    WAL 为库级持久设置, 此处保证每个新连接都带上 busy_timeout/synchronous;
    盘后预热写缓存与 API 并发读写场景下可避免 'database is locked' 间歇失败.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def init_db() -> None:
    """建表(幂等) + 兼容旧库补列."""
    from app.models import models  # noqa: F401  # 确保模型注册到 metadata

    SQLModel.metadata.create_all(engine)
    _migrate_columns()
    logger.info("SQLite 就绪: %s", DB_PATH)


def _migrate_columns() -> None:
    """为已存在的表补加新增列(ALTER TABLE), 避免旧库报错.

    仅补充已知的可空/有默认值列, 不影响已有数据.
    注意: 表名必须与 SQLModel 实际表名一致(默认是类名小写单数, 如 Trade -> trade),
    写错表名会被静默跳过, 导致旧库长期缺列。
    """
    alters = [
        ("trade", "fee", "REAL DEFAULT 0.0"),
        ("position", "cost_raw", "REAL DEFAULT 0.0"),
        ("position", "opened_at", "TEXT DEFAULT ''"),
        ("position", "pyramid_stage", "INTEGER DEFAULT 0"),
        ("klinecache", "updated_at", "TEXT DEFAULT ''"),
    ]
    added: list[tuple[str, str]] = []
    try:
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )}
            for table, column, col_type in alters:
                # 表不存在说明 create_all 已建好(含新列), 跳过; 仅给旧库补列
                if table not in tables:
                    continue
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                    conn.commit()
                    added.append((table, column))
                    logger.info("迁移: %s 增加列 %s", table, column)
    except Exception as exc:  # noqa: BLE001
        logger.warning("迁移补列失败(可忽略, 新建库不受影响): %s", exc)
        return
    # 新补的 fee 列, 对历史成交回填手续费(一次性)
    if ("trade", "fee") in added:
        backfill_trade_fees()
    # cost_raw 补列后, 按成交流水回放重算持仓的含费成本(一次性)
    if ("position", "cost_raw") in added:
        backfill_position_costs()
    # pyramid_stage 补列后, 按成交流水估算已加仓档位(一次性, 旧库兼容)
    if ("position", "pyramid_stage") in added:
        backfill_position_stages()


def backfill_trade_fees() -> int:
    """为缺手续费的历史成交按当前费率回填 fee; 卖出记录同步把 pnl 改为净额.

    仅处理 amount > 0 且 fee 为空/0 的行(正常成交因最低佣金 5 元, fee 不可能为 0),
    因此可安全重复执行。返回回填的行数。
    """
    from app.core.config import config_manager
    from app.core.fees import compute_trade_fee

    fee_cfg = config_manager.get().get("手续费")
    updated = 0
    try:
        with engine.connect() as conn:
            rows = list(conn.execute(text(
                "SELECT id, action, amount, pnl FROM trade "
                "WHERE amount > 0 AND (fee IS NULL OR fee = 0)"
            )))
            for tid, action, amount, pnl in rows:
                fee = compute_trade_fee(action or "buy", float(amount or 0.0), fee_cfg)
                if fee <= 0:
                    continue
                # 卖出且有已实现盈亏: 历史值是毛盈亏, 需扣费成为净额
                if action == "sell" and pnl is not None:
                    conn.execute(
                        text("UPDATE trade SET fee = :fee, pnl = :pnl WHERE id = :id"),
                        {"fee": fee, "pnl": round(float(pnl) - fee, 2), "id": tid},
                    )
                else:
                    conn.execute(
                        text("UPDATE trade SET fee = :fee WHERE id = :id"),
                        {"fee": fee, "id": tid},
                    )
                updated += 1
            conn.commit()
        if updated:
            logger.info("迁移: 历史成交回填手续费 %d 行", updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回填历史手续费失败: %s", exc)
    return updated


def backfill_position_costs() -> int:
    """按成交流水回放, 把持仓成本重算为**含费摊薄成本**, 并填充 cost_raw(纯均价).

    回放规则(加权平均法):
      buy  -> 含费市值 += amount + fee; 纯市值 += amount; 股数 += qty
      sell -> 按当时每股成本等比扣减两个市值, 股数 -= qty
    仅处理 cost_raw 为空/0 的持仓行, 可安全重复执行。
    若回放股数与持仓表不符(流水缺失), 则保守跳过含费重算, 仅把 cost_raw 置为原 cost。
    返回处理的持仓数。
    """
    updated = 0
    try:
        with engine.connect() as conn:
            rows = list(conn.execute(text(
                "SELECT id, symbol, qty, cost FROM position "
                "WHERE status = 'holding' AND qty > 0 AND (cost_raw IS NULL OR cost_raw = 0)"
            )))
            for pid, symbol, pos_qty, pos_cost in rows:
                trades = list(conn.execute(
                    text("SELECT action, qty, amount, fee FROM trade "
                         "WHERE symbol = :s ORDER BY time ASC, id ASC"),
                    {"s": symbol},
                ))
                qty = 0
                total_incl = 0.0   # 含费累计成本
                total_raw = 0.0    # 纯成交额累计
                for action, t_qty, amount, fee in trades:
                    t_qty = int(t_qty or 0)
                    amount = float(amount or 0.0)
                    fee = float(fee or 0.0)
                    if action == "buy":
                        qty += t_qty
                        total_incl += amount + fee
                        total_raw += amount
                    elif qty > 0:
                        sold = min(t_qty, qty)
                        total_incl -= (total_incl / qty) * sold
                        total_raw -= (total_raw / qty) * sold
                        qty -= sold
                if qty == int(pos_qty) and qty > 0:
                    new_cost = round(total_incl / qty, 4)
                    new_raw = round(total_raw / qty, 4)
                else:
                    # 流水与持仓对不上: 不臆造含费成本, 只补 cost_raw 保持现状
                    logger.warning("持仓 %s 流水回放股数 %d != 持仓 %s, 跳过含费重算",
                                   symbol, qty, pos_qty)
                    new_cost = float(pos_cost or 0.0)
                    new_raw = float(pos_cost or 0.0)
                conn.execute(
                    text("UPDATE position SET cost = :c, cost_raw = :r WHERE id = :id"),
                    {"c": new_cost, "r": new_raw, "id": pid},
                )
                updated += 1
            conn.commit()
        if updated:
            logger.info("迁移: 持仓成本重算为含费口径 %d 条", updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回填持仓含费成本失败: %s", exc)
    return updated


def backfill_position_stages() -> int:
    """按成交流水估算已加仓档位, 回填 pyramid_stage(旧库兼容).

    规则: 买入成交笔数 - 1 = 已加仓档位数(首仓不算加仓).
    与旧 pyramid_plan 的 used_stage = len(buys)-1 保持一致, 避免存量数据回退。
    仅处理 status='holding' 且 qty>0 的持仓行, 可安全重复执行。
    """
    updated = 0
    try:
        with engine.connect() as conn:
            rows = list(conn.execute(text(
                "SELECT id, symbol FROM position "
                "WHERE status = 'holding' AND qty > 0"
            )))
            for pid, symbol in rows:
                buy_rows = list(conn.execute(
                    text("SELECT COUNT(*) FROM trade WHERE symbol = :s AND action = 'buy'"),
                    {"s": symbol},
                ))
                buy_count = int(buy_rows[0][0]) if buy_rows else 0
                stage = max(0, buy_count - 1)
                conn.execute(
                    text("UPDATE position SET pyramid_stage = :st WHERE id = :id"),
                    {"st": stage, "id": pid},
                )
                updated += 1
            conn.commit()
        if updated:
            logger.info("迁移: 回填 pyramid_stage %d 条", updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回填 pyramid_stage 失败: %s", exc)
    return updated


@contextmanager
def session_scope() -> Iterator[Session]:
    """上下文管理器: with db.session_scope() as s: ..."""
    with Session(engine) as session:
        yield session


def get_session() -> Iterator[Session]:
    """FastAPI 依赖: 每请求一个 Session."""
    with Session(engine) as session:
        yield session
