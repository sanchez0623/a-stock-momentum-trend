"""一次性清理: 日线缓存按归一化日期去重(修复跨源日期格式不一致导致的重复行).

背景: 腾讯返回 '2026-02-06' 而旧缓存是 '2026-02-06 15:00', merge 按原字符串去重失败,
同一天存两条 -> 缓存膨胀 + 指标/回测污染. cache.py merge_and_save 已改为归一化去重,
本脚本对存量缓存做一次清洗(空合并触发去重重写), 之后正常读写即自愈.

用法: cd backend && ./.venv/Scripts/python.exe scripts/dedupe_kline_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.core.datasource.cache import kline_store  # noqa: E402
from app.models.models import KlineCache  # noqa: E402
from sqlmodel import Session, select  # noqa: E402


def main() -> None:
    db.init_db()
    with Session(db.engine) as s:
        rows = s.exec(select(KlineCache.symbol, KlineCache.period, KlineCache.ohlcv_json)).all()
    total_fixed = 0
    total_bars = 0
    for sym, period, blob in rows:
        if period != "daily" or not blob or blob == "[]":
            continue
        before = len(kline_store.load(sym, period) or [])
        df = kline_store.merge_and_save(sym, period, [])  # 空合并 -> 归一化去重重写
        after = len(df)
        if after < before:
            total_fixed += 1
            print(f"{sym}: {before} -> {after} (去重 {before - after})")
        total_bars += after
    print(f"完成: 处理 {len(rows)} 段, 修复 {total_fixed} 只, 现有日线总根数 {total_bars}")


if __name__ == "__main__":
    main()
