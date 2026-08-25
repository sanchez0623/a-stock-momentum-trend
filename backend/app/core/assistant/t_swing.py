"""盘前 LLM 做T波幅建议(P1).

流程:
- generate_premarket_swing(): 盘前对持仓股批量评估, LLM 输出每只股票当日
  建议做T触发波幅(%), 落库到 TSwingAdvice 表;
- get_today_swing(symbol): 盘中 SignalEngine._swing_threshold 读取, 优先于规则计算值.

设计要点:
- LLM 只在盘前跑一次, 不在盘中实时路径(零延迟);
- LLM 失败/未启用时静默降级到 P0 规则计算(ATR×市况);
- 建议值经 sanity 检查(0.5%~15% 区间钳位), 防幻觉;
- 每日每股一条, 重复生成覆盖旧值.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from sqlmodel import select

from app import db
from app.core.config import config_manager

logger = logging.getLogger(__name__)

TZ = dt.timezone(dt.timedelta(hours=8))

# 建议 prompt 版本(修改提示词时必须递增)
T_SWING_PROMPT_V = "t-swing-2026-08-25-v1"

# 建议值合理区间(百分比), 超出按边界钳位(防 LLM 幻觉)
SWING_MIN, SWING_MAX = 0.5, 15.0


def _now() -> str:
    return dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return dt.datetime.now(TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 数据模型
from app.models.models import TSwingAdvice  # noqa: E402


# ---------------------------------------------------------------- 读取(盘中热路径)
def get_today_swing(symbol: str) -> float | None:
    """读取今日该股的 LLM 做T波幅建议. 无建议/异常返回 None(调用方走规则计算)."""
    if not symbol:
        return None
    try:
        with db.session_scope() as s:
            row = s.exec(
                select(TSwingAdvice).where(
                    TSwingAdvice.date == _today(),
                    TSwingAdvice.symbol == symbol,
                )
            ).first()
            if row and row.swing_pct > 0:
                return float(row.swing_pct)
    except Exception:
        pass
    return None


def list_today_advices() -> list[dict[str, Any]]:
    """列出今日全部做T建议(前端/调试用)."""
    try:
        with db.session_scope() as s:
            rows = s.exec(
                select(TSwingAdvice).where(TSwingAdvice.date == _today())
            ).all()
            return [r.model_dump() for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------- 生成(盘前)
async def generate_premarket_swing(symbols: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """盘前生成做T波幅建议.

    Args:
        symbols: [(symbol, name), ...]; None 时自动取持仓+自选.

    Returns:
        {"total": n, "ok": m, "items": [{symbol, swing_pct, rationale}]}
    """
    cfg = config_manager.get()
    t_cfg = cfg.get("做T", {})
    if not t_cfg.get("llm_swing_enabled", False):
        return {"total": 0, "ok": 0, "items": [], "skipped": "llm_swing 未启用"}
    llm_cfg = cfg.get("llm", {})
    if not (llm_cfg.get("enabled") and llm_cfg.get("api_key")):
        return {"total": 0, "ok": 0, "items": [], "skipped": "LLM 未配置"}

    # 取标的池(默认持仓+自选)
    if symbols is None:
        from app.core.position import position_manager
        from app.models.models import Watchlist

        with db.session_scope() as s:
            pos_list = position_manager.list_positions(s)
            wl = s.exec(select(Watchlist)).all()
            symbols = sorted({(p.symbol, p.name) for p in pos_list} | {(w.symbol, w.name) for w in wl})

    if not symbols:
        return {"total": 0, "ok": 0, "items": [], "skipped": "无监控标的"}

    # 收集每只票的波动特征素材(近20日日内振幅分布 + ATR%)
    stats = await _collect_swing_stats([s for s, _ in symbols])
    if not stats:
        return {"total": len(symbols), "ok": 0, "items": [], "error": "波动素材获取失败"}

    # LLM 一次调用覆盖整批
    items = await _llm_swing_batch(stats, llm_cfg)

    # 落库(每股每日一条, 覆盖旧值)
    ok = 0
    with db.session_scope() as s:
        for it in items:
            sym = it.get("symbol", "")
            if not sym:
                continue
            raw = float(it.get("swing_pct", 0) or 0)
            if raw <= 0:
                continue
            # sanity 钳位
            val = min(SWING_MAX, max(SWING_MIN, raw))
            row = s.exec(
                select(TSwingAdvice).where(
                    TSwingAdvice.date == _today(), TSwingAdvice.symbol == sym)
            ).first()
            if row is None:
                row = TSwingAdvice(date=_today(), symbol=sym)
            row.name = next((n for sy, n in symbols if sy == sym), "")
            row.swing_pct = round(val, 2)
            row.rationale = str(it.get("rationale", ""))[:200]
            row.model = str(llm_cfg.get("model", ""))
            row.created_at = _now()
            s.add(row)
            ok += 1
        s.commit()

    logger.info("做T波幅建议已生成: %d/%d", ok, len(symbols))
    return {"total": len(symbols), "ok": ok, "items": items}


async def _collect_swing_stats(symbols: list[str]) -> list[dict[str, Any]]:
    """每只票的波动素材: 近20日日均振幅/最大振幅/ATR%/近5日振幅."""
    from app.core.datasource import data_source_manager
    from app.core.indicators import compute_all

    out: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            df = await data_source_manager.get_kline(sym, "daily", 60)
            if df is None or len(df) < 25:
                continue
            ind = compute_all(df)
            last = ind.iloc[-1]
            close = float(last["close"])
            if close <= 0:
                continue
            # 日内振幅序列
            amp = ((df["high"] - df["low"]) / df["close"] * 100).tail(20)
            atr_pct = float(last.get("atr14", 0) or 0) / close * 100
            out.append({
                "symbol": sym,
                "avg_amp_20d": round(float(amp.mean()), 2),
                "max_amp_20d": round(float(amp.max()), 2),
                "atr_pct": round(atr_pct, 2),
                "recent_amp_5d": round(float(amp.tail(5).mean()), 2),
            })
        except Exception:
            continue
    return out


async def _llm_swing_batch(stats: list[dict], llm_cfg: dict) -> list[dict[str, Any]]:
    """LLM 批量生成建议(失败返回空列表, 调用方走规则降级)."""
    try:
        from pydantic import BaseModel, Field

        from app.core.ai_review.chain import _call_parsed, build_chain_llm

        class SwingItem(BaseModel):
            symbol: str = Field(description="股票代码")
            swing_pct: float = Field(description="建议做T触发波幅(%), 0.5-15 之间")
            rationale: str = Field(description="一句话理由")

        class SwingOutput(BaseModel):
            items: list[SwingItem] = Field(description="逐股建议")

        lines = "\n".join(
            f"- {s['symbol']}: 近20日日均振幅{s['avg_amp_20d']}%, 最大{s['max_amp_20d']}%, "
            f"ATR%={s['atr_pct']}, 近5日均幅{s['recent_amp_5d']}%"
            for s in stats)
        llm = build_chain_llm(llm_cfg)
        out = await _call_parsed(
            system="你是 A 股日内做T参数顾问。根据每只股票的历史波动特征, 给出当日做T的"
                   "最小触发波幅建议(%): 波动大的股票阈值放宽(避免频繁无效做T), "
                   "波动小的收紧。输出必须基于给定数据, 不要编造。",
            user=(f"各股波动特征:\n{lines}\n\n"
                  f"参考: 阈值过高做T机会少, 过低则手续费吞噬利润。建议在 日均振幅的 "
                  f"60%-90% 区间内取值, 结合 ATR 与近期趋势微调。"),
            llm=llm, parser=_build_parser(SwingOutput),
            feature="assistant.t_swing", prompt_version=T_SWING_PROMPT_V,
        )
        return [it.model_dump() for it in out.items]
    except Exception:
        logger.warning("LLM 做T建议生成失败, 走规则降级", exc_info=True)
        return []


def _build_parser(pydantic_obj):
    """延迟构建 PydanticOutputParser(与 ai_review.chain 用法一致)."""
    from langchain_core.output_parsers import PydanticOutputParser

    return PydanticOutputParser(pydantic_object=pydantic_obj)
