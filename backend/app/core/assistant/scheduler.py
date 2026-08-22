"""AI 助理 - APScheduler 任务注册/注销(开关热生效).

总开关 ai_assistant.enabled 变化时由 config listener 动态注册/注销三个 job:
- premarket:   周一至五 08:30 盘前观察清单
- intraday:    周一至五 9-15 点每 5 分钟(内部再校验 9:30-15:00 交易窗口)
- after_close: 周一至五 16:30 盘后日报(复用 report 模块)
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.core.config import config_manager

logger = logging.getLogger(__name__)

JOB_IDS = ("assistant_premarket", "assistant_intraday", "assistant_after_close")
TZ = dt.timezone(dt.timedelta(hours=8))


async def _run_phase(phase: str) -> None:
    from app.core.assistant.pipeline import run_phase

    try:
        result = await run_phase(phase)
        logger.info("AI 助理 %s 完成: 通知 %d 条",
                    phase, len(result.get("notifications") or []))
    except Exception:  # noqa: BLE001
        logger.warning("AI 助理 %s 失败", phase, exc_info=True,
                       extra={"component": "assistant", "phase": phase})


async def _premarket_job() -> None:
    await _run_phase("premarket")


async def _intraday_job() -> None:
    # 交易时间窗(周一至五 9:30-15:00); 非窗口期空转
    now = dt.datetime.now(TZ)
    if now.weekday() >= 5:
        return
    hm = (now.hour, now.minute)
    if hm < (9, 30) or hm >= (15, 0):
        return
    await _run_phase("intraday")


async def _after_close_job() -> None:
    await _run_phase("after_close")


def setup_assistant_jobs(scheduler: Any) -> None:
    """按当前配置注册缺失的 job(幂等)."""
    cfg = config_manager.get().get("ai_assistant", {}) or {}
    if not cfg.get("enabled"):
        return
    pm = cfg.get("premarket", {}) or {}
    if pm.get("enabled", True) and scheduler.get_job("assistant_premarket") is None:
        scheduler.add_job(_premarket_job, "cron", day_of_week="mon-fri",
                          hour=int(pm.get("hour", 8)), minute=int(pm.get("minute", 30)),
                          id="assistant_premarket", coalesce=True, max_instances=1)
    ic = cfg.get("intraday", {}) or {}
    if ic.get("enabled", True) and scheduler.get_job("assistant_intraday") is None:
        scheduler.add_job(_intraday_job, "cron", day_of_week="mon-fri",
                          hour="9-15", minute="*/5",
                          id="assistant_intraday", coalesce=True, max_instances=1)
    ac = cfg.get("after_close", {}) or {}
    if ac.get("enabled", True) and scheduler.get_job("assistant_after_close") is None:
        scheduler.add_job(_after_close_job, "cron", day_of_week="mon-fri",
                          hour=int(ac.get("hour", 16)), minute=int(ac.get("minute", 30)),
                          id="assistant_after_close", coalesce=True, max_instances=1)
    logger.info("AI 助理任务已注册(盘前/盘中/盘后)")


def teardown_assistant_jobs(scheduler: Any) -> None:
    for jid in JOB_IDS:
        if scheduler.get_job(jid) is not None:
            scheduler.remove_job(jid)
    logger.info("AI 助理任务已注销")


def register_assistant_listener(scheduler: Any) -> None:
    """总开关热生效: 开启 -> 注册任务; 关闭 -> 注销任务."""

    def _on_change(snapshot: dict[str, Any]) -> None:
        enabled = bool((snapshot.get("ai_assistant") or {}).get("enabled"))
        active = scheduler.get_job("assistant_premarket") is not None
        try:
            if enabled and not active:
                setup_assistant_jobs(scheduler)
            elif not enabled and active:
                teardown_assistant_jobs(scheduler)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 助理任务热更新失败: %s", exc)

    config_manager.register_listener(_on_change)
