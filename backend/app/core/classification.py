"""个股分类映射: 申万一级/二级/三级 + 行业板块/概念板块.

数据来源(akshare, 用户环境可跑; 沙箱因网络/版本限制可能部分失败, 已做容错):
- 申万分类法: sw_index_first/second/third_info(31/131/335 级, 含父子关系)
- 申万成分股: sw_index_third_cons(每行直接带 股票代码/申万1/2/3级)
          回退: index_component_sw(按指数代码拉成分股)
- 行业板块:   stock_board_industry_name_em + stock_board_industry_cons_em
- 概念板块:   stock_board_concept_name_em  + stock_board_concept_cons_em

存储: StockClassification 表(见 models.py). 支持增量刷新与查询.

纯函数(可单测, 不依赖网络):
- apply_per_industry_cap(results, class_map, per_industry, level)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pandas as pd
from sqlmodel import func, select

from app import db
from app.core.datasource import data_source_manager
from app.models.models import StockClassification

logger = logging.getLogger(__name__)


def _retry(fn, times: int = 3, sleep: float = 1.0):
    """同步 I/O 重试: 乐咕乐股页面偶尔返回坏结构, 重试可恢复."""
    last: Exception | None = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < times - 1:
                time.sleep(sleep)
    raise last


# ---------------------------------------------------------------- 申万分类拉取
def _build_sw_hierarchy() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """构建 申万 三级->二级->一级 的父子映射.

    数据来自 akshare 的 sw_index_third_info / sw_index_second_info(元数据接口, 稳定可用).
    返回 (l3_to_l2, l2_to_l1, l3_name_by_code). 任一步失败仅降级(缺失部分映射), 不整体崩.
    """
    import akshare as ak

    l3_to_l2: dict[str, str] = {}   # 三级名 -> 二级名
    l2_to_l1: dict[str, str] = {}   # 二级名 -> 一级名
    l3_name_by_code: dict[str, str] = {}  # 三级代码 -> 三级名

    try:
        second = _retry(ak.sw_index_second_info)
        for _, r in second.iterrows():
            l2_to_l1[str(r.get("行业名称", ""))] = str(r.get("上级行业", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("申万二级分类法拉取失败: %s", exc)

    try:
        third = _retry(ak.sw_index_third_info)
        for _, r in third.iterrows():
            name = str(r.get("行业名称", ""))
            l2 = str(r.get("上级行业", ""))
            code = str(r.get("行业代码", ""))
            if name:
                l3_to_l2[name] = l2
            if code:
                l3_name_by_code[code] = name
    except Exception as exc:  # noqa: BLE001
        logger.warning("申万三级分类法拉取失败: %s", exc)

    return l3_to_l2, l2_to_l1, l3_name_by_code


def _fetch_sw_constituents_legulegu(code: str) -> list[str]:
    """直接抓乐咕乐股的三级行业成分股, 返回股票代码列表.

    绕过 akshare.sw_index_third_cons 的缺陷: 该接口写死 18 列表头,
    而乐咕乐股改版后表格列数与表头均变化(还混入了 JSON-LD 脏文本),
    会抛 'Length mismatch'. 这里直接读网页表格真实表头里含 '股票代码' 的列, 稳.
    """
    import requests
    from io import StringIO

    url = f"https://legulegu.com/stockdata/index-composition?industryCode={code}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; classification-refresh/1.0)"}

    def _get():
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.text

    html = _retry(_get)
    tables = pd.read_html(StringIO(html))
    if not tables:
        raise ValueError("乐咕页面无表格(可能限流/坏页), 重试")  # 触发重试
    t = tables[0]
    code_cols = [c for c in t.columns if "股票代码" in str(c)]
    if not code_cols:
        raise ValueError("乐咕页面缺'股票代码'列(可能限流/坏页), 重试")  # 触发重试
    return [str(x).strip() for x in t[code_cols[0]].tolist() if str(x).strip()]


async def _fetch_shenwan_raw() -> dict[str, dict[str, str]]:
    """拉取 股票代码 -> {sw_l1, sw_l2, sw_l3}.

    流程: 用分类法拼出 三级->二级->一级 层级, 再逐三级代码抓成分股,
    把每只成分股的 申万1/2/3级 由层级关系推导出来(成分股页只给三级名).
    并发有界(8), 单点失败不影响整体.
    """
    l3_to_l2, l2_to_l1, l3_name_by_code = _build_sw_hierarchy()
    if not l3_name_by_code:
        return {}

    out: dict[str, dict[str, str]] = {}
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(4)  # 乐咕有限流, 并发别太猛

    async def _one(code: str, l3_name: str) -> None:
        try:
            syms = await loop.run_in_executor(None, _fetch_sw_constituents_legulegu, code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("申万三级成分股失败 %s: %s", code, exc)
            return
        l2 = l3_to_l2.get(l3_name, "")
        l1 = l2_to_l1.get(l2, "")
        for sym in syms:
            out[sym] = {"sw_l1": l1, "sw_l2": l2, "sw_l3": l3_name}

    async def _guarded(code: str, name: str) -> None:
        async with sem:
            await _one(code, name)

    await asyncio.gather(*(_guarded(c, n) for c, n in l3_name_by_code.items()))
    return out


async def _fetch_boards_raw() -> dict[str, dict[str, list[str]]]:
    """拉取 股票代码 -> {boards_industry: [...], boards_concept: [...]}.

    行业板块 + 概念板块, 各拉成分股做反向映射. 任一步失败仅跳过该板块.
    """
    import akshare as ak

    out: dict[str, dict[str, list[str]]] = {}

    def _add(symbol: str, board: str, kind: str) -> None:
        e = out.setdefault(symbol, {"boards_industry": [], "boards_concept": []})
        lst = e[kind]
        if board not in lst:
            lst.append(board)

    # 行业板块
    try:
        names = ak.stock_board_industry_name_em()
        for nm in names["板块名称"].tolist():
            try:
                cons = ak.stock_board_industry_cons_em(symbol=nm)
            except Exception as exc:  # noqa: BLE001
                logger.debug("行业板块成分股失败 %s: %s", nm, exc)
                continue
            for s in cons["代码"].tolist():
                _add(str(s).strip(), str(nm), "boards_industry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("行业板块列表拉取失败: %s", exc)

    # 概念板块
    try:
        names = ak.stock_board_concept_name_em()
        for nm in names["板块名称"].tolist():
            try:
                cons = ak.stock_board_concept_cons_em(symbol=nm)
            except Exception as exc:  # noqa: BLE001
                logger.debug("概念板块成分股失败 %s: %s", nm, exc)
                continue
            for s in cons["代码"].tolist():
                _add(str(s).strip(), str(nm), "boards_concept")
    except Exception as exc:  # noqa: BLE001
        logger.warning("概念板块列表拉取失败: %s", exc)

    return out


# ---------------------------------------------------------------- 落库
def _upsert(rows: list[StockClassification]) -> None:
    with db.session_scope() as s:
        for r in rows:
            existing = s.get(StockClassification, r.symbol)
            if existing is None:
                s.add(r)
            else:
                existing.sw_l1 = r.sw_l1
                existing.sw_l2 = r.sw_l2
                existing.sw_l3 = r.sw_l3
                existing.boards_industry = r.boards_industry
                existing.boards_concept = r.boards_concept
                existing.source = r.source
                existing.updated_at = r.updated_at
                existing.name = r.name or existing.name
        s.commit()


async def refresh_classification(progress_cb: Any = None) -> dict[str, Any]:
    """刷新全量分类映射(申万 + 板块). 返回统计.

    数据由 akshare 生态专属接口提供(申万来自 legulegu.com, 板块来自 akshare SDK),
    无等价备用源(manager 的 get_industry_map 为证监会行业, 口径不同, 不可直接替换).
    故不强行改绑多源, 而是:
    1) 接入 manager 的 akshare 熔断状态 —— 熔断中直接跳过, 避免空跑/超时;
    2) 刷新成败回报 manager, 使分类失败计入 akshare 统一熔断(不再游离于健康体系外);
    3) 失败安全 —— 若本次拉取为空但库中已有数据, 保留上次有效数据并显式报错, 绝不静默清空.
    """
    from app.models.models import _now
    from app.core.datasource import data_source_manager

    # 1) 尊重 akshare 熔断: 熔断期跳过, 不浪费时间重试已知坏源
    if data_source_manager.source_circuit_open("akshare"):
        logger.warning("分类刷新跳过: akshare 处于熔断期")
        return {"ok": False, "error": "akshare_circuit_open", "total": 0}

    if progress_cb:
        progress_cb("申万分类拉取中", 0.1)
    sw = await _fetch_shenwan_raw()
    if progress_cb:
        progress_cb("板块分类拉取中", 0.5)
    boards = await _fetch_boards_raw()

    merged: dict[str, dict[str, Any]] = {}
    for sym, v in sw.items():
        merged[sym] = {**v, "boards_industry": [], "boards_concept": []}
    for sym, v in boards.items():
        e = merged.setdefault(sym, {"sw_l1": "", "sw_l2": "", "sw_l3": "", "boards_industry": [], "boards_concept": []})
        e["boards_industry"] = v.get("boards_industry", [])
        e["boards_concept"] = v.get("boards_concept", [])

    # 3) 失败安全: 拉取为空但库中已有数据 -> 保留上次有效数据, 不静默清空
    existing_count = 0
    with db.session_scope() as s:
        existing_count = s.exec(select(func.count()).select_from(StockClassification)).one()
    if not merged and existing_count > 0:
        logger.warning(
            "分类刷新落空(akshare/legulegu 可能异常), 保留库中已有 %d 条有效数据, 不覆盖",
            existing_count,
        )
        data_source_manager.report_source("akshare", False)
        return {"ok": False, "error": "empty_fetch_preserved", "preserved": existing_count, "total": 0}

    rows: list[StockClassification] = []
    for sym, v in merged.items():
        rows.append(StockClassification(
            symbol=sym,
            name="",
            sw_l1=v.get("sw_l1", ""),
            sw_l2=v.get("sw_l2", ""),
            sw_l3=v.get("sw_l3", ""),
            boards_industry=json.dumps(v.get("boards_industry", []), ensure_ascii=False),
            boards_concept=json.dumps(v.get("boards_concept", []), ensure_ascii=False),
            source="akshare",
            updated_at=_now(),
        ))
    if progress_cb:
        progress_cb("写入数据库", 0.9)
    _upsert(rows)

    # 2) 成败回报 manager: 接入统一熔断(分类失败也会拉高 akshare 连续失败计数)
    data_source_manager.report_source("akshare", ok=bool(merged))

    # 统计(在落库前基于 merged 计算, 避免 session 关闭后访问 detached ORM 对象)
    stats = {
        "ok": True,
        "total": len(merged),
        "sw_l1_covered": sum(1 for v in merged.values() if v.get("sw_l1")),
        "sw_l2_covered": sum(1 for v in merged.values() if v.get("sw_l2")),
        "sw_l3_covered": sum(1 for v in merged.values() if v.get("sw_l3")),
        "board_industry_covered": sum(1 for v in merged.values() if v.get("boards_industry")),
        "board_concept_covered": sum(1 for v in merged.values() if v.get("boards_concept")),
        "l1_distinct": len({v.get("sw_l1") for v in merged.values() if v.get("sw_l1")}),
    }
    if progress_cb:
        progress_cb("完成", 1.0)
    logger.info("分类映射刷新完成: %s", stats)
    return stats


# ---------------------------------------------------------------- 查询
def get_classification(symbol: str) -> StockClassification | None:
    with db.session_scope() as s:
        return s.get(StockClassification, symbol)


def load_classification_map(symbols: list[str]) -> dict[str, StockClassification]:
    """批量加载分类映射, 供选股器分组用. 仅取需要的 symbol, 避免全表扫描."""
    if not symbols:
        return {}
    out: dict[str, StockClassification] = {}
    with db.session_scope() as s:
        rows = s.exec(
            select(StockClassification).where(StockClassification.symbol.in_(symbols))
        ).all()
        for r in rows:
            out[r.symbol] = r
    return out


# ---------------------------------------------------------------- 纯函数(可单测)
def apply_per_industry_cap(
    results: list[dict[str, Any]],
    class_map: dict[str, Any],
    per_industry: int,
    level: str = "sw_l1",
) -> list[dict[str, Any]]:
    """按申万 level(sw_l1/sw_l2/sw_l3) 分组, 每组截到 per_industry 只.

    results: 已按 total 降序的扫描结果.
    class_map: {symbol: StockClassification 或含 level 字段的对象}.
    未知行业归入 '_未知_' 桶(不限制, 避免误杀).
    返回新的截断后列表(保持原序).
    """
    if per_industry <= 0:
        return results
    groups: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for r in results:
        sym = r.get("symbol", "")
        cls = class_map.get(sym)
        key = ""
        if cls is not None:
            key = str(getattr(cls, level, "") or getattr(cls, "industry", "") or "").strip()
        if not key:
            key = "_未知_"
        if key == "_未知_":
            out.append(r)  # 未知行业不限制
            continue
        if groups.get(key, 0) < per_industry:
            groups[key] = groups.get(key, 0) + 1
            out.append(r)
    return out
