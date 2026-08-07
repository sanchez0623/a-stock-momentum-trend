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


def _tag(text: str, kind: str = "info") -> dict[str, str]:
    """结构化标签. kind: good 利多 / warn 需注意 / bad 偏空 / info 中性."""
    return {"text": text, "kind": kind}


def _build_reason(f: dict[str, Any], cfg: dict) -> dict[str, Any]:
    """把三因子的原始指标翻译成人话.

    纯字符串拼装, 不做任何指标计算 —— 所有数值由 score_indicators 预先算好后传入,
    保证"理由"与"得分"永远同源、不会出现说法与分数打架的情况.

    返回 {reason, risk, tags, detail}:
      reason  一句话综合理由(表格主展示)
      risk    风险提示(可能为空字符串)
      tags    结构化标签, 供前端渲染彩色小chip
      detail  分因子拆解 {趋势/动量/量能: "得分 — 具体依据"}
    """
    trend, momentum, volume = cfg["趋势"], cfg["动量"], cfg["量能"]
    ms, mm, ml = trend["ma_short"], trend["ma_mid"], trend["ma_long"]
    roc_n = momentum["roc_period"]
    tags: list[dict[str, str]] = []
    risks: list[str] = []

    # ---------------------------------------------------------------- 趋势
    adx, adx_th = f["adx"], trend["adx_threshold"]
    parts_t: list[str] = []
    is_bear = False  # 由下方均线分支判定, ADX 措辞复用它, 避免两套条件打架
    if f["bullish"]:
        parts_t.append(f"均线多头排列(MA{ms}>MA{mm}>MA{ml})")
        tags.append(_tag("均线多头", "good"))
    elif f["ma_s"] > f["ma_m"] > 0:
        parts_t.append(f"MA{ms}上穿MA{mm}, 短期转强但长期均线未理顺")
        tags.append(_tag("短期转强", "info"))
    elif f["close"] > f["ma_l"] > 0:
        parts_t.append(f"股价站上{ml}日线, 但均线尚未多头排列")
        tags.append(_tag("站上长均线", "info"))
    else:
        is_bear = True
        parts_t.append(f"均线偏空, 股价运行在MA{mm}/MA{ml}下方")
        tags.append(_tag("趋势偏弱", "bad"))
        risks.append("均线空头排列, 逆势做多风险大")

    # ADX 只衡量趋势"强度"不辨方向: 空头格局下的高 ADX = 强势下跌, 必须标为利空
    if adx < adx_th:
        parts_t.append(f"ADX {adx:.0f} 未达阈值{adx_th}, 趋势力度不足")
        risks.append(f"ADX {adx:.0f} 低于{adx_th}, 可能是震荡而非趋势")
    elif is_bear:
        parts_t.append(f"ADX {adx:.0f} 趋势力度强, 但方向向下(强势下跌)")
        tags.append(_tag(f"ADX{adx:.0f} 下跌趋势", "bad"))
        risks.append(f"ADX {adx:.0f} 配合空头排列, 属强势下跌, 不宜抄底")
    elif adx >= adx_th + 10:
        parts_t.append(f"ADX {adx:.0f} 趋势强劲")
        tags.append(_tag(f"ADX{adx:.0f} 强趋势", "good"))
    else:
        parts_t.append(f"ADX {adx:.0f} 达标(阈值{adx_th}), 趋势成立")
        tags.append(_tag(f"ADX{adx:.0f}", "good"))

    # ---------------------------------------------------------------- 动量
    roc, rsi_v, hist, hist_prev = f["roc"], f["rsi"], f["hist"], f["hist_prev"]
    parts_m: list[str] = []
    if roc > 0:
        parts_m.append(f"近{roc_n}日涨{roc:.1f}%")
        if roc >= 20:
            tags.append(_tag(f"{roc_n}日+{roc:.0f}%", "warn"))
            risks.append(f"近{roc_n}日已涨{roc:.0f}%, 涨幅偏大, 追高需谨慎")
        else:
            tags.append(_tag(f"{roc_n}日+{roc:.1f}%", "good"))
    else:
        parts_m.append(f"近{roc_n}日跌{abs(roc):.1f}%, 动量为负")
        tags.append(_tag(f"{roc_n}日{roc:.1f}%", "bad"))

    # 措辞强度与 MACD 实际贡献分挂钩: 红柱极小(贡献<3分)时不能说成"放大", 否则理由比分数乐观
    if hist <= 0:
        parts_m.append("MACD仍在水下(绿柱)")
        tags.append(_tag("MACD水下", "bad"))
    elif f["macd_score"] < 3.0:
        parts_m.append("MACD柱刚翻红但幅度极小, 多空胶着")
        tags.append(_tag("MACD零轴胶着", "info"))
    elif hist_prev <= 0:
        parts_m.append("MACD金叉")
        tags.append(_tag("MACD金叉", "good"))
    elif hist >= hist_prev:
        parts_m.append("MACD红柱放大")
        tags.append(_tag("红柱放大", "good"))
    else:
        parts_m.append("MACD红柱缩短, 上攻动能衰减")
        tags.append(_tag("红柱缩短", "warn"))
        risks.append("MACD红柱缩短, 短线动能有衰减迹象")

    if rsi_v > 80:
        parts_m.append(f"RSI {rsi_v:.0f} 超买")
        tags.append(_tag(f"RSI{rsi_v:.0f} 超买", "warn"))
        risks.append(f"RSI {rsi_v:.0f} 进入超买区, 随时可能技术性回调")
    elif 70 < rsi_v <= 80:
        parts_m.append(f"RSI {rsi_v:.0f} 偏高")
        tags.append(_tag(f"RSI{rsi_v:.0f} 偏高", "warn"))
    elif 50 <= rsi_v <= 70:
        parts_m.append(f"RSI {rsi_v:.0f} 强势区")
        tags.append(_tag(f"RSI{rsi_v:.0f}", "good"))
    elif 40 <= rsi_v < 50:
        parts_m.append(f"RSI {rsi_v:.0f} 中性偏弱")
    else:
        parts_m.append(f"RSI {rsi_v:.0f} 弱势")
        tags.append(_tag(f"RSI{rsi_v:.0f} 弱势", "bad"))

    # ---------------------------------------------------------------- 量能
    vr, vr_th, price_up = f["volume_ratio"], volume["volume_ratio_threshold"], f["price_up"]
    parts_v: list[str] = []
    if vr >= vr_th and price_up:
        parts_v.append(f"放量{vr:.1f}倍且当日收阳, 量价配合")
        tags.append(_tag(f"放量{vr:.1f}倍", "good"))
    elif vr >= vr_th:
        parts_v.append(f"放量{vr:.1f}倍但当日收阴, 需防冲高出货")
        tags.append(_tag("放量收阴", "warn"))
        risks.append(f"放量{vr:.1f}倍却收阴线, 警惕高位派发")
    elif price_up:
        parts_v.append(f"量比{vr:.1f}倍未过阈值{vr_th}, 上涨缺量能配合")
    else:
        parts_v.append(f"量比{vr:.1f}倍, 缩量整理")
    if vr >= 3:
        risks.append(f"量比{vr:.1f}倍异常放大, 谨防冲高回落")

    # ---------------------------------------------------------------- 乖离
    bias = f["bias"]
    if bias >= 8:
        risks.append(f"股价偏离MA{ms}达{bias:.1f}%, 短线乖离过大易回踩")
        tags.append(_tag(f"乖离{bias:.0f}%", "warn"))

    return {
        "reason": "；".join(["、".join(parts_t), "、".join(parts_m), "、".join(parts_v)]),
        "risk": "；".join(risks),
        "tags": tags,
        "detail": {
            "趋势": f"{f['trend_score']:.1f}/40 — " + "、".join(parts_t),
            "动量": f"{f['momentum_score']:.1f}/40 — " + "、".join(parts_m),
            "量能": f"{f['volume_score']:.1f}/20 — " + "、".join(parts_v),
        },
    }


def score_indicators(ind: pd.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    """三因子评分(纯函数, 输入已含指标列的 DataFrame, 取最后一根 bar).

    输出除各项得分外, 还含 reason/risk/tags/detail 四个人话字段(见 _build_reason).
    """
    cfg = cfg or config_manager.get()
    trend = cfg["趋势"]
    momentum = cfg["动量"]
    volume = cfg["量能"]
    last = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else last
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
    hist_prev = _f(prev.get("macd_hist"))
    roc_score = min(15.0, max(0.0, roc / 5 * 15))
    rsi_score = 10.0 if 50 <= rsi <= 70 else (5.0 if 40 <= rsi < 50 or 70 < rsi <= 80 else 0.0)
    macd_score = min(15.0, max(0.0, hist / 1.0 * 15))
    momentum_score = min(40.0, roc_score + rsi_score + macd_score)

    # ---- 量能分 0-20
    vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
    vr_threshold = volume["volume_ratio_threshold"]
    volume_score = min(10.0, max(0.0, (vr - vr_threshold) / 3 * 10))
    # 量价配合: 收阳 + 放量 加分
    close_now, close_prev = _f(last["close"]), _f(prev["close"])
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

    # 乖离率: 现价偏离短期均线的百分比(判断短线是否追高)
    bias = round((close_now - ma_s_v) / ma_s_v * 100, 2) if ma_s_v > 0 else 0.0

    out: dict[str, Any] = {
        "trend_score": round(trend_score, 1),
        "momentum_score": round(momentum_score, 1),
        "volume_score": round(volume_score, 1),
        "total": total,
        "attention": attention,
        "close": round(close_now, 2),
        "adx": round(adx, 1),
        "roc": round(roc, 2),
        "rsi": round(rsi, 1),
        "volume_ratio": round(vr, 2),
        "bias": bias,
        "date": str(last["date"]),
    }
    # 人话理由(与上面得分同源, 不重算指标)
    out.update(_build_reason({
        **out,
        "bullish": bullish,
        "ma_s": ma_s_v, "ma_m": ma_m_v, "ma_l": ma_l_v,
        "hist": hist, "hist_prev": hist_prev, "macd_score": macd_score,
        "price_up": price_up,
    }, cfg))
    return out


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
        # symbol -> name 映射(结果带名称, 零额外接口调用)
        name_map = {sym: name for sym, name, _ in filtered}

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
                score["name"] = name_map.get(symbol, "")
                score["amount_avg"] = round(avg_amount / 100_000_000, 2)  # 亿
                results.append(score)
            except Exception as exc:  # noqa: BLE001
                logger.debug("扫描 %s 失败: %s", symbol, exc)
                continue
            await asyncio.sleep(0.05)  # 降压

        results.sort(key=lambda r: r["total"], reverse=True)
        return results[:top_n]
