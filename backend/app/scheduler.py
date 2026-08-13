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


async def _after_close_daily_report() -> None:
    """盘后日报: 当日回顾 + 明日行动清单(全只读). 开关在配置「日报.enabled」."""
    from app.core.config import config_manager

    cfg = config_manager.get().get("日报", {})
    if not cfg.get("enabled", True):
        return
    from app.core.report.service import report_service

    try:
        report = await report_service.generate()
        logger.info("盘后日报已生成: %s (status=%s)", report.date, report.status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘后日报生成失败: %s", exc)


async def _tracking_sample() -> None:
    """得分追踪采样(盘前 8:50 / 午间 12:30 / 盘后 16:00): 对活跃追踪票记录得分/价格/阶段/信号.

    三个采样点均落在日线缓存复用窗口(盘前/午休/盘后), 不触发重拉风暴;
    盘后 16:00 首次采样会按缓存规则自动重拉收盘数据.
    """
    from app.core.tracking import archive_expired, sample_all

    try:
        r = await sample_all()
        if r["total"]:
            logger.info("得分追踪采样: %d/%d 成功", r["ok"], r["total"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("得分追踪采样失败: %s", exc)
    try:
        archive_expired()
    except Exception as exc:  # noqa: BLE001
        logger.warning("得分追踪归档失败: %s", exc)


def _add_tracking_job(job_id: str, hour: int, minute: int) -> None:
    if scheduler.get_job(job_id) is None:
        scheduler.add_job(
            _tracking_sample,
            "cron",
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            id=job_id,
            coalesce=True,
            max_instances=1,
        )


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
    if scheduler.get_job("daily_report") is None:
        scheduler.add_job(
            _after_close_daily_report,
            "cron",
            day_of_week="mon-fri",
            hour=16,
            minute=30,
            id="daily_report",
            coalesce=True,
            max_instances=1,
        )
    # 得分追踪每日 2 次采样(午间 12:30 / 盘后 16:00; 盘前 8:50 与盘后数据重复, 已去掉)
    _add_tracking_job("tracking_sample_1230", 12, 30)
    _add_tracking_job("tracking_sample_1600", 16, 0)
    # AI 助理(独立模块): 开关驱动注册/注销, 配置变化热生效
    from app.core.assistant.scheduler import register_assistant_listener, setup_assistant_jobs

    setup_assistant_jobs(scheduler)
    register_assistant_listener(scheduler)


def start_scheduler() -> None:
    if not scheduler.running:
        setup_jobs()
        scheduler.start()
        logger.info("APScheduler 已启动")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler 已停止")
