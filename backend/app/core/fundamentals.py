"""基本面因子 + 业绩事件催化: 把纯价格动量升级为「动量 + 质量 + 事件」.

痛点: 纯三因子(趋势/动量/量能)只看价格, 会选出"涨得好但基本面烂"的票 ——
业绩爆雷、高负债、连年亏损的强势股, 一旦证伪就是断崖.

解法(数据源 baostock, 全部走后台刷新落库, 选股时零额外网络调用):
1. 质量筛/加分: ROE、净利同比、资产负债率、PE、ST 标记
2. 事件加权: 近 N 天业绩预告/快报, 预增超阈值加分, 预减/预亏减分

设计原则:
- 数据缺失不惩罚(require_data=False): 基本面是"增强"不是"门槛", 缺数据放行
- filter / score / both 三种模式: 保守用户只加分不过滤

纯函数(可单测, 不依赖网络/DB):
- evaluate_quality(fund, cfg)
- evaluate_events(events, cfg)
- apply_fundamental_factors(results, fund_map, event_map, cfg)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.core.datasource import data_source_manager
from app.models.models import EarningsEvent, StockFundamental

logger = logging.getLogger(__name__)

# 业绩预告类型 -> 方向. baostock profitForcastType 常见取值
POSITIVE_TYPES = ("预增", "略增", "扭亏", "续盈", "增亏减少")
NEGATIVE_TYPES = ("预减", "略减", "首亏", "预亏", "续亏", "增亏")


# ================================================================ 纯函数
def evaluate_quality(fund: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """质量评估. fund 为 StockFundamental 或含同名属性的对象; None 表示无数据.

    返回 {passed, score, tags, risks, has_data}
      passed  是否通过硬过滤(mode 含 filter 时才会被用来剔除)
      score   质量加分 0 ~ bonus_max
      tags    人话标签(good/warn/bad)
      risks   风险提示
    """
    bonus_max = float(cfg.get("bonus_max", 10.0))
    require_data = bool(cfg.get("require_data", False))

    if fund is None:
        return {
            "passed": not require_data,
            "score": 0.0,
            "tags": [],
            "risks": ["无基本面数据(未刷新或非沪深A股)"] if require_data else [],
            "has_data": False,
        }

    tags: list[dict[str, str]] = []
    risks: list[str] = []
    passed = True

    roe = _g(fund, "roe")
    yoy_ni = _g(fund, "yoy_ni")
    lta = _g(fund, "liability_to_asset")
    pe = _g(fund, "pe_ttm")
    gp = _g(fund, "gp_margin")
    is_st = getattr(fund, "is_st", None)

    # ---- 硬过滤条件
    if cfg.get("exclude_st") and is_st:
        passed = False
        risks.append("Baostock 标记为 ST")
        tags.append({"text": "ST", "kind": "bad"})

    min_roe = float(cfg.get("min_roe", 0.0))
    if roe is not None and roe < min_roe:
        passed = False
        risks.append(f"ROE {roe:.1f}% 低于下限 {min_roe:.0f}%")

    max_lta = float(cfg.get("max_liability_to_asset", 100.0))
    if lta is not None and lta > max_lta:
        passed = False
        risks.append(f"资产负债率 {lta:.0f}% 高于上限 {max_lta:.0f}%")

    min_yoy = float(cfg.get("min_yoy_ni", -999.0))
    if yoy_ni is not None and yoy_ni < min_yoy:
        passed = False
        risks.append(f"净利同比 {yoy_ni:.0f}% 低于下限 {min_yoy:.0f}%")

    if pe is not None:
        if cfg.get("exclude_negative_pe") and pe <= 0:
            passed = False
            risks.append("PE(TTM) 为负, 处于亏损状态")
            tags.append({"text": "亏损", "kind": "bad"})
        max_pe = float(cfg.get("max_pe_ttm", 0.0))
        if max_pe > 0 and pe > max_pe:
            passed = False
            risks.append(f"PE(TTM) {pe:.0f} 高于上限 {max_pe:.0f}")

    # ---- 质量加分(各项 0~1 后加权到 bonus_max)
    parts: list[float] = []
    if roe is not None:
        parts.append(_clip01(roe / 20.0))          # ROE 20% 给满
        if roe >= 15:
            tags.append({"text": f"ROE{roe:.0f}%", "kind": "good"})
    if yoy_ni is not None:
        parts.append(_clip01((yoy_ni + 20) / 70))  # -20%~50% 线性
        if yoy_ni >= 30:
            tags.append({"text": f"净利+{yoy_ni:.0f}%", "kind": "good"})
        elif yoy_ni < 0:
            tags.append({"text": f"净利{yoy_ni:.0f}%", "kind": "warn"})
            risks.append(f"净利润同比 {yoy_ni:.0f}%, 业绩在下滑")
    if lta is not None:
        parts.append(_clip01((80 - lta) / 50))     # 负债率越低越好
        if lta >= 70:
            tags.append({"text": f"负债{lta:.0f}%", "kind": "warn"})
    if gp is not None:
        parts.append(_clip01(gp / 50.0))           # 毛利率 50% 给满

    score = round(bonus_max * (sum(parts) / len(parts)), 2) if parts else 0.0
    return {"passed": passed, "score": score, "tags": tags, "risks": risks, "has_data": True}


def evaluate_events(events: list[Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """业绩事件评估. events 为该股近 N 天的事件列表(可为空).

    返回 {delta, tags, notes}. delta 为总分增减(正=利好, 负=利空).
    """
    if not events:
        return {"delta": 0.0, "tags": [], "notes": []}

    bonus = float(cfg.get("bonus", 5.0))
    penalty = float(cfg.get("penalty", -5.0))
    min_chg = float(cfg.get("min_chg_pct", 30.0))

    delta = 0.0
    tags: list[dict[str, str]] = []
    notes: list[str] = []
    # 只看最近一条(同一报告期多次修正时以最新为准)
    latest = max(events, key=lambda e: str(getattr(e, "pub_date", "") or ""))
    ftype = str(getattr(latest, "forecast_type", "") or "")
    up = _g(latest, "chg_pct_up")
    down = _g(latest, "chg_pct_down")
    mid = None
    if up is not None and down is not None:
        mid = (up + down) / 2
    elif up is not None:
        mid = up
    elif down is not None:
        mid = down

    is_pos = any(t in ftype for t in POSITIVE_TYPES) or (mid is not None and mid >= min_chg)
    is_neg = any(t in ftype for t in NEGATIVE_TYPES) or (mid is not None and mid <= -min_chg)

    if is_neg:
        delta = penalty
        tags.append({"text": f"业绩{ftype or '下滑'}", "kind": "bad"})
        notes.append(f"近期业绩{ftype or '预警'}" + (f"({mid:.0f}%)" if mid is not None else ""))
    elif is_pos:
        # 超预期幅度越大加分越多, 上限为 bonus
        ratio = _clip01((mid or min_chg) / max(min_chg * 3, 1)) if mid is not None else 0.5
        delta = round(bonus * max(0.4, ratio), 2)
        tags.append({"text": f"业绩{ftype or '超预期'}" + (f"+{mid:.0f}%" if mid is not None else ""), "kind": "good"})
        notes.append(f"近期业绩{ftype or '预增'}" + (f", 幅度约 {mid:.0f}%" if mid is not None else ""))
    return {"delta": delta, "tags": tags, "notes": notes}


def apply_fundamental_factors(
    results: list[dict[str, Any]],
    fund_map: dict[str, Any],
    event_map: dict[str, list[Any]],
    quality_cfg: dict[str, Any],
    event_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把质量分与事件分叠加到扫描结果上, 并按新总分重排.

    - 保留 base_total(原三因子总分), total 改为叠加后的最终分(前端排序/展示沿用 total)
    - mode=filter 时质量不达标直接剔除; mode=score 时只加分不剔除
    - 返回 (新结果列表, 统计信息)
    """
    q_enabled = bool(quality_cfg.get("enabled"))
    e_enabled = bool(event_cfg.get("enabled"))
    if not q_enabled and not e_enabled:
        return results, {"enabled": False}

    mode = str(quality_cfg.get("mode", "both"))
    do_filter = q_enabled and mode in ("filter", "both")
    do_score = q_enabled and mode in ("score", "both")

    out: list[dict[str, Any]] = []
    removed: list[dict[str, str]] = []
    no_data = 0
    event_hits = 0

    for r in results:
        sym = r.get("symbol", "")
        base = float(r.get("total", 0.0))
        r["base_total"] = base
        delta = 0.0

        if q_enabled:
            q = evaluate_quality(fund_map.get(sym), quality_cfg)
            if not q["has_data"]:
                no_data += 1
            if do_filter and not q["passed"]:
                removed.append({"symbol": sym, "name": r.get("name", ""),
                                "reason": "；".join(q["risks"]) or "基本面不达标"})
                continue
            if do_score:
                delta += q["score"]
            r["quality_score"] = q["score"]
            r["quality_tags"] = q["tags"]
            if q["risks"]:
                r["risk"] = "；".join(filter(None, [r.get("risk", ""), *q["risks"]]))
            r.setdefault("tags", []).extend(q["tags"])

        if e_enabled:
            ev = evaluate_events(event_map.get(sym, []), event_cfg)
            if ev["delta"]:
                event_hits += 1
                delta += ev["delta"]
                r["event_score"] = ev["delta"]
                r.setdefault("tags", []).extend(ev["tags"])
                if ev["notes"]:
                    r["reason"] = "；".join([r.get("reason", ""), *ev["notes"]])

        r["factor_delta"] = round(delta, 2)
        r["total"] = round(base + delta, 1)
        r["attention"] = _attention(r["total"])
        out.append(r)

    out.sort(key=lambda x: x["total"], reverse=True)
    stats = {
        "enabled": True,
        "quality_enabled": q_enabled,
        "quality_mode": mode,
        "event_enabled": e_enabled,
        "before": len(results),
        "after": len(out),
        "removed": len(removed),
        "removed_samples": removed[:10],
        "no_fundamental_data": no_data,
        "event_hits": event_hits,
    }
    return out, stats


def _attention(total: float) -> str:
    if total >= 70:
        return "强烈关注"
    if total >= 60:
        return "重点观察"
    if total >= 50:
        return "一般关注"
    return "观察"


def _clip01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _g(obj: Any, key: str) -> float | None:
    """安全取数值属性, 缺失/非数返回 None."""
    v = getattr(obj, key, None)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ================================================================ DB 读取
def load_fundamentals_map(symbols: list[str]) -> dict[str, StockFundamental]:
    """批量读取基本面(仅取需要的 symbol)."""
    if not symbols:
        return {}
    out: dict[str, StockFundamental] = {}
    with db.session_scope() as s:
        for chunk in _chunks(symbols, 500):
            rows = s.exec(select(StockFundamental).where(StockFundamental.symbol.in_(chunk))).all()
            for r in rows:
                out[r.symbol] = r
    return out


def load_recent_events(symbols: list[str], days: int = 90) -> dict[str, list[EarningsEvent]]:
    """批量读取近 N 天业绩事件."""
    if not symbols:
        return {}
    since = (dt.date.today() - dt.timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    out: dict[str, list[EarningsEvent]] = {}
    with db.session_scope() as s:
        for chunk in _chunks(symbols, 500):
            rows = s.exec(
                select(EarningsEvent)
                .where(EarningsEvent.symbol.in_(chunk))
                .where(EarningsEvent.pub_date >= since)
            ).all()
            for r in rows:
                out.setdefault(r.symbol, []).append(r)
    return out


def fundamentals_stats() -> dict[str, Any]:
    """基本面/事件表覆盖概况."""
    with db.session_scope() as s:
        funds = s.exec(select(StockFundamental)).all()
        events = s.exec(select(EarningsEvent)).all()
    latest_stat = max((f.stat_date for f in funds if f.stat_date), default="")
    latest_upd = max((f.updated_at for f in funds if f.updated_at), default="")
    return {
        "fundamentals": len(funds),
        "with_roe": sum(1 for f in funds if f.roe is not None),
        "with_pe": sum(1 for f in funds if f.pe_ttm is not None),
        "with_industry": sum(1 for f in funds if f.industry),
        "latest_stat_date": latest_stat,
        "updated_at": latest_upd,
        "events": len(events),
        "event_symbols": len({e.symbol for e in events}),
    }


def _chunks(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ================================================================ 后台刷新
async def refresh_fundamentals(symbols: list[str], *, full: bool = False,
                               progress_cb: Any = None) -> dict[str, Any]:
    """批量拉取基本面并落库.

    baostock 单连接串行, 每只票 4~6 次查询, 300 只约需数分钟 —— 因此设计为
    后台任务, 扫描时只读表. 单只失败跳过, 不影响整体.
    """
    from app.models.models import _now

    if not symbols:
        return {"total": 0, "ok": 0, "failed": 0}

    # 行业映射一次拿全量(证监会行业分类), 避免每只票单独查
    try:
        industry_map = await data_source_manager.get_industry_map(symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("行业映射拉取失败, 基本面将缺少行业字段: %s", exc)
        industry_map = {}

    ok = 0
    failed = 0
    total = len(symbols)
    buffer: list[StockFundamental] = []

    for i, sym in enumerate(symbols):
        if progress_cb and (i % 10 == 0 or i == total - 1):
            progress_cb(f"基本面 {i + 1}/{total}", (i + 1) / total)
        try:
            fund = await data_source_manager.get_fundamentals(sym, full=full)
        except Exception as exc:  # noqa: BLE001
            logger.debug("基本面拉取失败 %s: %s", sym, exc)
            fund = None
        if fund is None:
            failed += 1
            continue
        info = industry_map.get(sym, {})
        buffer.append(StockFundamental(
            symbol=sym,
            name=info.get("name", ""),
            stat_date=fund.stat_date,
            pub_date=fund.pub_date,
            roe=fund.roe, np_margin=fund.np_margin, gp_margin=fund.gp_margin,
            eps_ttm=fund.eps_ttm, yoy_ni=fund.yoy_ni, yoy_eps=fund.yoy_eps,
            yoy_equity=fund.yoy_equity, liability_to_asset=fund.liability_to_asset,
            current_ratio=fund.current_ratio, cfo_to_np=fund.cfo_to_np,
            dupont_roe=fund.dupont_roe, pe_ttm=fund.pe_ttm, pb_mrq=fund.pb_mrq,
            ps_ttm=fund.ps_ttm, is_st=fund.is_st,
            industry=info.get("industry", ""),
            industry_source=info.get("classification", ""),
            source="baostock", updated_at=_now(),
        ))
        ok += 1
        if len(buffer) >= 50:
            _upsert_fundamentals(buffer)
            buffer = []
        await asyncio.sleep(0)  # 让出事件循环, 避免阻塞 API

    if buffer:
        _upsert_fundamentals(buffer)
    stats = {"total": total, "ok": ok, "failed": failed, "industry_covered": len(industry_map)}
    logger.info("基本面刷新完成: %s", stats)
    return stats


async def refresh_earnings_events(symbols: list[str], days: int = 180,
                                  progress_cb: Any = None) -> dict[str, Any]:
    """批量拉取近 N 天业绩预告/快报并落库(全量替换该股区间内记录)."""
    from app.models.models import _now

    if not symbols:
        return {"total": 0, "events": 0}

    end = dt.date.today()
    start = end - dt.timedelta(days=max(7, days))
    s_str, e_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    total = len(symbols)
    n_events = 0
    hit_symbols = 0
    for i, sym in enumerate(symbols):
        if progress_cb and (i % 10 == 0 or i == total - 1):
            progress_cb(f"业绩事件 {i + 1}/{total}", (i + 1) / total)
        try:
            items = await data_source_manager.get_earnings_events(sym, s_str, e_str)
        except Exception as exc:  # noqa: BLE001
            logger.debug("业绩事件拉取失败 %s: %s", sym, exc)
            continue
        if not items:
            continue
        hit_symbols += 1
        now = _now()
        with db.session_scope() as s:
            for old in s.exec(
                select(EarningsEvent)
                .where(EarningsEvent.symbol == sym)
                .where(EarningsEvent.pub_date >= s_str)
            ).all():
                s.delete(old)
            s.flush()
            for it in items:
                s.add(EarningsEvent(
                    symbol=sym, kind=it.kind, pub_date=it.pub_date, stat_date=it.stat_date,
                    forecast_type=it.forecast_type, chg_pct_up=it.chg_pct_up,
                    chg_pct_down=it.chg_pct_down, abstract=it.abstract, updated_at=now,
                ))
                n_events += 1
            s.commit()
        await asyncio.sleep(0)

    stats = {"total": total, "events": n_events, "symbols_with_events": hit_symbols,
             "range": f"{s_str}~{e_str}"}
    logger.info("业绩事件刷新完成: %s", stats)
    return stats


def _upsert_fundamentals(rows: list[StockFundamental]) -> None:
    with db.session_scope() as s:
        for r in rows:
            existing = s.get(StockFundamental, r.symbol)
            if existing is None:
                s.add(r)
                continue
            for field in r.model_fields:
                if field == "symbol":
                    continue
                val = getattr(r, field)
                # name/industry 为空时不覆盖已有值
                if field in ("name", "industry", "industry_source") and not val:
                    continue
                setattr(existing, field, val)
        s.commit()
