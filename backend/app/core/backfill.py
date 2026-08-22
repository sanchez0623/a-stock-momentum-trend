"""历史 K 线补拉(backfill): 把全市场日线缓存从 ~80 根补到 ~260 根.

设计要点(复用现有机制, 零新数据源):
- 拉取走 data_source_manager.get_kline(五源 failover + 熔断 + 东财降频), akshare 仅末位兜底, 免疫单源限流
- 落库走 kline_store.merge_and_save(按 date 去重, 幂等), 重复拉取安全
- 断点续跑: 每次只处理"未达标/缓存陈旧/未缓存"的股票, 中断后重跑即续跑
- 次新股(历史本身短但数据新鲜)自动排除, 不反复重拉

用法(API): POST /api/kline/backfill -> task_id; GET /api/kline/backfill/status/{id} 轮询.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any, Callable

from sqlmodel import Session, select

from app import db
from app.core.datasource import data_source_manager
from app.core.screener.tasks import ScanTaskManager
from app.models.models import KlineCache, Stock

logger = logging.getLogger(__name__)

DEFAULT_TARGET = 260      # 目标根数(252 交易日 + 余量)
DEFAULT_CONCURRENCY = 4   # 并发数
BATCH_SLEEP = 0.5         # 批间休息(秒), 防瞬时打爆数据源
FRESH_DAYS = 10           # 最后一根 K 线距今天 <= N 天视为"数据新鲜"

# 补拉任务状态(内存态, 重启即清, 与扫描任务一致)
backfill_tasks = ScanTaskManager()

ProgressCb = Callable[[int, int], None]


def _fresh_date(date_str: str) -> bool:
    """K 线最后日期是否足够新(覆盖周末/节假日, 10 天内即视为新鲜)."""
    try:
        d = dt.date.fromisoformat(str(date_str)[:10])
        return (dt.date.today() - d).days <= FRESH_DAYS
    except (ValueError, TypeError):
        return False


def _is_deeply_insufficient(bars: list[dict], target: int) -> bool:
    """判定"短但新鲜"的缓存是否真的已尽力, 还是源没给全需重拉.

    - 达成度 >= 90%: 接近目标, 不再折腾 -> 已尽力
    - 首根日期距今 <= 一年(约 365 天): 真次新股, 上市时间就短 -> 已尽力
    - 否则: 老股但缓存明显不足(如仅 80 根) -> 源没给全, 标记待补重拉
    """
    if len(bars) >= target * 0.9:
        return False
    try:
        first = dt.date.fromisoformat(str(bars[0].get("date", ""))[:10])
        return (dt.date.today() - first).days > 365
    except (ValueError, TypeError):
        return False  # 首日无法解析, 保守不重拉


def _load_cache_status(target: int) -> dict[str, str]:
    """读取全部日线缓存, 返回 symbol -> 状态: ok(达标或已尽力) / stale(陈旧/源没给全) / missing(无)."""
    status: dict[str, str] = {}
    try:
        with Session(db.engine) as s:
            rows = s.exec(
                select(KlineCache.symbol, KlineCache.ohlcv_json)
                .where(KlineCache.period == "daily")
            ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: 读取缓存状态失败: %s", exc)
        return {}
    for sym, blob in rows:
        try:
            bars = json.loads(blob or "[]")
        except (ValueError, TypeError):
            status[str(sym)] = "stale"
            continue
        if not bars:
            status[str(sym)] = "missing"
        elif len(bars) >= target:
            status[str(sym)] = "ok"
        elif _fresh_date(bars[-1].get("date", "")):
            # 数据新鲜但不足: 只有次新/接近目标才视为已尽力, 老股不足则重拉
            if _is_deeply_insufficient(bars, target):
                status[str(sym)] = "stale"
            else:
                status[str(sym)] = "ok"
        else:
            status[str(sym)] = "stale"
    return status


# 补拉排除的代码段: "92"=北交所新代码段(920xxx, 上市时间短/数据不全/流动性差), 默认不补拉
EXCLUDE_PREFIXES = ("92",)


def _filter_symbols(symbols: list[str]) -> list[str]:
    """过滤掉不适于补拉的代码段(92 开头北交所)."""
    return [s for s in symbols if not s.startswith(EXCLUDE_PREFIXES)]


def _all_symbols() -> list[str]:
    """全市场股票列表(stock 表), 已排除 92 开头(北交所)."""
    try:
        with Session(db.engine) as s:
            rows = s.exec(select(Stock.symbol)).all()
        return _filter_symbols(sorted({str(r) for r in rows if r}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: 读取股票列表失败: %s", exc)
        return []


def pending_symbols(target: int = DEFAULT_TARGET) -> list[str]:
    """待补列表: 未缓存 / 缓存陈旧(根数不足且最后日期偏老) 的股票.

    - 已达标(根数 >= target)与已尽力(短但新鲜)自动排除 -> 断点续跑/次新股免疫.
    """
    status = _load_cache_status(target)
    all_syms = _all_symbols()
    if not all_syms:
        return []
    return [s for s in all_syms if status.get(s) != "ok"]


async def backfill_history(
    target: int = DEFAULT_TARGET,
    concurrency: int = DEFAULT_CONCURRENCY,
    symbols: list[str] | None = None,
    force: bool = False,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """补拉日线到 target 根.

    symbols: 指定股票列表(默认全市场). force=True 时忽略"已达标"跳过(测试/强刷用).
    返回统计: {target, universe, pending, done, insufficient, failed, started_at, finished_at}.
    """
    if symbols is None:
        universe = _all_symbols()
        pending = pending_symbols(target)
        if not force:
            pending = [s for s in pending]
    else:
        universe = list(symbols)
        cache_status = _load_cache_status(target)
        pending = [s for s in symbols if force or cache_status.get(s) != "ok"]

    total = len(pending)
    stats: dict[str, Any] = {
        "target": target,
        "universe": len(universe),
        "pending": total,
        "done": 0,
        "insufficient": [],
        "failed": [],
        "started_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if total == 0:
        logger.info("backfill: 无可补股票(全市场 %d 只均已达标或已尽力)", len(universe))
        stats["finished_at"] = stats["started_at"]
        return stats

    logger.info("backfill 开始: 全市场 %d, 待补 %d (target=%d, 并发=%d)",
                len(universe), total, target, concurrency)
    done = 0
    sem = asyncio.Semaphore(concurrency)

    async def _one(sym: str) -> tuple[str, str]:
        """返回 (状态, 详情): ok / insufficient(历史短, 已尽力) / failed."""
        async with sem:
            try:
                # prefer_long: 根数不足的源(mootdx/腾讯通常只给 ~120 根)不终止,
                # 继续尝试 baostock/东财等能拉长历史的源, 尽量凑满 target
                df = await data_source_manager.get_kline(sym, "daily", target, prefer_long=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("backfill %s 拉取异常: %s", sym, exc)
                return "failed", f"异常: {exc}"
            if df is None or df.empty:
                return "failed", "无数据"
            if len(df) >= target:
                return "ok", ""
            # 不足 target: 数据新鲜视为已尽力(上市短/源限制), 陈旧视为未完成(下次重跑再试)
            last = str(df.iloc[-1].get("date", ""))
            if _fresh_date(last):
                return "insufficient", f"仅{len(df)}根(历史短/源限制)"
            return "failed", f"仅{len(df)}根且数据陈旧"

    for i in range(0, total, concurrency):
        batch = pending[i:i + concurrency]
        results = await asyncio.gather(*(_one(s) for s in batch))
        for sym, (status, detail) in zip(batch, results, strict=True):
            if status == "ok":
                done += 1
            elif status == "insufficient":
                stats["insufficient"].append({"symbol": sym, "detail": detail})
            else:
                stats["failed"].append({"symbol": sym, "error": detail})
        stats["done"] = done
        if progress_cb:
            progress_cb(done, total)
        if i + concurrency < total:
            await asyncio.sleep(BATCH_SLEEP)

    stats["finished_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("backfill 完成: 成功 %d/%d, 失败 %d, 异常原因: %s",
                done, total, len(stats["failed"]),
                "; ".join(sorted({e for _, e in stats["failed"][:10]})))
    return stats
