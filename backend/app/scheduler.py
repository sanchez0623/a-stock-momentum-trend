"""APScheduler 定时任务(方案 §9.6).

一期: 盘后 K线预热占位
二期: 定时选股扫描 / 盘中自选盯盘
三期: 盘中实时监控预警(方案 A)
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import config_manager

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

TZ = dt.timezone(dt.timedelta(hours=8))

INTRADAY_MONITOR_JOB_ID = "intraday_monitor"


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
            w = s.exec(select(Watchlist.symbol)).all()
            p = s.exec(select(Position.symbol)).all()
        symbols = sorted({str(r[0]) for r in list(w) + list(p) if r and r[0]})
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


async def _intraday_monitor_job() -> None:
    """盘中实时监控预警任务: 仅交易时间(周一至五 9:30-15:00)执行.

    实际轮询间隔由配置「盘中监控.interval_sec」控制, 这里用 cron 秒级触发,
    内部再校验交易窗口与启用状态(双重保险)。
    """
    from app.core.config import config_manager
    from app.core.intraday import run_intraday_monitor

    cfg = config_manager.get().get("盘中监控", {})
    if not cfg.get("enabled", False):
        return

    now = dt.datetime.now(TZ)
    if now.weekday() >= 5:
        return
    hm = (now.hour, now.minute)
    if hm < (9, 30) or hm >= (15, 0):
        return

    try:
        result = await run_intraday_monitor()
        checked = result.get("checked", 0)
        alerts = result.get("alerts", 0)
        if checked or alerts:
            logger.info("盘中监控轮询: 检查 %d 只, 预警 %d 条", checked, alerts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘中监控轮询异常: %s", exc, exc_info=True)


def _setup_intraday_monitor_job() -> None:
    """按配置注册/注销盘中监控任务(幂等)."""
    cfg = config_manager.get().get("盘中监控", {}) or {}
    interval_sec = int(cfg.get("interval_sec", 30))
    # 使用 interval 触发器: 支持任意秒数间隔, 内部再校验交易窗口(9:30-15:00 周一至五)
    # interval 触发器不支持 cron 表达式的时段限制, 由任务函数内部 _is_trading_time() 双重校验
    if scheduler.get_job(INTRADAY_MONITOR_JOB_ID) is None:
        scheduler.add_job(
            _intraday_monitor_job,
            "interval",
            seconds=interval_sec,
            id=INTRADAY_MONITOR_JOB_ID,
            coalesce=True,
            max_instances=1,
        )
        logger.info("盘中监控任务已注册: 每 %d 秒", interval_sec)


def _teardown_intraday_monitor_job() -> None:
    if scheduler.get_job(INTRADAY_MONITOR_JOB_ID) is not None:
        scheduler.remove_job(INTRADAY_MONITOR_JOB_ID)
        logger.info("盘中监控任务已注销")


def register_intraday_monitor_listener() -> None:
    """总开关热生效: 开启 -> 注册任务; 关闭 -> 注销任务."""

    def _on_change(snapshot: dict[str, Any]) -> None:
        enabled = bool((snapshot.get("盘中监控") or {}).get("enabled"))
        active = scheduler.get_job(INTRADAY_MONITOR_JOB_ID) is not None
        try:
            if enabled and not active:
                _setup_intraday_monitor_job()
            elif not enabled and active:
                _teardown_intraday_monitor_job()
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中监控任务热更新失败: %s", exc)

    config_manager.register_listener(_on_change)


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
    # 盘中实时监控预警(方案 A): 开关驱动注册/注销, 配置变化热生效
    _setup_intraday_monitor_job()
    register_intraday_monitor_listener()
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
