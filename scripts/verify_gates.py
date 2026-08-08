"""验证 ④ 择时闸门 + ⑤ 行业限配 是否接通且效果正确。

特点: 纯合成数据, 不依赖网络 / 数据库 / 真实行情, 任何环境(含沙箱)都能跑。
运行: 在 backend/ 目录下 -> python ../scripts/verify_gates.py

它做的事情, 与真实 scan() 的后处理完全一致:
  1. 构造一批"已按总分降序"的扫描结果;
  2. 用 apply_per_industry_cap 做行业限配(⑤);
  3. 用 compute_market_gate 算闸门乘数(④), 作用到 top_n;
  4. 打印每步前后对比 + 触顶行业明细, 一眼看出"用上了 + 效果"。
"""

from __future__ import annotations

import os
import sys
import tempfile

# 避免触碰真实数据目录(导入 app 会创建 engine)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="verify_gates_"))

import numpy as np
import pandas as pd

# 让脚本能从 backend/ 外运行
_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.core.classification import apply_per_industry_cap
from app.core.market_gate import compute_market_gate


# ---------------------------------------------------------------- 合成数据
class _Cls:
    """StockClassification 替身, 只提供 sw_l1 属性。"""

    def __init__(self, sw_l1: str = ""):
        self.sw_l1 = sw_l1
        self.industry = sw_l1


def _mk_index_df(n: int, trend: str = "up") -> pd.DataFrame:
    """构造指数 K 线(含 close, 行数 n)。trend=up 走牛, down 走熊。"""
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = np.linspace(100, 200, n) if trend == "up" else np.linspace(200, 100, n)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": close, "high": close + 1.0, "low": close - 1.0, "close": close,
        "volume": np.full(n, 1_000_000.0), "amount": np.full(n, 1_000_000.0) * close,
    })


def _build_scan_and_map() -> tuple[list[dict], dict]:
    """构造 30 只已排序结果 + 行业映射。

    行业分布(刻意制造扎堆): 电子 12 / 医药 8 / 食品饮料 6 / 计算机 4。
    每只总分从高到低, 同行业内也降序 -> 限配会砍掉每个行业超出 3 只的部分。
    """
    plan = [("电子", 12), ("医药", 8), ("食品饮料", 6), ("计算机", 4)]
    results: list[dict] = []
    class_map: dict = {}
    score = 100.0
    for ind, cnt in plan:
        for i in range(cnt):
            sym = f"{ind[0]}{i+1:02d}"  # 电01, 医01, ...
            results.append({"symbol": sym, "total": round(score, 1)})
            class_map[sym] = _Cls(sw_l1=ind)
            score -= 0.7
    # 已按 total 降序
    return results, class_map


def _hr(t: str) -> None:
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def main() -> None:
    results, class_map = _build_scan_and_map()
    top_n = 30
    per_industry = 3
    level = "sw_l1"

    _hr("原始扫描结果(已按总分降序)")
    print(f"原始只数: {len(results)}  | top_n 请求: {top_n}")

    # ---------- ⑤ 行业限配 ----------
    _hr("⑤ 行业限配  apply_per_industry_cap(per_industry=3, level=sw_l1)")
    before = results
    capped = apply_per_industry_cap(before, class_map, per_industry, level)

    from collections import Counter

    def _grp(r):
        cls = class_map.get(r["symbol"])
        return (getattr(cls, level, "") or "").strip() or "_未知_"

    before_g = Counter(_grp(r) for r in before)
    after_g = Counter(_grp(r) for r in capped)
    print(f"{'行业':<10}{'限配前':>8}{'限配后':>8}{'移除':>8}")
    print("-" * 36)
    for k in sorted(before_g, key=lambda x: -before_g[x]):
        b, a = before_g[k], after_g.get(k, 0)
        flag = "  <- 触顶" if a < b else ""
        print(f"{k:<10}{b:>8}{a:>8}{b - a:>8}{flag}")
    print(f"\n限配后保留: {len(capped)} 只 (原 {len(before)} 只, 移除 {len(before) - len(capped)} 只)")

    # ---------- ④ 择时闸门 ----------
    _hr("④ 择时闸门  compute_market_gate(不同市场环境下的乘数)")
    scenarios = {
        "牛市(沪深300↑ 创业板指↑)": {"沪深300": _mk_index_df(250, "up"), "创业板指": _mk_index_df(250, "up")},
        "熊市(沪深300↓ 创业板指↓)": {"沪深300": _mk_index_df(250, "down"), "创业板指": _mk_index_df(250, "down")},
        "中性(一上一下)": {"沪深300": _mk_index_df(250, "up"), "创业板指": _mk_index_df(250, "down")},
    }
    cfg = {"ma_long": 200, "ma_mid": 60, "bull_top_n_ratio": 1.0, "bear_top_n_ratio": 0.3}
    print(f"{'场景':<26}{'环境':>8}{'乘数':>8}{'top_n→':>12}")
    print("-" * 56)
    finals = {}
    for name, dfs in scenarios.items():
        g = compute_market_gate(dfs, cfg)
        eff = int(top_n * g["multiplier"])
        final = min(len(capped), eff)
        finals[name] = (g["environment"], g["multiplier"], eff, final)
        print(f"{name:<26}{g['environment']:>8}{g['multiplier']:>8.2f}{f'{top_n}→{eff}':>12}")

    # ---------- 组合效果 ----------
    _hr("组合效果(先限配, 再乘闸门) —— 即 scan() 的最终输出只数")
    print(f"{'场景':<26}{'限配后':>8}{'闸门后top_n':>14}{'最终输出':>10}")
    print("-" * 60)
    for name, (env, mult, eff, final) in finals.items():
        print(f"{name:<26}{len(capped):>8}{eff:>14}{final:>10}")
    print("\n结论: 行业限配把'扎堆'砍散(30→12), 闸门再按市场环境缩放;")
    print("       二者串行作用在 scan() 中, 与真实路径完全一致。")


if __name__ == "__main__":
    main()
