"""真实验证 backfill: 小范围(5 只)联网补拉, 输出前后缓存根数对比.

用法: cd backend && ./.venv/Scripts/python.exe scripts/verify_backfill.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.core.backfill import backfill_history, pending_symbols  # noqa: E402
from app.core.datasource import build_sources, data_source_manager  # noqa: E402
from app.core.datasource.cache import kline_store  # noqa: E402

SYMBOLS = ["600111", "000001", "600519", "300750", "688146", "002594", "601318", "000858"]


def cached_len(sym: str) -> int:
    df = kline_store.get_dataframe(sym, "daily")
    return 0 if df is None else len(df)


async def main() -> None:
    db.init_db()
    data_source_manager.setup(build_sources())
    print("拉取前缓存根数:", {s: cached_len(s) for s in SYMBOLS})
    stats = await backfill_history(target=260, concurrency=4, symbols=SYMBOLS, force=True)
    print("backfill 统计:", json.dumps(stats, ensure_ascii=False, indent=1))
    print("拉取后缓存根数:", {s: cached_len(s) for s in SYMBOLS})
    # 待补列表验证(应不再包含已补成功的)
    print("当前待补列表示例(前10):", pending_symbols(260)[:10])


if __name__ == "__main__":
    asyncio.run(main())
