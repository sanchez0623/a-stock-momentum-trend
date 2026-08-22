"""总分分桶回测验证脚本: 用本地 K 线缓存跑一次, 打印 阶段/总分 双分桶结果.

用法: python scripts/run_score_bucket_backtest.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.backtest.factor import backtest_factors  # noqa: E402


def _row(info: dict, holds: tuple[str, ...]) -> str:
    parts = []
    for h in holds:
        s = info["holds"].get(h)
        if not s or s["n"] == 0:
            parts.append(f"{h}: -")
        else:
            parts.append(f"{h}: n={s['n']} 胜率{s['win_rate']}% 期望{s['avg']}% 中位{s['median']}%")
    return " | ".join(parts)


def main() -> None:
    report = backtest_factors(hold_days=(5, 10, 20), cost=True)
    print("=== META ===")
    print(json.dumps(report["meta"], ensure_ascii=False, indent=2))

    print("\n=== 阶段分布 ===")
    print(json.dumps(report["stage_distribution"], ensure_ascii=False))

    print("\n=== 按趋势阶段分桶 ===")
    for stage, info in report["by_stage"].items():
        print(f"[{info['label']}] {_row(info, ('hold_5', 'hold_10', 'hold_20'))}")

    print("\n=== 分数分布 ===")
    print(json.dumps(report["score_distribution"], ensure_ascii=False))

    print("\n=== 按选股总分分桶(核心: 期望是否随分数单调递增) ===")
    for key, info in report["by_score"].items():
        print(f"[{info['label']}] {_row(info, ('hold_5', 'hold_10', 'hold_20'))}")

    # 单调性快评: 相邻桶 20 日期望是否递增
    keys = list(report["by_score"].keys())
    if len(keys) >= 2:
        print("\n=== 20日期望单调性 ===")
        avgs = [(k, report["by_score"][k]["holds"]["hold_20"]["avg"]) for k in keys]
        print(" -> ".join(f"{k}:{a}%" for k, a in avgs))
        mono = all(avgs[i][1] <= avgs[i + 1][1] for i in range(len(avgs) - 1))
        print(f"单调递增: {'是' if mono else '否'}")


if __name__ == "__main__":
    main()
