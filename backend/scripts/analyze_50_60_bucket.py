"""50-60 分桶解剖: 定位期望塌陷集中在哪个因子组合.

分析内容(全部用 hold_20 净收益):
A. 分数桶 × 阶段 交叉: 50-60 塌陷是普跌还是集中在特定阶段
B. 50-60 桶内: 动量分档 × 阶段 交叉(动量分高+趋势中段 = 猜想中的追高票)
C. 50-60 桶内: 趋势分档 × 阶段 交叉
D. 50-60 桶内 ROC/RSI/乖离/ADX 分布分位数(对照 40-50 / 60-70)
E. 猜想直接验证: 50-60 且动量分>=28 的样本, 按阶段切期望

用法: python scripts/analyze_50_60_bucket.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.backtest.factor import _f, _net_return, _score_bucket, _to_df
from app.core.config import config_manager
from app.core.datasource import kline_store
from app.core.indicators import compute_all
from app.core.screener.engine import score_indicators

HOLD = 20
MIN_BARS = 60
STAGE_CN = {"launch": "启动", "accelerate": "加速", "overheat": "过热",
            "exhaust": "衰竭", "none": "无趋势"}


def collect() -> pd.DataFrame:
    cfg = config_manager.get()
    fee_cfg = cfg.get("手续费", {})
    rows = []
    symbols = kline_store.list_symbols(period="daily")
    for idx, sym in enumerate(symbols):
        if idx % 200 == 0:
            print(f"  ...{idx}/{len(symbols)}", flush=True)
        df = _to_df(kline_store.load(sym, "daily") or [])
        if df is None or len(df) < MIN_BARS + 2:
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
        for i in range(MIN_BARS, n - 1 - HOLD):
            buy = _f(opens[i + 1])
            sell = _f(closes[i + 1 + HOLD])
            if buy <= 0 or sell <= 0:
                continue
            sc = score_indicators(ind, cfg, end=i, with_reason=False)
            rows.append({
                "bucket": _score_bucket(sc["total"]),
                "total": sc["total"],
                "stage": sc["stage"],
                "stage_sub": sc.get("stage_sub") or "",
                "trend": sc["trend_score"],
                "mom": sc["momentum_score"],
                "vol": sc["volume_score"],
                "roc": sc.get("roc", 0.0),
                "rsi": sc.get("rsi", 0.0),
                "bias": sc.get("bias", 0.0),
                "adx": sc.get("adx", 0.0),
                "ret20": _net_return(buy, sell, fee_cfg),
            })
    return pd.DataFrame(rows)


def _agg(g: pd.DataFrame) -> str:
    if len(g) == 0:
        return "-"
    return f"n={len(g):>6} 期望{g['ret20'].mean():+6.2f}% 胜率{(g['ret20'] > 0).mean() * 100:4.1f}%"


def mom_band(v: float) -> str:
    return "低<20" if v < 20 else ("中20-28" if v < 28 else "高28+")


def trend_band(v: float) -> str:
    return "低<15" if v < 15 else ("中15-30" if v < 30 else "高30+")


def main() -> None:
    df = collect()
    print(f"\n样本总数 {len(df)}, 持有 {HOLD} 日(净收益)\n")

    buckets = ["0-40", "40-50", "50-60", "60-70", "70+"]

    # ---- A. 分数桶 × 阶段
    print("=== A. 分数桶 × 阶段 (20日期望) ===")
    for b in buckets:
        sub = df[df["bucket"] == b]
        if not len(sub):
            continue
        cells = [f"{STAGE_CN[s]}: {_agg(g)}" for s, g in sub.groupby("stage")
                 if s in STAGE_CN]
        print(f"[{b}分]")
        for c in cells:
            print(f"   {c}")

    # ---- B. 50-60 桶内 动量分档 × 阶段
    t = df[df["bucket"] == "50-60"]
    print(f"\n=== B. 50-60桶 动量分档 × 阶段 ===")
    for band in ("低<20", "中20-28", "高28+"):
        sub = t[t["mom"].map(mom_band) == band]
        print(f"动量{band}  合计 {_agg(sub)}")
        for s in ("launch", "accelerate", "overheat", "exhaust", "none"):
            g = sub[sub["stage"] == s]
            if len(g):
                print(f"    {STAGE_CN[s]}: {_agg(g)}")

    # ---- C. 50-60 桶内 趋势分档 × 阶段
    print(f"\n=== C. 50-60桶 趋势分档 × 阶段 ===")
    for band in ("低<15", "中15-30", "高30+"):
        sub = t[t["trend"].map(trend_band) == band]
        print(f"趋势{band}  合计 {_agg(sub)}")
        for s in ("launch", "accelerate", "overheat", "exhaust", "none"):
            g = sub[sub["stage"] == s]
            if len(g):
                print(f"    {STAGE_CN[s]}: {_agg(g)}")

    # ---- D. 指标分布分位数
    print("\n=== D. 指标分布分位数 (P25/P50/P75) ===")
    for b in buckets:
        sub = df[df["bucket"] == b]
        if not len(sub):
            continue
        q = sub[["roc", "rsi", "bias", "adx", "mom", "trend", "vol"]].quantile([0.25, 0.5, 0.75])
        print(f"[{b}分] ROC {q['roc'][0.25]:.1f}/{q['roc'][0.5]:.1f}/{q['roc'][0.75]:.1f}"
              f"  RSI {q['rsi'][0.25]:.0f}/{q['rsi'][0.5]:.0f}/{q['rsi'][0.75]:.0f}"
              f"  乖离 {q['bias'][0.25]:.1f}/{q['bias'][0.5]:.1f}/{q['bias'][0.75]:.1f}"
              f"  ADX {q['adx'][0.25]:.0f}/{q['adx'][0.5]:.0f}/{q['adx'][0.75]:.0f}"
              f"  动量分 {q['mom'][0.25]:.0f}/{q['mom'][0.5]:.0f}/{q['mom'][0.75]:.0f}"
              f"  趋势分 {q['trend'][0.25]:.0f}/{q['trend'][0.5]:.0f}/{q['trend'][0.75]:.0f}")

    # ---- E. 直接验证猜想: 高动量分在各阶段 vs 低动量分
    print("\n=== E. 猜想验证: 同阶段下动量分高 vs 低 (全样本, 排除分数桶干扰) ===")
    for s in ("launch", "accelerate", "overheat"):
        sub = df[df["stage"] == s]
        hi = sub[sub["mom"] >= 28]
        lo = sub[sub["mom"] < 20]
        print(f"{STAGE_CN[s]}: 动量高(28+) {_agg(hi)}")
        print(f"{'':>10s} 动量低(<20) {_agg(lo)}")

    # ---- F. 加速期子阶段 × 动量档(细化定位)
    print("\n=== F. 加速期 子阶段(前/中/后) × 动量分档 ===")
    acc = df[df["stage"] == "accelerate"]
    for sub_key in ("early", "mid", "late"):
        sub = acc[acc["stage_sub"] == sub_key]
        if not len(sub):
            continue
        print(f"加速{ {'early': '前期', 'mid': '中期', 'late': '后期'}[sub_key] }  合计 {_agg(sub)}")
        for band in ("低<20", "中20-28", "高28+"):
            g = sub[sub["mom"].map(mom_band) == band]
            if len(g):
                print(f"    动量{band}: {_agg(g)}")


if __name__ == "__main__":
    main()
