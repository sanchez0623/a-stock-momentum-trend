"""Baostock 集成冒烟测试: 验证新能力 + 因子叠加接线正确(不跑全市场扫描).

分层:
  1. 纯函数(无网络/无DB): parse_universe / apply_universe / evaluate_quality /
     evaluate_events / apply_fundamental_factors
  2. 实时 Baostock 能力: 成分股 / 行业映射 / 单票基本面 / 业绩事件 / 指数日线
  3. 接线: data_source_manager 配置自愈 + 能力路由 + screener.scan(小池, apply_factors=True)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile

# 用临时 DB, 避免污染真实库
TMP = tempfile.mkdtemp(prefix="bs_test_")
os.environ["DATA_DIR"] = TMP

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("BS_TEST")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    log.info("%s %s %s", "✅" if ok else "❌", name, extra)


# ---------------------------------------------------------------- 1. 纯函数
def test_pure() -> None:
    from app.core.universe import parse_universe, apply_universe
    from app.core.fundamentals import (
        apply_fundamental_factors,
        evaluate_events,
        evaluate_quality,
    )

    # universe 解析
    check("parse_universe zz800", parse_universe("zz800") == ["hs300", "zz500"], str(parse_universe("zz800")))
    check("parse_universe unknown->all", parse_universe("nope") == [], str(parse_universe("nope")))

    pool = [("600000", "浦发银行", ""), ("000001", "平安银行", ""), ("300750", "宁德", "")]
    allowed = {"600000", "000001"}
    out = apply_universe(pool, allowed)
    check("apply_universe 预筛", [s for s, _, _ in out] == ["600000", "000001"], str(out))

    # 质量评估
    class F:
        roe = 15.0
        yoy_ni = 30.0
        liability_to_asset = 50.0
        gp_margin = 40.0
        pe_ttm = 25.0
        is_st = False

    q = evaluate_quality(F(), {"bonus_max": 10, "min_roe": 5, "max_liability_to_asset": 70,
                               "min_yoy_ni": -20, "max_pe_ttm": 100, "exclude_negative_pe": True,
                               "exclude_st": True, "require_data": False, "mode": "both"})
    check("evaluate_quality 通过+加分", q["passed"] and q["score"] > 0, f"score={q['score']}")

    class Bad:
        roe = 1.0
        yoy_ni = -50.0
        liability_to_asset = 90.0
        gp_margin = None
        pe_ttm = -3.0
        is_st = True

    qb = evaluate_quality(Bad(), {"bonus_max": 10, "min_roe": 5, "max_liability_to_asset": 70,
                                  "min_yoy_ni": -20, "max_pe_ttm": 100, "exclude_negative_pe": True,
                                  "exclude_st": True, "require_data": False, "mode": "both"})
    check("evaluate_quality 垃圾被否", not qb["passed"], f"risks={qb['risks']}")

    # 事件评估
    class Ev:
        pub_date = "2026-07-01"
        forecast_type = "预增"
        chg_pct_up = 80.0
        chg_pct_down = 50.0

    ev = evaluate_events([Ev()], {"bonus": 5, "penalty": -5, "min_chg_pct": 30})
    check("evaluate_events 预增加分", ev["delta"] > 0, f"delta={ev['delta']}")

    # 因子叠加
    results = [{"symbol": "600000", "name": "浦发", "total": 60.0, "attention": "重点观察",
                "tags": [], "reason": "x", "risk": ""}]
    fund_map = {"600000": F()}
    new, stats = apply_fundamental_factors(
        results, fund_map, {},
        {"enabled": True, "mode": "both", "bonus_max": 10, "min_roe": 5, "max_liability_to_asset": 70,
         "min_yoy_ni": -20, "max_pe_ttm": 100, "exclude_negative_pe": True, "exclude_st": True,
         "require_data": False},
        {"enabled": False},
    )
    check("apply_fundamental_factors 加分", new[0]["total"] >= 60.0 and "base_total" in new[0],
          f"total={new[0]['total']} base={new[0].get('base_total')}")


# ---------------------------------------------------------------- 2. 实时 Baostock
async def test_baostock_live() -> None:
    from app.core.datasource import build_sources, data_source_manager

    data_source_manager.setup(build_sources())
    bs = data_source_manager._sources.get("baostock")
    if bs is None:
        check("baostock 已注册", False, "未启用或未安装")
        return
    check("baostock 已注册", True, f"supports={bs.supports}")

    # 成分股
    cons = await data_source_manager.get_index_constituents("hs300")
    check("get_index_constituents(hs300)", len(cons) > 50, f"{len(cons)} 只")

    # 行业映射(证监会行业)
    imap = await data_source_manager.get_industry_map(["600000", "000001", "300750"])
    check("get_industry_map", len(imap) > 0, f"{len(imap)} 只, 样本={list(imap)[:2]}")

    # 基本面
    fund = await data_source_manager.get_fundamentals("600000")
    check("get_fundamentals 600000", fund is not None and (fund.roe is not None or fund.pe_ttm is not None),
          f"roe={fund.roe if fund else None} pe={fund.pe_ttm if fund else None}")

    # 业绩事件(回看 180 天)
    evs = await data_source_manager.get_earnings_events("600000", "2026-02-01", "2026-08-08")
    check("get_earnings_events", isinstance(evs, list), f"{len(evs)} 条")

    # 指数日线(hs300 sh.000300)
    df = await data_source_manager.get_index_kline("0.000300", "daily", 30)
    check("get_index_kline(沪深300)", df is not None and not df.empty, f"{len(df)} 根" if df is not None else "None")


# ---------------------------------------------------------------- 3. 接线(小池扫描)
async def test_screener_wiring() -> None:
    from app.core.screener import screener

    # 仅 2 只票, 验证 scan 接受 universe / apply_factors 且不崩
    try:
        res = await screener.scan(symbols=["600000", "000001"], top_n=10,
                                  apply_gate=False, universe=None, apply_factors=True,
                                  per_industry=0)
        ok = isinstance(res, list)
        info = ""
        if screener.last_scan_summary:
            fs = screener.last_scan_summary.get("factor", {})
            info = f"factor.applied={fs.get('applied')} universe={screener.last_scan_summary.get('universe',{}).get('universe')}"
        check("screener.scan 小池+因子", ok, info)
    except Exception as exc:  # noqa: BLE001
        check("screener.scan 小池+因子", False, f"异常: {exc}")


async def main() -> None:
    test_pure()
    log.info("=== 纯函数层通过 %d / 失败 %d ===", len(PASS), len(FAIL))
    await test_baostock_live()
    await test_screener_wiring()
    log.info("\n================ 总结 ================")
    log.info("PASS(%d): %s", len(PASS), PASS)
    if FAIL:
        log.error("FAIL(%d): %s", len(FAIL), FAIL)
        sys.exit(1)
    log.info("全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
