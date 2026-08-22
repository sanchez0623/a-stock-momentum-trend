"""测试: db 补列迁移机制(旧库缺列 -> 启动自动 ALTER)."""

from __future__ import annotations

from sqlalchemy import text


def test_migrate_columns_adds_missing(tmp_engine):
    """旧库缺 fingerprint 列时 _migrate_columns 自动补列(与启动流程一致)."""
    from app import db

    # 模拟旧库: 重建 notification 表(无 fingerprint 列)
    with db.engine.connect() as conn:
        conn.execute(text("ALTER TABLE notification RENAME TO notification_old"))
        conn.execute(text(
            "CREATE TABLE notification (id INTEGER PRIMARY KEY, time TEXT, "
            "category TEXT, title TEXT, content TEXT, read BOOLEAN)"))
        conn.execute(text("DROP TABLE notification_old"))
        conn.commit()

    db._migrate_columns()

    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(notification)"))}
        assert "fingerprint" in cols


def test_migrate_columns_idempotent(tmp_engine):
    """新库全列齐全时迁移静默跳过(幂等, 不报错)."""
    from app import db

    db._migrate_columns()  # 新表已有全部列 -> 无操作
    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(notification)"))}
        assert "fingerprint" in cols
