"""策略回测修复效果对比(历史留档 + 当前引擎验证).

同一股票池(随机抽样 seed=42), 修复路径与各轮结果:
- 基线(修复前): 硬防守(触发后永久禁开仓) + 无冷却 + 旧止损 —— -8.56%
- 变体B: 软防守+恢复 + 冷却10日 + 旧止损(固定线无条件触发) —— +17.54% (当前最优)
- 变体C(已回退): 结构优先止损("结构未坏让趋势呼吸到硬线8%") —— -26.76%, 净伤害.
  混合验证(结构离场保留+静态线无条件)与 B 逐项一致, 证明:
  ① 恶化 100% 来自"结构未坏时软线放宽到-8%"(单笔亏损放大60%, 止损笔数几乎未减);
  ② 结构离场信号(MA短穿中+ADX掉头)零增量——确认时价格几乎总已跌破移动止损线.
  最终采纳: 保留结构离场信号 + 亏损区恢复无条件静态线, 删除硬线.
- 当前引擎: 结构离场(浮盈浮亏都走) + 静态线无条件 + 移动止损 + 软防守 + 冷却10日
  —— 本脚本实时跑一遍, 验证与变体B完全一致(回退无副作用).

用法: python scripts/run_strategy_backtest_compare.py [n_samples]
"""
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.backtest.strategy import StrategyBacktest  # noqa: E402
from app.core.datasource import kline_store  # noqa: E402

ACTION_CN = {
    "buy_first": "首仓", "buy_add": "加仓", "sell_reduce": "减仓",
    "sell_stop": "止损", "t_buy": "T买回", "t_sell": "T卖出",
}

# 基线(2026-08-22 第四轮, 同池同参数): 硬防守 + 无冷却 + 旧止损
BASELINE = {
    "total": -8.56, "max_dd": 11.8, "sharpe": -0.94, "win": 54.3,
    "pf": 0.54, "exp": -2261, "trades": 143, "stops": (-186_363, 24),
    "reduce": 82_366, "t": 19_098,
}

# 变体B(2026-08-22 第五轮): 软防守+恢复 + 冷却10日, 旧止损(固定线无条件触发)
VARIANT_B = {
    "total": 17.54, "max_dd": 33.99, "sharpe": 0.57, "win": 60.6,
    "pf": 0.54, "exp": -1485, "trades": 2205, "stops": (-1_842_462, 309),
    "reduce": 857_621, "t": 954_731,
}

# 变体C(2026-08-22 第六轮, 已回退): 结构优先(软线让趋势呼吸+硬线8%)
VARIANT_C = {
    "total": -26.76, "max_dd": 45.03, "sharpe": -0.75, "win": 61.3,
    "pf": 0.41, "exp": -1754, "trades": 2246, "stops": (-1_609_524, 291),
    "reduce": 535_874, "t": 777_015,
}

# 消融(同轮): 结构止损但冷却关闭 —— 冷却闸门贡献 +29.5pp
VARIANT_A_STRUCT_NO_COOLDOWN = {
    "total": -56.24, "max_dd": 60.07, "sharpe": -2.15, "win": 56.5,
    "pf": 0.45, "exp": -1267, "trades": 2031, "stops": (-1_252_068, 365),
    "reduce": 389_569, "t": 323_948,
}


def breakdown(trades: list[dict]) -> dict[str, dict]:
    by = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        by[t["action"]]["n"] += 1
        by[t["action"]]["pnl"] += t["pnl"]
    return by


def run_current(symbols: list[str]) -> dict:
    bt = StrategyBacktest(initial_capital=1_000_000.0)
    bt.stop_cooldown_days = 10
    r = bt.run(symbols=symbols)
    if "error" in r:
        print("当前引擎 ERROR:", r["error"])
        sys.exit(1)
    m, s = r["meta"], r["stats"]
    by = breakdown(r["trades"])
    print("\n=== 当前引擎: 结构离场+静态线无条件+移动止损+软防守+冷却10日 ===")
    print(f"总收益 {m['total_return_pct']:+.2f}%  最大回撤 {m['max_drawdown_pct']}%  "
          f"Sharpe {m['sharpe']}  胜率 {s['win_rate']}%  PF {s['profit_factor']}  "
          f"单笔期望 {s['expectancy']:.0f}")
    print(f"交易 {s['trades']} 笔  冷却拦截 {s['cooldown_blocks']} 次(symbol-日)  "
          f"期末防守状态 {'开' if s['defense_mode'] else '关'}")
    line = "  ".join(
        f"{ACTION_CN[a]} {by[a]['n']}笔 {by[a]['pnl']:+,.0f}"
        for a in ("buy_first", "buy_add", "sell_reduce", "sell_stop", "t_buy", "t_sell") if by[a]["n"]
    )
    print(f"  {line}")
    return {
        "total": m["total_return_pct"], "max_dd": m["max_drawdown_pct"], "sharpe": m["sharpe"],
        "win": s["win_rate"], "pf": s["profit_factor"], "exp": s["expectancy"],
        "trades": s["trades"], "stops": (by["sell_stop"]["pnl"], by["sell_stop"]["n"]),
        "reduce": by["sell_reduce"]["pnl"], "t": by["t_sell"]["pnl"],
        "blocks": s["cooldown_blocks"],
    }


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    all_syms = kline_store.list_symbols(period="daily")
    rng = random.Random(42)
    symbols = sorted(rng.sample(all_syms, min(n, len(all_syms))))
    print(f"股票池: {len(symbols)}/{len(all_syms)} 只(随机抽样, seed=42, 与基线一致)\n")

    cur = run_current(symbols)

    # ---- 验证: 当前引擎应精确复现变体B(回退无副作用)
    print("\n" + "=" * 56)
    print(f"{'指标':<14}{'变体B(历史)':>14}{'当前引擎(实时)':>16}  一致?")
    print("-" * 56)
    checks = [
        ("总收益%", f"{VARIANT_B['total']:+.2f}", f"{cur['total']:+.2f}", abs(VARIANT_B['total'] - cur['total']) < 0.01),
        ("最大回撤%", f"{VARIANT_B['max_dd']}", f"{cur['max_dd']}", abs(VARIANT_B['max_dd'] - cur['max_dd']) < 0.01),
        ("Sharpe", f"{VARIANT_B['sharpe']}", f"{cur['sharpe']}", VARIANT_B['sharpe'] == cur['sharpe']),
        ("止损", f"{VARIANT_B['stops'][0]:+,.0f}({VARIANT_B['stops'][1]})", f"{cur['stops'][0]:+,.0f}({cur['stops'][1]})", VARIANT_B['stops'][1] == cur['stops'][1]),
    ]
    all_ok = True
    for name, v1, v2, ok in checks:
        all_ok &= ok
        print(f"{name:<14}{v1:>14}{v2:>16}  {'✓' if ok else '✗ 不一致!'}")
    print("-" * 56)
    print("回退验证:", "✓ 当前引擎与变体B完全一致" if all_ok else "✗ 与变体B不一致, 需排查")

    # ---- 历史留档: 修复路径全景
    print("\n" + "=" * 56)
    print(f"{'指标':<14}{'基线(修复前)':>14}{'B: 防守+冷却':>14}{'C: 结构优先(弃)':>16}")
    print("-" * 56)
    for name, key, fmt in [
        ("总收益%", "total", "{:+.2f}"), ("最大回撤%", "max_dd", "{}"),
        ("Sharpe", "sharpe", "{}"), ("胜率%", "win", "{}"),
        ("止损笔数", "stops", "{}"),
    ]:
        if key == "stops":
            row = [f"{d[key][1]}" for d in (BASELINE, VARIANT_B, VARIANT_C)]
        else:
            row = [fmt.format(d[key]) for d in (BASELINE, VARIANT_B, VARIANT_C)]
        print(f"{name:<14}{row[0]:>14}{row[1]:>14}{row[2]:>16}")


if __name__ == "__main__":
    main()
