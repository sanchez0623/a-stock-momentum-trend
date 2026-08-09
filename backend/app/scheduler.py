"""APScheduler 定时任务(方案 §9.6).

一期: 盘后 K线预热占位
二期: 定时选股扫描 / 盘中自选盯盘
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def _after_close_warmup() -> None:
    """收盘后预热: 把自选股 + 持仓股的日线 K 线预拉入缓存.

    走 data_source_manager.get_kline(沿用 failover + K线缓存), 次日开盘前页面/选股
    直接命中缓存, 减少实时回源抖动. 任意单只失败不影响其余.
    """
    from sqlmodel import select

    from app import db
    from app.core.datasource import data_source_manager
    from app.models.models import Position, Watchlist

    try:
        with db.session_scope() as s:
            rows = s.exec(
                select(Watchlist.symbol).union(select(Position.symbol))
            ).all()
        symbols = sorted({str(r[0]) for r in rows if r and r[0]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘后预热: 读取自选/持仓失败: %s", exc)
        return

    if not symbols:
        logger.info("盘后 K 线预热: 无自选/持仓, 跳过")
        return

    logger.info("盘后 K 线预热开始: %d 只", len(symbols))
    done = 0
    for sym in symbols:
        try:
            df = await data_source_manager.get_kline(sym, "daily", count=260)
            if df is not None and not df.empty:
                done += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("盘后预热失败 %s: %s", sym, exc)
    logger.info("盘后 K 线预热完成: 成功 %d/%d", done, len(symbols))


def setup_jobs() -> None:
    if scheduler.get_job("after_close_warmup") is None:
        scheduler.add_job(
            _after_close_warmup,
            "cron",
            day_of_week="mon-fri",
            hour=16,
            minute=0,
            id="after_close_warmup",
            coalesce=True,
            max_instances=1,
        )


def start_scheduler() -> None:
    if not scheduler.running:
        setup_jobs()
        scheduler.start()
        logger.info("APScheduler 已启动")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler 已停止")
