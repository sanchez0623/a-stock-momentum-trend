"""选股器核心实现.

- 三因子评分(与入场信号同源, 方案 §4.5):
  趋势分(0-40): ADX + 多头排列
  动量分(0-40): ROC + RSI 位置 + MACD 柱
  量能分(0-20): 量比 + 量价配合度
- 过滤: 剔除 ST / 停牌 / 流动性不足(日均成交额阈值)
- 输出: Top N 排名表, 含每项得分/总分/人话理由/建议关注度
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import pandas as pd

from app.core.config import config_manager
from app.core.datasource import data_source_manager
from app.core.indicators import compute_all

logger = logging.getLogger(__name__)

# 流动性过滤: 日均成交额下限(元)
MIN_DAILY_AMOUNT = 50_000_000  # 5000 万

# 板块 -> 代码前缀(扫描范围过滤)
BOARD_PREFIXES: dict[str, tuple[str, ...]] = {
    "main": ("600", "601", "603", "605", "000", "001", "002", "003"),  # 沪深主板
    "chinext": ("300", "301", "302"),  # 创业板
    "star": ("688", "689"),            # 科创板
    "bj": ("43", "83", "87", "88", "92"),  # 北交所
}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return default if pd.isna(v) else v
    except (TypeError, ValueError):
        return default


def score_indicators(ind: pd.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    """三因子评分(纯函数, 输入已含指标列的 DataFrame, 取最后一根 bar)."""
    cfg = cfg or config_manager.get()
    trend = cfg["趋势"]
    momentum = cfg["动量"]
    volume = cfg["量能"]
    last = ind.iloc[-1]
    ma_s, ma_m, ma_l = f"ma{trend['ma_short']}", f"ma{trend['ma_mid']}", f"ma{trend['ma_long']}"

    # ---- 趋势分 0-40
    adx = _f(last.get(f"adx{trend.get('adx_period', 14)}"))
    ma_s_v, ma_m_v, ma_l_v = _f(last.get(ma_s)), _f(last.get(ma_m)), _f(last.get(ma_l))
    bullish = ma_s_v > ma_m_v > ma_l_v
    trend_score = min(20.0, max(0.0, (adx - trend["adx_threshold"]) / 25 * 20)) + (20.0 if bullish else 0.0)

    # ---- 动量分 0-40
    roc = _f(last.get(f"roc{momentum['roc_period']}"))
    rsi = _f(last.get(f"rsi{momentum['rsi_period']}"), 50)
    hist = _f(last.get("macd_hist"))
    roc_score = min(15.0, max(0.0, roc / 5 * 15))
    rsi_score = 10.0 if 50 <= rsi <= 70 else (5.0 if 40 <= rsi < 50 or 70 < rsi <= 80 else 0.0)
    macd_score = min(15.0, max(0.0, hist / 1.0 * 15))
    momentum_score = min(40.0, roc_score + rsi_score + macd_score)

    # ---- 量能分 0-20
    vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
    vr_threshold = volume["volume_ratio_threshold"]
    volume_score = min(10.0, max(0.0, (vr - vr_threshold) / 3 * 10))
    # 量价配合: 收阳 + 放量 加分
    close_now, close_prev = _f(last["close"]), _f(ind.iloc[-2]["close"]) if len(ind) > 1 else _f(last["close"])
    price_up = close_now > close_prev
    volume_score += 10.0 if (price_up and vr > vr_threshold) else 0.0

    total = round(trend_score + momentum_score + volume_score, 1)
    if total >= 70:
        attention = "强烈关注"
    elif total >= 60:
        attention = "重点观察"
    elif total >= 50:
        attention = "一般关注"
    else:
        attention = "观察"
    return {
        "trend_score": round(trend_score, 1),
        "momentum_score": round(momentum_score, 1),
        "volume_score": round(volume_score, 1),
        "total": total,
        "attention": attention,
        "close": round(_f(last["close"]), 2),
        "adx": round(adx, 1),
        "roc": round(roc, 2),
        "rsi": round(rsi, 1),
        "volume_ratio": round(vr, 2),
        "date": str(last["date"]),
    }


class StockScreener:
    """全市场扫描(盘后定时) / 指定池扫描."""

    def __init__(self) -> None:
        pass

    def _cfg(self) -> dict:
        return config_manager.get()

    async def _resolve_symbols(self, market: str) -> list[tuple[str, str, str]]:
        """确定扫描池: 本地 stocks 表缓存优先, 其次在线拉取(成功则回写缓存).

        返回 [(symbol, name, industry)].
        东财 clist 全量列表接口存在连接级风控, 本地缓存保证列表获取不依赖每次在线调用.
        """
        from sqlmodel import select

        from app import db
        from app.models.models import Stock

        # 1. 本地缓存
        with db.session_scope() as s:
            stmt = select(Stock.symbol, Stock.name, Stock.industry).order_by(Stock.symbol)
            rows = list(s.exec(stmt).all())
        if rows:
            logger.info("扫描池来自本地 stocks 缓存: %d 只", len(rows))
            return [(r[0], r[1], r[2] or "") for r in rows]

        # 2. 在线拉取 + 回写缓存
        stocks = await data_source_manager.get_stock_list(market)
        if stocks:
            with db.session_scope() as s:
                for st in stocks:
                    row = s.get(Stock, st.symbol)
                    if row is None:
                        s.add(Stock(symbol=st.symbol, name=st.name, market=st.market, industry=st.industry))
                    elif not row.industry and st.industry:
                        row.industry = st.industry  # 补全行业
                s.commit()
            logger.info("扫描池在线拉取: %d 只(已写入 stocks 缓存)", len(stocks))
            return [(st.symbol, st.name, st.industry or "") for st in stocks]

        # 3. 降级: 自选 + 持仓(东财列表接口风控时仍有池可扫)
        from app.models.models import Position, Watchlist

        with db.session_scope() as s:
            wl = [(r.symbol, r.name) for r in s.exec(select(Watchlist)).all()]
            pos = [(r.symbol, r.name) for r in s.exec(select(Position).where(Position.status == "holding")).all()]
        combined = {sym: (name, "") for sym, name in wl + pos}
        if combined:
            logger.warning("在线股票列表不可用(东财风控?), 降级为自选+持仓池: %d 只", len(combined))
            return [(sym, info[0], info[1]) for sym, info in combined.items()]
        return []

    @staticmethod
    def _match_board(symbol: str, board: str) -> bool:
        prefixes = BOARD_PREFIXES.get(board)
        return bool(prefixes) and symbol.startswith(prefixes)

    async def scan(
        self,
        symbols: list[str] | None = None,
        market: str = "all",
        board: str | None = None,   # main/chinext/star/bj
        industry: str | None = None,  # 申万行业名(包含匹配)
        top_n: int = 30,
        min_amount: float = MIN_DAILY_AMOUNT,
        progress_cb: Callable[[int, int], None] | None = None,
        count: int = 80,
    ) -> list[dict[str, Any]]:
        """扫描并排名. symbols 为空时用本地缓存/在线列表(默认过滤 ST), 支持板块/行业缩小范围."""
        cfg = self._cfg()
        pool: list[tuple[str, str, str]] = []
        if symbols is not None:
            pool = [(sym, "", "") for sym in symbols]
        else:
            pool = await self._resolve_symbols(market)

        # 过滤 ST / *ST / 退市
        filtered = [(sym, name, ind) for sym, name, ind in pool
                    if "ST" not in name.upper() and "退" not in name]
        # 板块过滤(代码前缀)
        if board and board in BOARD_PREFIXES:
            filtered = [(sym, name, ind) for sym, name, ind in filtered if self._match_board(sym, board)]
        # 行业过滤(申万行业, 包含匹配)
        if industry:
            kw = industry.strip().lower()
            filtered = [(sym, name, ind) for sym, name, ind in filtered if kw in (ind or "").lower()]
            if not filtered:
                logger.warning("行业过滤后为空: %s(本地行业数据可能未就绪, 需东财列表成功拉取一次)", industry)

        symbols = [sym for sym, _, _ in filtered]
        if not symbols:
            return []

        results: list[dict[str, Any]] = []
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            if progress_cb and (i % 20 == 0 or i == total - 1):
                progress_cb(i + 1, total)
            try:
                df = await data_source_manager.get_kline(symbol, "daily", count)
                if df is None or len(df) < 40:
                    continue  # 停牌/数据不足
                # 流动性过滤: 近 20 日均额
                avg_amount = _f(pd.to_numeric(df["amount"], errors="coerce").tail(20).mean())
                if avg_amount < min_amount:
                    continue
                ind = compute_all(
                    df,
                    ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
                    macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
                    rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
                    volume_ma=cfg["量能"]["volume_ma"],
                )
                score = score_indicators(ind, cfg)
                score["symbol"] = symbol
                score["amount_avg"] = round(avg_amount / 100_000_000, 2)  # 亿
                results.append(score)
            except Exception as exc:  # noqa: BLE001
                logger.debug("扫描 %s 失败: %s", symbol, exc)
                continue
            await asyncio.sleep(0.05)  # 降压

        results.sort(key=lambda r: r["total"], reverse=True)
        return results[:top_n]
