"""选股池(universe)预筛: 用指数成分股把全A噪声池缩成质量池.

为什么值得做:
- 全A 5000+ 只里绝大多数是低流动性、机构不覆盖、消息面驱动的票, 动量信号噪声极大
- 沪深300/中证500 是"中大盘 + 流动性好 + 有基本面支撑"的天然预筛, 正好匹配动量趋势风格
- 池子从 5000 缩到 300~800, 单次扫描耗时同比例下降

数据源: baostock(query_hs300_stocks / query_zz500_stocks / query_sz50_stocks).
缓存: IndexConstituent 表, 超过 max_age_days 自动刷新; 取不到时按配置降级为不预筛.

纯函数(可单测, 不依赖网络/DB):
- parse_universe(name) -> 需要的指数 key 列表
- apply_universe(pool, allowed) -> 预筛后的池子
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.core.datasource import data_source_manager
from app.models.models import IndexConstituent

logger = logging.getLogger(__name__)

# universe 名称 -> 需要合并的指数 key
UNIVERSE_ALIASES: dict[str, list[str]] = {
    "all": [],
    "hs300": ["hs300"],
    "zz500": ["zz500"],
    "sz50": ["sz50"],
    "hs300+zz500": ["hs300", "zz500"],
    "zz800": ["hs300", "zz500"],  # 中证800 的近似(300+500)
}

UNIVERSE_LABELS: dict[str, str] = {
    "all": "全A",
    "hs300": "沪深300",
    "zz500": "中证500",
    "sz50": "上证50",
    "hs300+zz500": "沪深300+中证500(≈中证800)",
    "zz800": "沪深300+中证500(≈中证800)",
}

INDEX_LABELS = {"hs300": "沪深300", "zz500": "中证500", "sz50": "上证50"}


# ---------------------------------------------------------------- 纯函数
def parse_universe(name: str | None) -> list[str]:
    """universe 名称 -> 指数 key 列表. 未知名称按 all 处理(不预筛)."""
    key = (name or "all").strip().lower()
    if key not in UNIVERSE_ALIASES:
        logger.warning("未知选股池 '%s', 按全A处理(可选: %s)", name, list(UNIVERSE_ALIASES))
        return []
    return UNIVERSE_ALIASES[key]


def apply_universe(pool: list[tuple[str, str, str]], allowed: set[str]) -> list[tuple[str, str, str]]:
    """按成分股集合过滤扫描池. allowed 为空视为不预筛(原样返回)."""
    if not allowed:
        return pool
    return [item for item in pool if item[0] in allowed]


# ---------------------------------------------------------------- DB 读写
def load_universe_symbols(index_keys: list[str]) -> tuple[set[str], str]:
    """从缓存表读取成分股集合. 返回 (symbols, 最早的 updated_at)."""
    if not index_keys:
        return set(), ""
    syms: set[str] = set()
    oldest = ""
    with db.session_scope() as s:
        rows = s.exec(
            select(IndexConstituent).where(IndexConstituent.index_key.in_(index_keys))
        ).all()
        for r in rows:
            syms.add(r.symbol)
            if not oldest or r.updated_at < oldest:
                oldest = r.updated_at
    return syms, oldest


def _save_constituents(index_key: str, items: list[tuple[str, str]]) -> int:
    """全量替换某指数的成分股(成分股会调整, 增量合并会残留退出的票)."""
    from app.models.models import _now

    now = _now()
    with db.session_scope() as s:
        for old in s.exec(select(IndexConstituent).where(IndexConstituent.index_key == index_key)).all():
            s.delete(old)
        s.flush()
        for sym, name in items:
            s.add(IndexConstituent(index_key=index_key, symbol=sym, name=name, updated_at=now))
        s.commit()
    return len(items)


def universe_stats() -> dict[str, Any]:
    """各指数成分股缓存概况(供 API/前端展示)."""
    out: dict[str, Any] = {}
    with db.session_scope() as s:
        rows = s.exec(select(IndexConstituent)).all()
    for r in rows:
        e = out.setdefault(r.index_key, {"count": 0, "updated_at": "", "label": INDEX_LABELS.get(r.index_key, r.index_key)})
        e["count"] += 1
        if r.updated_at > e["updated_at"]:
            e["updated_at"] = r.updated_at
    return out


def _is_stale(updated_at: str, max_age_days: int) -> bool:
    """缓存是否过期. 解析失败按过期处理(宁可多刷一次)."""
    if not updated_at:
        return True
    try:
        ts = dt.datetime.strptime(updated_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (dt.datetime.now() - ts).days >= max(1, int(max_age_days))


# ---------------------------------------------------------------- 刷新
async def refresh_universe(index_keys: list[str] | None = None,
                           progress_cb: Any = None) -> dict[str, Any]:
    """拉取成分股并落库. index_keys 为空时刷新全部三个指数."""
    keys = index_keys or list(INDEX_LABELS)
    stats: dict[str, Any] = {"refreshed": {}, "failed": []}
    for i, key in enumerate(keys):
        if progress_cb:
            progress_cb(f"拉取 {INDEX_LABELS.get(key, key)} 成分股", (i + 0.5) / len(keys))
        try:
            items = await data_source_manager.get_index_constituents(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("成分股拉取失败 %s: %s", key, exc)
            items = []
        if not items:
            stats["failed"].append(key)
            logger.warning("成分股为空 %s(baostock 未启用或网络不通?)", key)
            continue
        n = _save_constituents(key, [(it.symbol, it.name) for it in items])
        stats["refreshed"][key] = n
        logger.info("成分股已更新 %s: %d 只", INDEX_LABELS.get(key, key), n)
    if progress_cb:
        progress_cb("完成", 1.0)
    return stats


async def ensure_universe(universe: str, max_age_days: int = 7) -> tuple[set[str], str]:
    """获取 universe 的成分股集合, 缓存缺失/过期时自动刷新.

    返回 (symbols, note). note 为人话说明, 供日志与扫描汇总展示.
    symbols 为空集表示"不预筛"(全A 或 拉取失败降级).
    """
    keys = parse_universe(universe)
    if not keys:
        return set(), "全A(未预筛)"

    syms, oldest = load_universe_symbols(keys)
    if not syms or _is_stale(oldest, max_age_days):
        reason = "缓存为空" if not syms else f"缓存已过期({oldest})"
        logger.info("选股池 %s %s, 触发在线刷新", universe, reason)
        await refresh_universe(keys)
        syms, oldest = load_universe_symbols(keys)

    label = UNIVERSE_LABELS.get((universe or "all").lower(), universe)
    if not syms:
        return set(), f"{label}成分股不可用(baostock 未就绪), 已降级为全A"
    return syms, f"{label} {len(syms)} 只(更新于 {oldest[:10]})"
