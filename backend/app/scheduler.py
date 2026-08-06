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
    """收盘后预热自选股 K线(占位, 二期接入真实逻辑)."""
    logger.info("盘后 K线预热任务触发(占位)")


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
