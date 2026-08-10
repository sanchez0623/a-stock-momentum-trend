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
from app.models.models import StockClassification

logger = logging.getLogger(__name__)


def _retry(fn, times: int = 5, sleep: float = 2.0):
    """同步 I/O 重试(指数退避): 乐咕页面限流/超时/坏页时逐步拉长间隔, 比固定间隔更扛限流."""
    last: Exception | None = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < times - 1:
                time.sleep(sleep * (2 ** i))
    raise last


# ---------------------------------------------------------------- 申万分类拉取
def _fetch_sw_hierarchy_once() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """**一次请求**乐咕行业总览页, 解析 level1/2/3 三个 tab, 返回全部行业层级.

    替代 akshare 的 sw_index_first/second/third_info(三者都请求同一页面、各解析一个
    div, 连调三次 = 三倍限流压力). 本函数单次请求拿到 31+124+335 个行业的
    代码/名称/上级行业, 限流场景下大幅降低请求数.

    返回 (l3_to_l2, l2_to_l1, l3_name_by_code), 任一步失败抛异常由调用方降级.
    """
    import requests
    from bs4 import BeautifulSoup

    url = "https://legulegu.com/stockdata/sw-industry-overview"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; classification-refresh/1.0)"}

    def _get():
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.text

    html = _retry(_get)
    soup = BeautifulSoup(html, features="lxml")

    def _parse(level: str) -> list[tuple[str, str, str]]:
        """返回 [(代码, 名称, 上级行业)]; 一级无上级则为 ''."""
        node = soup.find(name="div", attrs={"id": f"level{level}Items"})
        if node is None:
            raise ValueError(f"行业总览页缺少 level{level} 区块(限流/坏页)")
        codes = node.find_all(name="div", attrs={"class": "lg-industries-item-chinese-title"})
        names = node.find_all(name="div", attrs={"class": "lg-industries-item-number"})
        if len(codes) != len(names):
            raise ValueError(f"level{level} 区块结构异常")
        out: list[tuple[str, str, str]] = []
        for c, n in zip(codes, names, strict=True):
            code = c.get_text().strip()
            raw = n.get_text()
            name = raw.split("(")[0].strip()
            parent = ""
            if level != "1" and n.find("span"):
                parent = n.find("span").get_text().split("(")[0].strip()[1:-1]
            if code and name:
                out.append((code, name, parent))
        return out

    l3_to_l2: dict[str, str] = {}
    l2_to_l1: dict[str, str] = {}
    l3_name_by_code: dict[str, str] = {}
    for code, name, parent in _parse("3"):
        l3_to_l2[name] = parent
        l3_name_by_code[code] = name
    for _code, name, parent in _parse("2"):
        l2_to_l1[name] = parent
    return l3_to_l2, l2_to_l1, l3_name_by_code


def _build_sw_hierarchy() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """构建 申万 三级->二级->一级 的父子映射.

    数据来自 akshare 的 sw_index_third_info / sw_index_second_info(元数据接口, 稳定可用).
    返回 (l3_to_l2, l2_to_l1, l3_name_by_code). 任一步失败仅降级(缺失部分映射), 不整体崩.
    """
    try:
        return _fetch_sw_hierarchy_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("单页层级解析失败(%s), 回退 akshare 两次独立调用", exc)
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
    from io import StringIO

    import requests

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
    sem = asyncio.Semaphore(2)  # 乐咕限流敏感, 并发 2 且每请求间隔降压

    async def _one(code: str, l3_name: str) -> None:
        try:
            syms = await loop.run_in_executor(None, _fetch_sw_constituents_legulegu, code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("申万三级成分股失败 %s: %s", code, exc)
            return
        l2 = l3_to_l2.get(l3_name, "")
        l1 = l2_to_l1.get(l2, "")
        for sym in syms:
            # 乐咕可能返回带交易所后缀的代码(如 600598.SH), 归一化为 6 位;
            # 非 6 位数字的脏数据直接丢弃, 避免污染分类表(2026-08-10 清理过 2 条)
            sym = str(sym).strip().split(".")[0]
            if len(sym) != 6 or not sym.isdigit():
                continue
            out[sym] = {"sw_l1": l1, "sw_l2": l2, "sw_l3": l3_name}

    async def _guarded(code: str, name: str) -> None:
        async with sem:
            await _one(code, name)
            await asyncio.sleep(0.8)  # 每次请求后固定间隔, 压低瞬时频率

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
def _upsert(rows: list[StockClassification], update_boards: bool = True) -> None:
    """批量 upsert 分类映射.

    update_boards=False: 只更新申万三级(理杏仁源无东财板块数据), 板块字段保留已有值.
    """
    with db.session_scope() as s:
        for r in rows:
            existing = s.get(StockClassification, r.symbol)
            if existing is None:
                s.add(r)
                continue
            existing.sw_l1 = r.sw_l1
            existing.sw_l2 = r.sw_l2
            existing.sw_l3 = r.sw_l3
            if update_boards:
                existing.boards_industry = r.boards_industry
                existing.boards_concept = r.boards_concept
            existing.source = r.source
            existing.updated_at = r.updated_at
            existing.name = r.name or existing.name
        s.commit()


# ---------------------------------------------------------------- 理杏仁源(申万2021)
_LIX_SOURCE: Any = None


def _get_lixinger_source() -> Any:
    """理杏仁源单例: token 读配置, 配置变更后自动重建(随 .env 热更新)."""
    global _LIX_SOURCE
    from app.core.config import config_manager
    from app.core.datasource.lixinger_src import LixingerSource

    token = str(config_manager.get().get("数据源", {}).get("lixinger", {}).get("token", ""))
    if _LIX_SOURCE is None or getattr(_LIX_SOURCE, "token", None) != token:
        _LIX_SOURCE = LixingerSource(token=token)
    return _LIX_SOURCE


async def _fetch_lixinger_sw_raw(progress_cb: Any = None) -> dict[str, dict[str, str]]:
    """理杏仁申万2021 全市场三级映射(两次请求, 见 lixinger_src.get_sw_classification)."""
    src = _get_lixinger_source()
    if not src.enabled:
        raise RuntimeError("LIXINGER_TOKEN 未配置, 无法使用理杏仁源")
    if progress_cb:
        progress_cb("理杏仁: 拉取行业与成分股", 0.15)
    return await src.get_sw_classification()


async def _save_lixinger_sw(sw: dict[str, dict[str, str]], progress_cb: Any = None) -> dict[str, Any]:
    """理杏仁结果落库: 只更新 sw_* 字段, 板块字段保留已有值."""
    from app.models.models import _now

    rows: list[StockClassification] = []
    for sym, v in sw.items():
        rows.append(StockClassification(
            symbol=sym, name="",
            sw_l1=v.get("sw_l1", ""), sw_l2=v.get("sw_l2", ""), sw_l3=v.get("sw_l3", ""),
            boards_industry="[]", boards_concept="[]",
            source="lixinger", updated_at=_now(),
        ))
    if progress_cb:
        progress_cb("写入数据库", 0.9)
    _upsert(rows, update_boards=False)
    if progress_cb:
        progress_cb("完成", 1.0)
    stats = {
        "ok": True, "source": "lixinger",
        "total": len(sw),
        "sw_l1_covered": sum(1 for v in sw.values() if v.get("sw_l1")),
        "sw_l2_covered": sum(1 for v in sw.values() if v.get("sw_l2")),
        "sw_l3_covered": sum(1 for v in sw.values() if v.get("sw_l3")),
        "l1_distinct": len({v.get("sw_l1") for v in sw.values() if v.get("sw_l1")}),
    }
    logger.info("理杏仁分类映射刷新完成: %s", stats)
    return stats


async def refresh_classification(progress_cb: Any = None, source: str = "auto") -> dict[str, Any]:
    """刷新全量分类映射(申万三级 + 板块). 返回统计.

    source:
      - auto:      理杏仁(申万2021)优先, 未配置/失败自动回落 akshare(原逻辑)
      - lixinger:  仅理杏仁, 两次请求构建全市场三级映射, 只更新 sw_* 字段
      - akshare:   仅原逻辑(legulegu 申万 + akshare 东财板块)

    理杏仁路径(2026-08-10 接入):
    1) 接入 manager 熔断(lixinger) —— 熔断中直接跳过; 成败回报 manager;
    2) 失败安全 —— 拉取为空则回落/报错, 不覆盖库中数据;
    3) 板块数据(东财口径)理杏仁不提供, 落库时保留库中已有值.
    申万2021 与 akshare 申万旧版为不同口径, 二者互为补充, 以最近一次刷新源为准.

    akshare 路径(原逻辑):
    数据由 akshare 生态专属接口提供(申万来自 legulegu.com, 板块来自 akshare SDK),
    接入 manager 的 akshare 熔断状态与成败回报 + 失败安全.
    """
    from app.core.datasource import data_source_manager

    manager = data_source_manager

    # ---- 理杏仁优先(auto) 或 显式指定(lixinger) ----
    if source in ("auto", "lixinger"):
        lx = _get_lixinger_source()
        lx_usable = lx.enabled and not manager.source_circuit_open("lixinger")
        if lx_usable:
            try:
                sw = await _fetch_lixinger_sw_raw(progress_cb)
            except Exception as exc:  # noqa: BLE001
                logger.warning("理杏仁分类刷新失败: %s", exc)
                sw = {}
            if sw:
                manager.report_source("lixinger", True)
                return await _save_lixinger_sw(sw, progress_cb)
            manager.report_source("lixinger", False)
            if source == "lixinger":
                return {"ok": False, "error": "lixinger_empty", "total": 0}
        elif source == "lixinger":
            reason = ("lixinger_circuit_open" if manager.source_circuit_open("lixinger")
                      else "lixinger_not_configured")
            return {"ok": False, "error": reason, "total": 0}

    # ---- akshare 路径(原逻辑: source=akshare 或 auto 回落) ----
    from app.models.models import _now

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
        "source": "akshare",
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


def industry_tree() -> list[dict[str, Any]]:
    """申万三级行业树(一级 -> 二级 -> 三级), 每级带覆盖股票数, 按一级名排序.

    选股页树形多选用: 选中任意级节点, 扫描时按 sw_l1/l2/l3 精确匹配.
    """
    with db.session_scope() as s:
        rows = s.exec(
            select(StockClassification.sw_l1, StockClassification.sw_l2,
                   StockClassification.sw_l3, func.count())
            .where(StockClassification.sw_l1 != "")
            .group_by(StockClassification.sw_l1, StockClassification.sw_l2,
                      StockClassification.sw_l3)
        ).all()
    root: dict[str, dict[str, Any]] = {}
    for l1, l2, l3, cnt in rows:
        n1 = root.setdefault(l1, {"name": l1, "count": 0, "children": {}})
        n1["count"] += int(cnt)
        if l2:
            n2 = n1["children"].setdefault(l2, {"name": l2, "count": 0, "children": {}})
            n2["count"] += int(cnt)
            if l3:
                n3 = n2["children"].setdefault(l3, {"name": l3, "count": 0})
                n3["count"] += int(cnt)

    def _to_list(node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"name": node["name"], "count": node["count"]}
        if node.get("children"):
            kids = [_to_list(v) for v in node["children"].values()]
            kids.sort(key=lambda x: (-x["count"], x["name"]))
            out["children"] = kids
        return out

    items = [_to_list(v) for v in root.values()]
    items.sort(key=lambda x: (-x["count"], x["name"]))
    return items


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
