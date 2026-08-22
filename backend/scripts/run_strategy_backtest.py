"""策略回测验证脚本: SignalEngine 全流程(建仓/加仓/减仓/止损/做T)真实交易循环.

股票池: 本地缓存随机抽样(固定种子, 可复现) —— 全市场逐日×盘中24段评估过慢,
抽样 120 只已足以评估信号质量与风控效果。

输出: 总体表现 + 按信号类型的盈亏拆解(看每类信号是否真的增加价值)。

用法: python scripts/run_strategy_backtest.py [n_samples]
"""
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.backtest.strategy import run_strategy_backtest  # noqa: E402
from app.core.datasource import kline_store  # noqa: E402

ACTION_CN = {
    "buy_first": "首仓", "buy_add": "加仓", "sell_reduce": "减仓",
    "sell_stop": "止损", "t_buy": "T买回", "t_sell": "T卖出",
}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    all_syms = kline_store.list_symbols(period="daily")
    rng = random.Random(42)  # 固定种子, 可复现
    symbols = sorted(rng.sample(all_syms, min(n, len(all_syms))))
    print(f"股票池: {len(symbols)}/{len(all_syms)} 只(随机抽样, seed=42)\n")

    report = run_strategy_backtest(symbols=symbols, initial_capital=1_000_000.0)

    if "error" in report:
        print("ERROR:", report["error"])
        return

    print("=== 总体表现 ===")
    print(json.dumps(report["meta"], ensure_ascii=False, indent=2))
    print("\n=== 交易统计 ===")
    print(json.dumps(report["stats"], ensure_ascii=False, indent=2))

    # ---- 按信号类型拆解
    trades = report["trades"]
    by_action: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0, "closed": 0})
    for t in trades:
        a = t["action"]
        by_action[a]["n"] += 1
        if t["pnl"] != 0 or a in ("sell_reduce", "sell_stop", "t_sell"):
            by_action[a]["closed"] += 1
            by_action[a]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                by_action[a]["wins"] += 1
    print("\n=== 按信号类型拆解 ===")
    print(f"{'类型':<6}{'笔数':>6}{'平仓笔':>8}{'胜率':>8}{'实现盈亏':>12}{'平均/笔':>10}")
    for a in ("buy_first", "buy_add", "sell_reduce", "sell_stop", "t_buy", "t_sell"):
        d = by_action.get(a)
        if not d or d["n"] == 0:
            print(f"{ACTION_CN[a]:<6}{0:>6}")
            continue
        wr = d["wins"] / d["closed"] * 100 if d["closed"] else 0.0
        avg = d["pnl"] / d["closed"] if d["closed"] else 0.0
        print(f"{ACTION_CN[a]:<6}{d['n']:>6}{d['closed']:>8}{wr:>7.1f}%{d['pnl']:>12.0f}{avg:>10.0f}")

    # ---- 最大单笔盈亏 & 亏损Top5
    sells = [t for t in trades if t["action"] in ("sell_reduce", "sell_stop", "t_sell")]
    if sells:
        sells.sort(key=lambda t: t["pnl"])
        print("\n=== 最差5笔平仓 ===")
        for t in sells[:5]:
            print(f"  {t['date']} {t['symbol']} {ACTION_CN.get(t['action'], t['action'])} "
                  f"pnl={t['pnl']:.0f} | {t['reason'][:40]}")
        print("=== 最好5笔平仓 ===")
        for t in sells[-5:][::-1]:
            print(f"  {t['date']} {t['symbol']} {ACTION_CN.get(t['action'], t['action'])} "
                  f"pnl={t['pnl']:.0f} | {t['reason'][:40]}")

    # ---- 净值曲线关键节点(每 ~1/6 区间)
    eq = report["equity_curve"]
    if eq:
        step = max(1, len(eq) // 6)
        print("\n=== 净值曲线(抽样) ===")
        for pt in eq[::step]:
            print(f"  {pt['date']}  净值 {pt['equity']:,.0f}")


if __name__ == "__main__":
    main()
