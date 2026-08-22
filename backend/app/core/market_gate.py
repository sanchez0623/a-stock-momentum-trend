"""大盘择时闸门(④ 最小版).

思路: 扫描前拉取若干"参考指数"K 线(沪深300/创业板指/上证指数...),
计算每只指数是否处于多头排列(收盘 > MA_long 且 MA_mid > MA_long),
汇总成环境判分 -> 环境 = 看多/看空/中性 -> TopN 乘数 + 人话理由.

纯函数 compute_market_gate(index_dfs, cfg) 不依赖网络, 可单测.
fetch_gate_index_dfs(cfg) 用 DataSourceManager.get_index_kline 拉真实指数 K 线.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.core.datasource import data_source_manager

logger = logging.getLogger(__name__)


def compute_market_gate(index_dfs: dict[str, pd.DataFrame], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """纯函数: 根据参考指数 K 线计算择时闸门.

    返回 {
      environment: 'bull' | 'bear' | 'neutral',
      multiplier: float,        # 应用于 top_n 的乘数
      reason: str,              # 人话理由
      details: [ {name, status, close, ma_long, above_ma} ],
    }
    """
    cfg = cfg or {}
    ma_long = int(cfg.get("ma_long", 200))
    ma_mid = int(cfg.get("ma_mid", 60))
    bull_ratio = float(cfg.get("bull_top_n_ratio", 1.0))
    bear_ratio = float(cfg.get("bear_top_n_ratio", 0.3))
    min_bars = int(cfg.get("min_index_bars", 220))

    details: list[dict[str, Any]] = []
    bull_count = 0
    n_valid = 0  # 仅统计"数据足"的指数(数据不足不计入多/空, 避免冷启动误判空头)

    for name, df in index_dfs.items():
        if df is None or len(df) < ma_long:
            details.append({"name": name, "status": "数据不足", "above_ma": None})
            continue
        close = pd.to_numeric(df["close"], errors="coerce")
        if len(close) < min_bars:
            details.append({"name": name, "status": "数据不足", "above_ma": None})
            continue
        ma_l = float(close.rolling(ma_long).mean().iloc[-1])
        ma_m = float(close.rolling(ma_mid).mean().iloc[-1])
        last = float(close.iloc[-1])
        above = last > ma_l
        aligned = ma_m > ma_l  # 中期均线在长均线上方 = 多头排列
        bull = above and aligned
        bull_count += 1 if bull else 0
        n_valid += 1
        details.append({
            "name": name,
            "status": "bull" if bull else "bear",
            "close": round(last, 2),
            "ma_long": round(ma_l, 2),
            "above_ma": above,
            "aligned": aligned,
        })

    if n_valid == 0:
        # 全部参考指数数据不足(冷启动/缓存未命中): 闸门降级为不生效, 不误判空头
        return {"environment": "neutral", "multiplier": bull_ratio,
                "reason": "参考指数数据不足, 闸门不生效", "details": details}

    if bull_count == n_valid:
        env, mult = "bull", bull_ratio
    elif bull_count == 0:
        env, mult = "bear", bear_ratio
    else:
        env, mult = "neutral", (bull_ratio + bear_ratio) / 2

    reason = (
        f"参考指数 {bull_count}/{n_valid} 处于多头排列"
        f"(站上MA{ma_long} 且 MA{ma_mid}>MA{ma_long})"
        f"→ {'看多(不缩减)' if env=='bull' else ('看空(TopN×'+format(mult,'.1f')+')' if env=='bear' else '中性(折中)' )}"
    )
    return {"environment": env, "multiplier": mult, "reason": reason, "details": details}


async def fetch_gate_index_dfs(cfg: dict[str, Any] | None = None) -> dict[str, pd.DataFrame]:
    """用 DataSourceManager.get_index_kline 拉取配置中的参考指数 K 线.

    任一指数失败跳过(降级). 返回 {name: df}. 依赖网络(用户环境).
    """
    cfg = cfg or {}
    indices = cfg.get("reference_indices", []) or []
    count = int(cfg.get("ma_long", 200)) + int(cfg.get("lookback", 20)) + 20
    out: dict[str, pd.DataFrame] = {}
    for it in indices:
        name = it.get("name", "")
        secid = it.get("secid", "")
        if not secid:
            continue
        try:
            df = await data_source_manager.get_index_kline(secid, "daily", count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("择时闸门: 指数 %s 拉取失败: %s", name, exc)
            df = None
        if df is not None and not df.empty:
            out[name] = df
        else:
            logger.warning("择时闸门: 指数 %s 无数据, 跳过", name)
    return out
