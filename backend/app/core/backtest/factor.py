"""因子回测(MVP, 方案C: 阶段分桶验证).

目的: 用历史数据回答「启动/加速/过热/衰竭 各阶段买入, 未来 N 日胜率与期望收益差多少」,
为"刚起趋势优于追高"的打法提供数据依据。

规则(防未来函数, 与 docs/回测中心-回测功能设计方案.md §4 一致):
- 信号日 T 收盘数据判定阶段(指标只用到 T, 无前视)
- T+1 开盘价买入(A股 T+1, 信号日不可成交)
- T+1+N 收盘价卖出
- 可选扣除手续费(复用 fees.compute_trade_fee, 佣金/印花税/三费与实盘一致)

性能: 每只股票只跑一次 compute_all(向量化), 逐日判定用 detect_stage(ind, cfg, end=i)
只取窗口数据, 不反复复制全量 —— 全市场 5000 只 × 800 根也能在分钟级完成。

输出: 按 阶段 × 持有期 分桶的 样本数/胜率/平均收益/中位数/期望(净收益), 附样本范围与阶段分布。
"""

from __future__ import annotations

import logging
import statistics
from typing import Any, Callable

import pandas as pd

from app.core.config import config_manager
from app.core.datasource import kline_store
from app.core.fees import compute_trade_fee
from app.core.indicators import compute_all
from app.core.screener.engine import detect_stage

logger = logging.getLogger(__name__)

# 阶段 -> 中文标签(报告用)
STAGE_LABELS = {
    "launch": "启动期",
    "accelerate": "加速期",
    "overheat": "过热期",
    "exhaust": "衰竭期",
    "none": "无趋势",
}

DEFAULT_HOLD_DAYS = (5, 10, 20)
DEFAULT_MIN_BARS = 60          # 不足此根数不参与(指标/阶段判定需要预热)
FIXED_QTY = 100                # 每笔固定股数(费用与收益计算的基准, 不影响胜率口径)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def _to_df(rows: list[dict]) -> pd.DataFrame | None:
    """缓存段 JSON -> DataFrame; 校验必需列."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    need = {"date", "open", "high", "low", "close", "volume", "amount"}
    if not need.issubset(df.columns):
        return None
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df if len(df) > 0 else None


def _net_return(buy_price: float, sell_price: float, fee_cfg: dict) -> float:
    """单笔净收益率(%, 扣除双边手续费). buy/sell 为成交价."""
    if buy_price <= 0:
        return 0.0
    amount_buy = buy_price * FIXED_QTY
    amount_sell = sell_price * FIXED_QTY
    fee = compute_trade_fee("buy", amount_buy, fee_cfg) + compute_trade_fee("sell", amount_sell, fee_cfg)
    return (amount_sell - amount_buy - fee) / amount_buy * 100


def _summarize(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"n": 0, "win_rate": 0.0, "avg": 0.0, "median": 0.0, "expectancy": 0.0}
    wins = [r for r in returns if r > 0]
    return {
        "n": len(returns),
        "win_rate": round(len(wins) / len(returns) * 100, 1),
        "avg": round(statistics.mean(returns), 3),
        "median": round(statistics.median(returns), 3),
        "expectancy": round(statistics.mean(returns), 3),  # 净收益均值即期望(已扣费)
    }


def backtest_factors(
    symbols: list[str] | None = None,
    hold_days: tuple[int, ...] = DEFAULT_HOLD_DAYS,
    min_bars: int = DEFAULT_MIN_BARS,
    cost: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """阶段分桶因子回测.

    symbols: 为空则取缓存中全部有有效日线的股票(盘后预热落库的全市场).
    返回报告: {meta, by_stage: {stage: {hold_N: {...}}}, stage_distribution, samples}
    """
    cfg = config_manager.get()
    fee_cfg = cfg.get("手续费", {}) if cost else None
    hold_days = tuple(sorted(hold_days))

    # ---- 解析股票池: 显式传入 or 缓存全部有效段
    if symbols is None:
        symbols = kline_store.list_symbols(period="daily")
    total = len(symbols)
    logger.info("阶段分桶回测: 股票池 %d 只, hold_days=%s, cost=%s", total, hold_days, cost)

    # 分桶: stage -> hold_day -> 收益率列表
    buckets: dict[str, dict[int, list[float]]] = {}
    stage_hits: dict[str, int] = {}
    sample_dates: list[str] = []
    n_used = 0

    for idx, sym in enumerate(symbols):
        if progress_cb and (idx % 50 == 0 or idx == total - 1):
            progress_cb(idx + 1, total)
        rows = kline_store.load(sym, "daily")
        df = _to_df(rows)
        if df is None or len(df) < min_bars + 2:
            continue
        ind = compute_all(
            df,
            ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
            macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
            rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
            volume_ma=cfg["量能"]["volume_ma"],
        )
        n = len(ind)
        closes = ind["close"].tolist()
        opens = ind["open"].tolist()
        max_hold = max(hold_days)
        n_used += 1

        for i in range(min_bars, n - 1 - max_hold):
            stage = detect_stage(ind, cfg, end=i)["stage"]
            stage_hits[stage] = stage_hits.get(stage, 0) + 1
            b = buckets.setdefault(stage, {})
            for hd in hold_days:
                # T+1 开盘买入, T+1+hd 收盘卖出(索引: 买入=i+1, 卖出=i+1+hd)
                buy_px = _f(opens[i + 1])
                sell_px = _f(closes[i + 1 + hd])
                if buy_px <= 0 or sell_px <= 0:
                    continue
                ret = _net_return(buy_px, sell_px, fee_cfg) if fee_cfg is not None \
                    else (sell_px - buy_px) / buy_px * 100
                b.setdefault(hd, []).append(ret)
            sample_dates.append(str(ind["date"].iloc[i]))

    # ---- 汇总
    by_stage: dict[str, dict[str, Any]] = {}
    for stage, per_hold in buckets.items():
        by_stage[stage] = {
            "label": STAGE_LABELS.get(stage, stage),
            "holds": {f"hold_{hd}": _summarize(per_hold.get(hd, [])) for hd in hold_days},
        }
    dist = {k: v for k, v in sorted(stage_hits.items(), key=lambda x: -x[1])}

    meta = {
        "symbols_total": total,
        "symbols_used": n_used,
        "hold_days": list(hold_days),
        "cost_included": cost,
        "date_from": min(sample_dates) if sample_dates else "",
        "date_to": max(sample_dates) if sample_dates else "",
        "notes": "信号日T收盘判定阶段, T+1开盘买入, T+N收盘卖出; 净收益已扣双边手续费(佣金万0.5最低5元+印花税万5+三费)" if cost
                 else "信号日T收盘判定阶段, T+1开盘买入, T+N收盘卖出; 未扣费",
    }
    return {"meta": meta, "by_stage": by_stage, "stage_distribution": dist}


def stage_label(key: str) -> str:
    return STAGE_LABELS.get(key, key)
