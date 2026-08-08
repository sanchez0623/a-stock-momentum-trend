"""验证刷新端点的 progress_cb bug 已修复: 直接调用 API 函数跑一次真实刷新, 确认任务成功落库.
用法: 在 backend/ 下用「装有 baostock 的 Python」运行:
  python scripts/verify_refresh_fix.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.db import init_db
init_db()

from app.core.datasource import build_sources, data_source_manager
data_source_manager.setup(build_sources())  # 真实服务由 lifespan 调用, 独立脚本需手动初始化

from app.api import screener as scr
from app.core.screener import scan_tasks
from app.core import universe as uni_mod
from app.core import fundamentals as fund_mod


async def run_one(label: str, coro):
    resp = await coro
    tid = resp["data"]["task_id"]
    print(f"\n[{label}] 启动 task_id={tid} msg={resp['msg']}")
    for _ in range(120):  # 最多等 4 分钟
        t = scan_tasks.get(tid)
        st = t["status"]
        print(f"  状态={st} 进度={t['progress']}% error={t.get('error', '')}")
        if st in ("done", "failed"):
            return t
        await asyncio.sleep(2)
    return scan_tasks.get(tid)


async def main() -> None:
    # 1) universe(hs300) —— 验证回调不再崩
    t1 = await run_one("universe/hs300", scr.refresh_universe_api(universe="hs300"))
    print("  universe_stats:", uni_mod.universe_stats())

    # 2) fundamentals(hs300) —— 验证批量基本面落库(较慢, 串行)
    t2 = await run_one("fundamentals/hs300", scr.refresh_fundamentals_api(universe="hs300", full=False))
    print("  fundamentals_stats:", fund_mod.fundamentals_stats())

    ok = t1["status"] == "done" and t2["status"] == "done"
    print("\n=== 结论:", "✅ 修复生效, 两任务均 done" if ok else "❌ 仍有失败, 见上", "===")


if __name__ == "__main__":
    asyncio.run(main())
