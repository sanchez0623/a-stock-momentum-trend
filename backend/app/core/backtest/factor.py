"""因子回测(MVP, 方案C: 阶段分桶 + 总分分桶验证).

目的: 用历史数据回答两个问题:
1.「启动/加速/过热/衰竭 各阶段买入, 未来 N 日胜率与期望收益差多少」—— 验证"刚起趋势优于追高"的打法;
2.「选股总分 50-60 / 60-70 / 70+ 分买入, 未来 N 日收益是否单调递增」—— 验证评分体系本身的有效性
   (若高分桶期望不优于低分桶, 说明分数与未来收益脱节, 评分体系需要重校).

规则(防未来函数, 与 docs/回测中心-回测功能设计方案.md §4 一致):
- 信号日 T 收盘数据判定阶段/评分(指标只用到 T, 无前视)
- T+1 开盘价买入(A股 T+1, 信号日不可成交)
- T+1+N 收盘价卖出
- 可选扣除手续费(复用 fees.compute_trade_fee, 佣金/印花税/三费与实盘一致)

性能: 每只股票只跑一次 compute_all(向量化), 逐日判定用 score_indicators(ind, cfg, end=i)
复用整段指标(含阶段判定, 与总分同源), 不反复复制全量 —— 全市场 5000 只 × 800 根能在分钟级完成。

输出: 按 阶段×持有期 与 总分×持有期 分桶的 样本数/胜率/平均收益/中位数/期望(净收益),
附样本范围与阶段/分数分布。总分口径 = 技术面三因子 + 阶段加减分(与选股中心排序分同源,
不含基本面/事件因子 —— 回测只有 K 线, 无逐日历史基本面数据)。
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
from app.core.screener.engine import score_indicators

logger = logging.getLogger(__name__)

# 阶段 -> 中文标签(报告用)
STAGE_LABELS = {
    "launch": "启动期",
    "accelerate": "加速期",
    "overheat": "过热期",
    "exhaust": "衰竭期",
    "none": "无趋势",
}

# 总分分桶: (key, 下界含, 上界不含)。含低分区作对照, 看收益是否随分数单调递增
SCORE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-40", 0.0, 40.0),
    ("40-50", 40.0, 50.0),
    ("50-60", 50.0, 60.0),
    ("60-70", 60.0, 70.0),
    ("70+", 70.0, 1e9),
)

DEFAULT_HOLD_DAYS = (5, 10, 20)
DEFAULT_MIN_BARS = 60          # 不足此根数不参与(指标/阶段判定需要预热)
FIXED_QTY = 100                # 每笔固定股数(费用与收益计算的基准, 不影响胜率口径)


def _score_bucket(total: float) -> str:
    """总分 -> 分桶 key(按 SCORE_BUCKETS 顺序首个命中区间)."""
    for key, lo, hi in SCORE_BUCKETS:
        if lo <= total < hi:
            return key
    return SCORE_BUCKETS[0][0]


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
    """阶段 + 总分双分桶因子回测.

    symbols: 为空则取缓存中全部有有效日线的股票(盘后预热落库的全市场).
    返回报告: {meta, by_stage, by_score, stage_distribution, score_distribution}
    """
    cfg = config_manager.get()
    fee_cfg = cfg.get("手续费", {}) if cost else None
    hold_days = tuple(sorted(hold_days))

    # ---- 解析股票池: 显式传入 or 缓存全部有效段
    if symbols is None:
        symbols = kline_store.list_symbols(period="daily")
    total = len(symbols)
    logger.info("因子回测(阶段+总分分桶): 股票池 %d 只, hold_days=%s, cost=%s", total, hold_days, cost)

    # 分桶: stage -> hold_day -> 收益率列表; score_bucket -> hold_day -> 收益率列表
    buckets: dict[str, dict[int, list[float]]] = {}
    score_buckets: dict[str, dict[int, list[float]]] = {}
    stage_hits: dict[str, int] = {}
    score_hits: dict[str, int] = {}
    sample_dates: list[str] = []
    n_used = 0

    for idx, sym in enumerate(symbols):
        if progress_cb and (idx % 50 == 0 or idx == total - 1):
            progress_cb(idx + 1, total)
        rows = kline_store.load(sym, "daily") or []
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
            # 逐日评分(含阶段判定, 二者同源): stage 取自评分结果, 保证与总分口径一致
            sc = score_indicators(ind, cfg, end=i, with_reason=False)
            stage = sc["stage"]
            stage_hits[stage] = stage_hits.get(stage, 0) + 1
            b = buckets.setdefault(stage, {})
            skey = _score_bucket(sc["total"])
            score_hits[skey] = score_hits.get(skey, 0) + 1
            sb = score_buckets.setdefault(skey, {})
            for hd in hold_days:
                # T+1 开盘买入, T+1+hd 收盘卖出(索引: 买入=i+1, 卖出=i+1+hd)
                buy_px = _f(opens[i + 1])
                sell_px = _f(closes[i + 1 + hd])
                if buy_px <= 0 or sell_px <= 0:
                    continue
                ret = _net_return(buy_px, sell_px, fee_cfg) if fee_cfg is not None \
                    else (sell_px - buy_px) / buy_px * 100
                b.setdefault(hd, []).append(ret)
                sb.setdefault(hd, []).append(ret)
            sample_dates.append(str(ind["date"].iloc[i]))

    # ---- 汇总(阶段分桶)
    by_stage: dict[str, dict[str, Any]] = {}
    for stage, per_hold in buckets.items():
        by_stage[stage] = {
            "label": STAGE_LABELS.get(stage, stage),
            "holds": {f"hold_{hd}": _summarize(per_hold.get(hd, [])) for hd in hold_days},
        }
    dist = {k: v for k, v in sorted(stage_hits.items(), key=lambda x: -x[1])}

    # ---- 汇总(总分分桶, 按分数区间从低到高排序, 便于看单调性)
    bucket_order = [k for k, _, _ in SCORE_BUCKETS]
    by_score: dict[str, dict[str, Any]] = {}
    for key in bucket_order:
        per_hold = score_buckets.get(key)
        if not per_hold:
            continue
        by_score[key] = {
            "label": f"{key}分",
            "holds": {f"hold_{hd}": _summarize(per_hold.get(hd, [])) for hd in hold_days},
        }
    score_dist = {k: score_hits[k] for k in bucket_order if k in score_hits}

    fee_note = ("净收益已扣双边手续费(佣金万0.5最低5元+印花税万5+三费)" if cost else "未扣费")
    meta = {
        "symbols_total": total,
        "symbols_used": n_used,
        "hold_days": list(hold_days),
        "cost_included": cost,
        "date_from": min(sample_dates) if sample_dates else "",
        "date_to": max(sample_dates) if sample_dates else "",
        "notes": (f"信号日T收盘评分(总分=技术面三因子+阶段加减分, 与选股中心排序分同源, 不含基本面/事件因子), "
                  f"T+1开盘买入, T+N收盘卖出; {fee_note}"),
    }
    return {"meta": meta, "by_stage": by_stage, "by_score": by_score,
            "stage_distribution": dist, "score_distribution": score_dist}


def stage_label(key: str) -> str:
    return STAGE_LABELS.get(key, key)
