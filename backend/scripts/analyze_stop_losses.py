"""止损明细深挖: 对策略回测的每笔止损做解剖.

对每笔 sell_stop:
1. 止损类型: 固定线(跌破止损线) / 移动线(跌破移动止损线) / 结构性(MA短穿中且ADX掉头)
2. 持仓画像: 入场日->止损日持有天数, 入场时的选股总分(当时的入场质量)
3. 止损后走势(核心): 止损价买入持有 5/10/20 日的收益 ——
   反弹为正 = 止损打在噪音上(错杀), 继续下跌 = 止对了
4. 盈利止损 vs 亏损止损拆分(移动止损在浮盈中触发是好止损)

用法: python scripts/analyze_stop_losses.py [n_samples]
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.backtest.factor import _to_df
from app.core.backtest.strategy import run_strategy_backtest
from app.core.config import config_manager
from app.core.datasource import kline_store
from app.core.indicators import compute_all
from app.core.screener.engine import score_indicators


def classify_stop(reason: str) -> str:
    if "移动止损" in reason:
        return "移动止损线"
    if "MA短穿中" in reason:
        return "结构性" if "跌破" not in reason else "混合(线+结构)"
    if "跌破止损线" in reason:
        return "固定止损线"
    return "其他"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    cfg = config_manager.get()

    all_syms = kline_store.list_symbols(period="daily")
    rng = random.Random(42)
    symbols = sorted(rng.sample(all_syms, min(n, len(all_syms))))

    report = run_strategy_backtest(symbols=symbols, initial_capital=1_000_000.0)
    trades = report["trades"]
    print(f"总交易 {len(trades)} 笔, 其中止损 {sum(1 for t in trades if t['action'] == 'sell_stop')} 笔\n")

    # ---- 预载指标表(入场分用)
    inds: dict[str, pd.DataFrame] = {}
    dis: dict[str, dict[str, int]] = {}
    for sym in symbols:
        df = _to_df(kline_store.load(sym, "daily") or [])
        if df is None or len(df) < 62:
            continue
        ind = compute_all(
            df,
            ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
            macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
            rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
            volume_ma=cfg["量能"]["volume_ma"],
        )
        inds[sym] = ind
        dis[sym] = {str(d)[:10]: i for i, d in enumerate(ind["date"])}

    # ---- 按票重放持仓账本, 找每笔止损对应的入场
    stops: list[dict] = []
    for sym in sorted({t["symbol"] for t in trades if t["action"] == "sell_stop"}):
        tl = [t for t in trades if t["symbol"] == sym]
        entry_date = None
        entry_px = None
        qty = 0
        cost = 0.0
        for t in tl:
            if t["action"] in ("buy_first", "buy_add"):
                if qty == 0:
                    entry_date = t["date"]
                cost = (cost * qty + t["price"] * t["qty"]) / (qty + t["qty"])
                qty += t["qty"]
                entry_px = entry_px or t["price"]
            elif t["action"] in ("sell_reduce", "sell_stop"):
                qty -= t["qty"]
                if t["action"] == "sell_stop" and qty < 0:
                    qty = 0
                if t["action"] == "sell_stop":
                    stops.append({
                        "symbol": sym, "date": t["date"], "price": t["price"],
                        "pnl": t["pnl"], "reason": t["reason"],
                        "entry_date": entry_date, "entry_px": entry_px,
                    })
                    entry_date, entry_px, qty, cost = None, None, 0, 0.0
                if qty == 0:
                    entry_date, entry_px, qty, cost = None, None, 0, 0.0

    # ---- 逐笔补充: 持有天数/入场分/止损后走势
    rows = []
    for s in stops:
        ind, di = inds.get(s["symbol"]), dis.get(s["symbol"])
        if ind is None:
            continue
        i_stop = di.get(s["date"][:10])
        i_entry = di.get(s["entry_date"][:10]) if s["entry_date"] else None
        hold_days = (i_stop - i_entry) if (i_stop is not None and i_entry is not None) else -1
        # 入场时选股总分(当时入场质量)
        entry_score = score_indicators(ind, cfg, end=i_entry + 1, with_reason=False)["total"] \
            if i_entry is not None and i_entry >= 60 else None
        # 止损后走势: 以止损价买入持有 N 日(收盘)
        post = {}
        for hd in (5, 10, 20):
            j = i_stop + hd
            if i_stop is not None and j < len(ind):
                px = float(ind["close"].iloc[j])
                post[hd] = round((px / s["price"] - 1) * 100, 1) if s["price"] > 0 else None
            else:
                post[hd] = None
        rows.append({
            "sym": s["symbol"], "stop_date": s["date"][:10], "type": classify_stop(s["reason"]),
            "pnl": s["pnl"], "ret_pct": round(s["pnl"] / max(s["price"] * 100, 1) * 100, 1),
            "hold": hold_days, "entry_score": entry_score,
            "post5": post[5], "post10": post[10], "post20": post[20],
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("无止损明细")
        return

    # ---- 明细表
    print(f"{'代码':<8}{'止损日':>11}{'类型':<10}{'盈亏':>9}{'持有日':>6}{'入场分':>6}{'后5日':>7}{'后10日':>7}{'后20日':>7}")
    for _, r in df.sort_values("pnl").iterrows():
        es = f"{r['entry_score']:.0f}" if r["entry_score"] is not None else "-"
        print(f"{r['sym']:<8}{r['stop_date']:>11}{r['type']:<10}{r['pnl']:>9.0f}{r['hold']:>6}"
              f"{es:>6}{r['post5']:>6}%{r['post10']:>6}%{r['post20']:>6}%")

    # ---- 按类型汇总
    print("\n=== 按止损类型汇总 ===")
    for tp, g in df.groupby("type"):
        post20 = g["post20"].dropna()
        print(f"[{tp}] n={len(g)}")
        print(f"    盈亏合计 {g['pnl'].sum():>10.0f}   平均 {g['pnl'].mean():>8.0f}"
              f"   盈利单占比 {(g['pnl'] > 0).mean() * 100:.0f}%")
        print(f"    平均持有 {g['hold'].mean():.1f} 日   入场均分 "
              f"{g['entry_score'].dropna().mean():.1f}" if g["entry_score"].notna().any() else "    入场分: 无数据")
        if len(post20):
            print(f"    止损后20日: 均值 {post20.mean():+.1f}%  中位 {post20.median():+.1f}%  反弹(>0)占比 {(post20 > 0).mean() * 100:.0f}%")

    # ---- 错杀 vs 止对(20日口径)
    print("\n=== 止损质量判定(止损价起算, 20日窗口) ===")
    ok = df[df["post20"].notna()]
    if len(ok):
        right = ok[ok["post20"] < 0]   # 止损后继续跌 = 止对
        wrong = ok[ok["post20"] >= 0]  # 反弹 = 错杀
        print(f"止对(后20日仍下跌): {len(right)} 笔, 平均后续 {right['post20'].mean():+.1f}%")
        print(f"错杀(后20日反弹):   {len(wrong)} 笔, 平均后续 {wrong['post20'].mean():+.1f}%")
        # 错杀代价: 这些票如果不止损, 20日能少亏/多赚多少
        missed = (wrong["post20"] / 100 * wrong["pnl"].abs() / 5).sum()  # 粗估: 按止损额比例折算
        print(f"错杀票合计止损亏损 {wrong['pnl'].sum():.0f}, 若持有20日平均可修复 {wrong['post20'].mean():+.1f}%")

    # ---- 入场分与止损的关系
    print("\n=== 入场质量 vs 是否止损(全回测首仓) ===")
    first_buys = [t for t in trades if t["action"] == "buy_first"]
    stopped_syms = set(df["sym"])
    print(f"首仓 {len(first_buys)} 笔, 涉及 {len({t['symbol'] for t in first_buys})} 只, 其中被止损 {len(stopped_syms)} 只")
    scores_stopped, scores_survived = [], []
    for t in first_buys:
        ind, di = inds.get(t["symbol"]), dis.get(t["symbol"])
        if ind is None:
            continue
        i = di.get(t["date"][:10])
        if i is None or i < 60:
            continue
        sc = score_indicators(ind, cfg, end=i + 1, with_reason=False)["total"]
        (scores_stopped if t["symbol"] in stopped_syms else scores_survived).append(sc)
    if scores_stopped and scores_survived:
        print(f"被止损票入场均分: {sum(scores_stopped) / len(scores_stopped):.1f} (n={len(scores_stopped)})")
        print(f"存活票入场均分:   {sum(scores_survived) / len(scores_survived):.1f} (n={len(scores_survived)})")


if __name__ == "__main__":
    main()
