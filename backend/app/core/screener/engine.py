"""选股器核心实现.

- 三因子评分(与入场信号同源, 方案 §4.5):
  趋势分(0-40): ADX(强度 + 近 N 根斜率) + 多头排列 + 趋势连贯性
  动量分(0-40): ROC(过热衰减) + RSI 位置 + MACD 柱(ATR 归一化) + 加速度
  量能分(0-20): 量比 + 量价配合度
- 过滤: 剔除 ST / 停牌 / 流动性不足(日均成交额阈值)
- 输出: Top N 排名表, 含每项得分/总分/人话理由/建议关注度
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
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

# 启动事件判定窗口(近 N 根内发生才算"刚起趋势")
STAGE_WINDOW = 4

_STAGE_EVENT_LABEL = {
    "macd_golden": "MACD金叉",
    "roc_turn": "ROC转正",
    "ma_cross": "短均线刚上穿",
    "adx_first": "ADX首次达标",
}


def _event_golden_cross(hist: pd.Series) -> bool:
    """近 N 根内 MACD 柱由非正转正(金叉)."""
    vals = [_f(v) for v in hist.tail(STAGE_WINDOW)]
    return any(v > 0 and vals[i - 1] <= 0 for i, v in enumerate(vals) if i > 0)


def _event_turn_positive(series: pd.Series) -> bool:
    """近 N 根内由非正转正(如 ROC 由负转正)."""
    vals = [_f(v) for v in series.tail(STAGE_WINDOW)]
    return any(v > 0 and vals[i - 1] <= 0 for i, v in enumerate(vals) if i > 0)


def _event_cross_up(a: pd.Series, b: pd.Series) -> bool:
    """近 N 根内 a 上穿 b(如 短均线刚上穿中均线)."""
    ta, tb = a.tail(STAGE_WINDOW).tolist(), b.tail(STAGE_WINDOW).tolist()
    return any(ta[i] > tb[i] and ta[i - 1] <= tb[i - 1] for i in range(1, len(ta)))


def detect_stage(ind: pd.DataFrame, cfg: dict, end: int | None = None) -> dict[str, Any]:
    """识别趋势阶段(方案B, 纯函数, 与评分同源).

    阶段: launch 启动 / accelerate 加速 / overheat 过热 / exhaust 衰竭 / none 无趋势.
    判定优先级: 衰竭 > 过热 > 启动 > 加速 > 无趋势(仅对趋势向上标的判阶段).
    end: 可选, 指定判定位置(回测逐日复用整段指标时传入, 避免反复复制).

    输出: {stage, events, bonus, penalty, note}
      events  命中的启动事件列表(macd_golden/roc_turn/ma_cross/adx_first)
      bonus   启动加分(已按 launch_bonus_max 封顶)
      penalty 过热/衰竭扣分
    """
    trend = cfg["趋势"]
    momentum = cfg["动量"]
    volume = cfg["量能"]
    sc = cfg.get("趋势阶段", {})
    n = len(ind)
    if end is None:
        end = n
    out: dict[str, Any] = {"stage": "none", "events": [], "bonus": 0.0, "penalty": 0.0, "note": ""}
    if not sc.get("enabled", True) or end < STAGE_WINDOW:
        return out

    last = ind.iloc[end - 1]
    prev = ind.iloc[end - 2] if end > 1 else last
    adx_period = int(trend.get("adx_period", 14))
    roc_period = int(momentum["roc_period"])
    ma_s = f"ma{trend['ma_short']}"
    ma_m = f"ma{trend['ma_mid']}"
    ma_s_v, ma_m_v = _f(last.get(ma_s)), _f(last.get(ma_m))
    close = _f(last["close"])
    adx = _f(last.get(f"adx{adx_period}"))
    adx_th = float(trend["adx_threshold"])
    rsi = _f(last.get(f"rsi{momentum['rsi_period']}"), 50)
    hist, hist_prev = _f(last.get("macd_hist")), _f(prev.get("macd_hist"))
    vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
    bias = round((close - ma_s_v) / ma_s_v * 100, 2) if ma_s_v > 0 else 0.0
    # 趋势向上前提: 收盘站上中期均线(比"多头排列"宽松, 启动期均线往往未完全理顺)
    up_trend = close > ma_m_v > 0

    # ---- 启动事件(近 N 根内"刚发生")——只取判定窗口, 避免整段复制
    win = slice(max(0, end - STAGE_WINDOW), end)
    events: list[str] = []
    if _event_golden_cross(ind["macd_hist"].iloc[win]):
        events.append("macd_golden")
    if _event_turn_positive(ind[f"roc{roc_period}"].iloc[win]):
        events.append("roc_turn")
    if _event_cross_up(ind[ma_s].iloc[win], ind[ma_m].iloc[win]):
        events.append("ma_cross")
    adx_tail = [_f(v) for v in ind[f"adx{adx_period}"].iloc[win]]
    adx_rising = adx >= _f(ind[f"adx{adx_period}"].iloc[end - 6]) if end > 5 else True
    if adx >= adx_th and any(v < adx_th for v in adx_tail) and adx_rising:
        events.append("adx_first")

    # ---- 阶段判定(优先级: 衰竭 > 过热 > 启动 > 加速)
    rsi_overheat = float(sc.get("rsi_overheat", 75.0))
    rsi_exhaust = float(sc.get("rsi_exhaust", 80.0))
    if up_trend and rsi >= rsi_exhaust and hist < hist_prev:
        out["stage"] = "exhaust"
        out["penalty"] = float(sc.get("exhaust_penalty", 5.0))
        out["note"] = f"RSI {rsi:.0f} 超买且 MACD 红柱缩短, 动能衰竭"
    elif up_trend and (bias >= float(sc.get("overheat_bias", 10.0))
                       or rsi >= rsi_overheat
                       or vr >= float(sc.get("overheat_volume", 3.0))):
        out["stage"] = "overheat"
        if bias >= float(sc.get("overheat_bias", 10.0)):
            out["penalty"] += float(sc.get("overheat_bias_penalty", 3.0))
        if rsi >= rsi_overheat:
            out["penalty"] += float(sc.get("overheat_rsi_penalty", 2.0))
        if vr >= float(sc.get("overheat_volume", 3.0)):
            out["penalty"] += float(sc.get("overheat_volume_penalty", 3.0))
        out["note"] = f"乖离{bias:.1f}% / RSI {rsi:.0f} / 量比{vr:.1f} 过热"
    elif events and up_trend:
        weights = {
            "macd_golden": float(sc.get("launch_macd_golden", 2.0)),
            "roc_turn": float(sc.get("launch_roc_turn", 2.0)),
            "ma_cross": float(sc.get("launch_ma_cross", 2.0)),
            "adx_first": float(sc.get("launch_adx_first", 1.0)),
        }
        out["stage"] = "launch"
        out["events"] = events
        out["bonus"] = min(float(sc.get("launch_bonus_max", 5.0)),
                           sum(weights.get(e, 0.0) for e in events))
        out["note"] = "刚起趋势: " + "、".join(_STAGE_EVENT_LABEL.get(e, e) for e in events)
    elif ma_s_v > ma_m_v:
        out["stage"] = "accelerate"
        out["note"] = "多头排列且动量健康, 加速期"
    return out


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
        parts_t.append(f"ADX {adx:.1f} 低于阈值{adx_th:.0f}, 趋势力度不足")
        risks.append(f"ADX {adx:.1f} 低于{adx_th:.0f}, 可能是震荡而非趋势")
    elif is_bear:
        parts_t.append(f"ADX {adx:.1f} 趋势力度强, 但方向向下(强势下跌)")
        tags.append(_tag(f"ADX{adx:.1f} 下跌趋势", "bad"))
        risks.append(f"ADX {adx:.1f} 配合空头排列, 属强势下跌, 不宜抄底")
    elif adx >= adx_th + 10:
        parts_t.append(f"ADX {adx:.1f} 趋势强劲")
        tags.append(_tag(f"ADX{adx:.1f} 强趋势", "good"))
    else:
        parts_t.append(f"ADX {adx:.1f} 达标(阈值{adx_th:.0f}), 趋势成立")
        tags.append(_tag(f"ADX{adx:.1f}", "good"))

    # ② 趋势持续度/连贯性(与分数同源, 不重算指标)
    if f.get("adx_rising"):
        tags.append(_tag("ADX走高", "good"))
    elif adx >= adx_th:
        risks.append("ADX 走平或回落, 趋势动能减弱")
    if f.get("consistency", 0) >= 0.8 and f["bullish"]:
        tags.append(_tag("趋势连贯", "good"))

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

    # ---------------------------------------------------------------- 趋势阶段(方案B)
    stage = f.get("stage", "none")
    if stage == "launch":
        tags.append(_tag("启动期", "good"))
        parts_t.append(f"刚起趋势({f.get('stage_note', '')})")
    elif stage == "accelerate":
        tags.append(_tag("加速期", "good"))
    elif stage == "overheat":
        tags.append(_tag("过热期", "warn"))
        risks.append(f"趋势过热({f.get('stage_note', '')}), 追高风险大, 宜等回踩")
    elif stage == "exhaust":
        tags.append(_tag("衰竭期", "bad"))
        risks.append(f"趋势衰竭({f.get('stage_note', '')}), 不宜新建仓")

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
    """三因子评分(纯函数, 输入已含指标列的 DataFrame, 主要取最后一根 bar, 并辅以近 N 根序列判断趋势持续度/加速度/连贯性).

    输出除各项得分外, 还含 reason/risk/tags/detail 四个人话字段(见 _build_reason)。
    """
    cfg = cfg or config_manager.get()
    trend = cfg["趋势"]
    momentum = cfg["动量"]
    volume = cfg["量能"]
    last = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else last
    adx_period = int(trend.get("adx_period", 14))
    roc_period = int(momentum["roc_period"])
    ma_s, ma_m, ma_l = f"ma{trend['ma_short']}", f"ma{trend['ma_mid']}", f"ma{trend['ma_long']}"

    # ---- 趋势分 0-40
    adx = _f(last.get(f"adx{adx_period}"))
    ma_s_v, ma_m_v, ma_l_v = _f(last.get(ma_s)), _f(last.get(ma_m)), _f(last.get(ma_l))
    bullish = ma_s_v > ma_m_v > ma_l_v
    # ② 趋势持续度: 读近 N 根判断 ADX 是否仍在走强, 达标但转弱打折
    adx_n_ago = _f(ind[f"adx{adx_period}"].iloc[-6]) if len(ind) > 5 else adx
    adx_rising = adx >= adx_n_ago and adx >= trend["adx_threshold"]
    adx_base = min(20.0, max(0.0, (adx - trend["adx_threshold"]) / 25 * 20))
    if adx >= trend["adx_threshold"] and not adx_rising:
        adx_base *= 0.7  # 趋势力度达标但动能衰减
    trend_score = adx_base + (20.0 if bullish else 0.0)

    # ---- 动量分 0-40
    roc = _f(last.get(f"roc{roc_period}"))
    rsi = _f(last.get(f"rsi{momentum['rsi_period']}"), 50)
    hist = _f(last.get("macd_hist"))
    hist_prev = _f(prev.get("macd_hist"))
    close_now, close_prev = _f(last["close"]), _f(prev["close"])
    # ③ ROC 过热衰减: 0~8% 健康区线性给满, 8~20% 边际递减, 20%+ 追高继续衰减到下限
    if roc <= 0:
        roc_score = 0.0
    elif roc < 8:
        roc_score = roc / 8 * 15
    elif roc < 20:
        roc_score = 15 - (roc - 8) / 12 * 5
    else:
        roc_score = max(3.0, 10 - (roc - 20) / 15 * 7)
    roc_score = round(min(15.0, max(0.0, roc_score)), 2)
    # ② 动量加速度: 近几日 ROC 变化, 减速则轻微打折
    roc_n_ago = _f(ind[f"roc{roc_period}"].iloc[-4]) if len(ind) > 3 else roc
    if roc > 0 and (roc - roc_n_ago) < 0:
        roc_score *= 0.9
    rsi_score = 10.0 if 50 <= rsi <= 70 else (5.0 if 40 <= rsi < 50 or 70 < rsi <= 80 else 0.0)
    # ① MACD 评分按 ATR 归一化(无 ATR 时回退 close), 消除绝对价位扭曲
    atr_v = _f(last.get(f"atr{momentum.get('atr_period', 14)}"))
    macd_norm = hist / atr_v if atr_v > 0 else (hist / close_now if close_now > 0 else 0.0)
    macd_score = min(15.0, max(0.0, macd_norm * 15.0))
    momentum_score = min(40.0, roc_score + rsi_score + macd_score)

    # ---- 量能分 0-20
    vr = _f(last.get(f"volume_ratio{volume['volume_ma']}"))
    vr_threshold = volume["volume_ratio_threshold"]
    volume_score = min(10.0, max(0.0, (vr - vr_threshold) / 3 * 10))
    # 量价配合: 收阳 + 放量 加分
    price_up = close_now > close_prev
    volume_score += 10.0 if (price_up and vr > vr_threshold) else 0.0

    # ② 趋势连贯性: 近 20 日收盘站在短期均线之上的占比(0~1)
    consistency = round(float((ind["close"].tail(20) > ind[ma_s].tail(20)).mean()), 2) if len(ind) >= 20 else 0.0

    # ---- 趋势阶段(方案B): 启动加分 / 过热·衰竭扣分
    # 让"刚起趋势"的票浮进高分区, 把乖离过大/动能衰竭的票压下去
    stage_info = detect_stage(ind, cfg)
    stage_bonus = stage_info["bonus"]
    stage_penalty = stage_info["penalty"]

    total = round(trend_score + momentum_score + volume_score + stage_bonus - stage_penalty, 1)
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
        "adx_rising": adx_rising,
        "consistency": consistency,
        "date": str(last["date"]),
        # 趋势阶段(方案B): 阶段/启动事件/加减分
        "stage": stage_info["stage"],
        "stage_events": stage_info["events"],
        "stage_bonus": round(stage_bonus, 1),
        "stage_penalty": round(stage_penalty, 1),
        "stage_note": stage_info["note"],
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
        self.last_scan_summary: dict[str, Any] | None = None

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

    @staticmethod
    def _split_multi(value: str | None) -> list[str]:
        """逗号分隔多值参数 -> 去空白列表. 空/None -> []."""
        if not value:
            return []
        return [x.strip() for x in value.split(",") if x.strip()]

    @staticmethod
    def _filter_by_industry(pool: list[tuple[str, str, str]], keywords: list[str],
                            class_map: dict[str, Any]) -> list[tuple[str, str, str]]:
        """行业过滤: 选中名精确命中申万 sw_l1/l2/l3 任一即通过(支持三级树多选).

        无分类映射的票回退到东财行业(Stock.industry)包含匹配(兼容旧数据).
        """
        if not keywords:
            return pool
        lowered = {kw.lower() for kw in keywords}

        def _match(sym: str, ind: str) -> bool:
            cl = class_map.get(sym)
            if cl is not None:
                # 有申万映射: 精确匹配, 不命中即排除
                return any(name and name.strip().lower() in lowered
                           for name in (cl.sw_l1, cl.sw_l2, cl.sw_l3))
            return any(kw in (ind or "").lower() for kw in lowered)  # 无映射: 回退包含匹配

        return [(sym, name, ind) for sym, name, ind in pool if _match(sym, ind)]

    async def scan(
        self,
        symbols: list[str] | None = None,
        market: str = "all",
        board: str | None = None,   # main/chinext/star/bj, 逗号分隔可多值(如 "main,chinext")
        industry: str | None = None,  # 行业名(包含匹配), 逗号分隔可多值(任一命中即通过)
        top_n: int = 30,
        min_amount: float = MIN_DAILY_AMOUNT,
        progress_cb: Callable[[int, int], None] | None = None,
        count: int = 260,  # 日线根数: 覆盖 52 周高点/长均线/回测所需; 由历史补拉(backfill)预填缓存, 日常命中缓存
        per_industry: int = 0,       # ⑤ 每行业限配 N 只(0=不限)
        industry_level: str = "sw_l1",  # 分组用申万级别
        apply_gate: bool = True,     # ④ 大盘择时闸门
        universe: str | None = None,    # ⑥ 选股池: all/hs300/zz500/hs300+zz500/sz50
        apply_factors: bool = True,     # ⑦ 基本面质量 + 业绩事件因子
    ) -> list[dict[str, Any]]:
        """扫描并排名.

        symbols 为空时用本地缓存/在线列表(默认过滤 ST), 支持板块/行业缩小范围.
        ④ 大盘择时闸门: 环境差时缩减 top_n(扫描前拉参考指数 K 线).
        ⑤ 每行业限配: 排序后按申万级别分组, 每组截到 per_industry 只.
        ⑥ 选股池预筛: 用指数成分股(沪深300/中证500/上证50)缩小扫描池.
        ⑦ 基本面 + 事件: 质量筛/加分与业绩预告加权(只读本地表, 零额外网络调用).
        """
        from app.core.classification import apply_per_industry_cap, load_classification_map
        from app.core.market_gate import compute_market_gate, fetch_gate_index_dfs
        from app.core.universe import apply_universe, ensure_universe

        cfg = self._cfg()
        pool: list[tuple[str, str, str]] = []
        if symbols is not None:
            pool = [(sym, "", "") for sym in symbols]
        else:
            pool = await self._resolve_symbols(market)

        # ---- ⑥ 选股池预筛(指数成分股, 在最耗时的逐票取数之前先缩池) ----
        uni_cfg = cfg.get("选股池", {})
        uni_name = (universe or uni_cfg.get("universe") or "all").strip()
        uni_info: dict[str, Any] = {"universe": uni_name, "applied": False,
                                    "before": len(pool), "after": len(pool), "note": "全A(未预筛)"}
        # 显式传入 symbols(指定列表扫描)时跳过预筛: 显式列表优先, 不被成分股池过滤
        if symbols is not None:
            uni_info["note"] = "显式列表(未预筛)"
        if uni_name.lower() != "all" and symbols is None:
            try:
                allowed, note = await ensure_universe(uni_name, int(uni_cfg.get("max_age_days", 7)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("选股池预筛失败, 降级为全A: %s", exc)
                allowed, note = set(), f"预筛异常降级: {exc}"
            if allowed:
                before = len(pool)
                pool = apply_universe(pool, allowed)
                uni_info.update({"applied": True, "before": before, "after": len(pool), "note": note})
                logger.info("选股池预筛: %s -> %d 只(原 %d 只) | %s", uni_name, len(pool), before, note)
                if not pool and not uni_cfg.get("fallback_on_empty", True):
                    logger.warning("选股池预筛后为空且未允许降级, 返回空结果")
                    return []
            else:
                uni_info["note"] = note
                logger.warning("选股池 %s 不可用, 按全A扫描: %s", uni_name, note)

        # 过滤 ST / *ST / 退市
        filtered = [(sym, name, ind) for sym, name, ind in pool
                    if "ST" not in name.upper() and "退" not in name]
        # 板块过滤(代码前缀, 多值任一命中)
        boards = self._split_multi(board)
        if boards:
            filtered = [(sym, name, ind) for sym, name, ind in filtered
                        if any(self._match_board(sym, b) for b in boards)]
        # 行业过滤(申万三级: 选中名命中 sw_l1/l2/l3 任一即通过; 无映射回退东财包含匹配)
        keywords = self._split_multi(industry)
        if keywords:
            class_map = load_classification_map([sym for sym, _, _ in filtered])
            filtered = self._filter_by_industry(filtered, keywords, class_map)
            if not filtered:
                logger.warning("行业过滤后为空: %s(分类映射未就绪时回退东财行业匹配)", industry)

        symbols = [sym for sym, _, _ in filtered]
        if not symbols:
            return []
        # symbol -> name 映射(结果带名称, 零额外接口调用)
        name_map = {sym: name for sym, name, _ in filtered}

        # ---- ④ 大盘择时闸门(扫描前, 仅拉几次指数 K 线) ----
        gate_info: dict[str, Any] = {"enabled": False, "environment": "neutral", "multiplier": 1.0, "reason": "", "details": []}
        gate_cfg = cfg.get("择时闸门", {})
        if apply_gate and gate_cfg.get("enabled"):
            try:
                idx_dfs = await fetch_gate_index_dfs(gate_cfg)
                gate_info = compute_market_gate(idx_dfs, gate_cfg)
                gate_info["enabled"] = True
                mult = float(gate_info.get("multiplier", 1.0))
                eff = int(top_n * mult)
                logger.info(
                    "择时闸门生效: 环境=%s 乘数=%.2f top_n %d→%d | %s",
                    gate_info["environment"], mult, top_n, eff, gate_info["reason"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("择时闸门计算失败, 降级为不缩减: %s", exc)
                gate_info = {"enabled": True, "environment": "neutral", "multiplier": 1.0,
                             "reason": f"闸门计算异常, 降级不缩减: {exc}", "details": []}
        else:
            logger.info("择时闸门未启用(apply_gate=%s, 配置enabled=%s) -> 不缩放",
                        apply_gate, bool(gate_cfg.get("enabled")))

        results: list[dict[str, Any]] = []
        total = len(symbols)
        # 并发 3(2026-08-10 拍板): 网络等待重叠, 总耗时约为串行的 1/3;
        # 每只仍保留 0.05s 降压, 对上游限流友好
        SCAN_CONCURRENCY = 3

        async def _score_one(symbol: str) -> dict[str, Any] | None:
            """取数+评分单只票; 停牌/数据不足/失败返回 None."""
            try:
                df = await data_source_manager.get_kline(symbol, "daily", count)
                if df is None or len(df) < 40:
                    return None  # 停牌/数据不足
                # 流动性过滤: 近 20 日均额
                avg_amount = _f(pd.to_numeric(df["amount"], errors="coerce").tail(20).mean())
                if avg_amount < min_amount:
                    return None
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
                return score
            except Exception as exc:  # noqa: BLE001
                logger.debug("扫描 %s 失败: %s", symbol, exc)
                return None
            finally:
                await asyncio.sleep(0.05)  # 降压

        for start in range(0, total, SCAN_CONCURRENCY):
            batch = symbols[start:start + SCAN_CONCURRENCY]
            for score in await asyncio.gather(*(_score_one(s) for s in batch)):
                if score is not None:
                    results.append(score)
            if progress_cb:
                progress_cb(min(start + SCAN_CONCURRENCY, total), total)

        # 按总分降序
        results.sort(key=lambda r: r["total"], reverse=True)

        # ---- ⑦ 基本面质量 + 业绩事件因子(读本地表, 零额外网络调用) ----
        # 放在行业限配之前: 质量 filter 先剔除垃圾, 限配再在"质量达标"的池子里分组
        q_cfg = cfg.get("基本面因子", {})
        e_cfg = cfg.get("业绩事件", {})
        factor_enabled = bool(q_cfg.get("enabled") or e_cfg.get("enabled"))
        factor_info: dict[str, Any] = {"enabled": factor_enabled, "applied": False}
        if factor_enabled and apply_factors:
            from app.core.fundamentals import (
                apply_fundamental_factors,
                load_fundamentals_map,
                load_recent_events,
            )
            syms = [r["symbol"] for r in results]
            fund_map = load_fundamentals_map(syms)
            event_map = load_recent_events(syms, int(e_cfg.get("lookback_days", 90)))
            results, factor_stats = apply_fundamental_factors(
                results, fund_map, event_map, q_cfg, e_cfg,
            )
            factor_info.update({"applied": True, **factor_stats})
            logger.info(
                "基本面+事件因子生效: 候选%d -> %d(剔除%d, 无数据%d, 事件命中%d)",
                factor_stats.get("before", 0), factor_stats.get("after", 0),
                factor_stats.get("removed", 0), factor_stats.get("no_fundamental_data", 0),
                factor_stats.get("event_hits", 0),
            )
        elif factor_enabled and not apply_factors:
            factor_info["reason"] = "apply_factors=False 本次跳过"
            logger.info("apply_factors=False, 跳过基本面+事件因子")
        else:
            factor_info["reason"] = "配置未启用"
            logger.info("基本面+事件因子未启用(配置 enabled=false)")

        # ---- ⑤ 每行业限配(内存后处理, 零额外调用) ----
        cap_cfg = cfg.get("行业限配", {})
        cap_enabled = False
        cap_per = per_industry
        cap_level = industry_level
        cap_before = len(results)
        cap_after = len(results)
        cap_removed = 0
        cap_capped: list[dict[str, Any]] = []
        if per_industry <= 0 and cap_cfg.get("enabled"):
            cap_per = int(cap_cfg.get("per_industry", 0) or 0)
            cap_level = cap_cfg.get("level", "sw_l1") or "sw_l1"
            per_industry = cap_per
            industry_level = cap_level
        if per_industry > 0:
            cap_enabled = True
            class_map = load_classification_map([r["symbol"] for r in results])
            before = results
            results = apply_per_industry_cap(results, class_map, per_industry, industry_level)

            # 统计每组截断情况(验证/日志/汇总用)
            def _grp(r: dict[str, Any]) -> str:
                cls = class_map.get(r.get("symbol", ""))
                key = str(getattr(cls, industry_level, "") or getattr(cls, "industry", "") or "").strip()
                return key or "_未知_"
            before_groups = Counter(_grp(r) for r in before)
            after_groups = Counter(_grp(r) for r in results)
            capped = {k: (b, after_groups.get(k, 0))
                      for k, b in before_groups.items() if after_groups.get(k, 0) < b}
            cap_before = len(before)
            cap_after = len(results)
            cap_removed = len(before) - len(results)
            cap_capped = [{"industry": k, "before": b, "after": a}
                          for k, (b, a) in sorted(capped.items(), key=lambda x: -x[1][0])]
            if capped:
                detail = ", ".join(f"{c['industry']}:{c['before']}→{c['after']}" for c in cap_capped)
                logger.info("行业限配生效(level=%s, 每组%d): 移除%d只→剩%d只; 触顶组[%s]",
                            industry_level, per_industry, cap_removed, cap_after, detail)
            else:
                logger.info("行业限配生效(level=%s, 每组%d): 无组触顶, 剩%d只",
                            industry_level, per_industry, cap_after)
        else:
            logger.info("行业限配未启用(per_industry=%d) -> 不限制", per_industry)

        # ---- ④ 闸门乘子作用于 top_n ----
        effective_top_n = int(top_n * float(gate_info.get("multiplier", 1.0)))
        results = results[:effective_top_n]
        if gate_info.get("enabled"):
            for r in results:
                r["market_gate"] = {
                    "environment": gate_info["environment"],
                    "multiplier": gate_info["multiplier"],
                    "reason": gate_info["reason"],
                }

        # ---- 汇总本次扫描的闸门/限配结果, 供 GET /api/screener/last-scan-summary 读取 ----
        self.last_scan_summary = {
            "scanned_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "requested_top_n": top_n,
            "effective_top_n": effective_top_n,
            "final_count": len(results),
            "universe": uni_info,
            "factor": factor_info,
            "gate": {
                "enabled": bool(gate_info.get("enabled")),
                "applied": bool(apply_gate and gate_cfg.get("enabled")),
                "environment": gate_info["environment"],
                "multiplier": float(gate_info.get("multiplier", 1.0)),
                "reason": gate_info.get("reason", ""),
                "details": gate_info.get("details", []),
            },
            "cap": {
                "enabled": cap_enabled,
                "per_industry": cap_per,
                "level": cap_level,
                "before": cap_before,
                "after": cap_after,
                "removed": cap_removed,
                "capped_groups": cap_capped,
            },
        }
        return results
