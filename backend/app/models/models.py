"""SQLModel 表定义(方案 §5)."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 基础
class Stock(SQLModel, table=True):
    """股票代码/名称/市场/行业."""

    symbol: str = Field(primary_key=True)
    name: str = Field(default="", index=True)
    market: str = Field(default="", index=True)  # sh/sz/bj
    industry: str = Field(default="")


class ConfigRow(SQLModel, table=True):
    """全局配置(单行 JSON)."""

    id: int | None = Field(default=1, primary_key=True)
    data_json: str = Field(default="{}")
    updated_at: str = Field(default_factory=_now)


class KlineCache(SQLModel, table=True):
    """K线缓存 (symbol, period, date, ohlcv)."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    period: str = Field(index=True)
    date: str = Field(default="")  # 最后一条日期, 用于增量判断
    ohlcv_json: str = Field(default="[]")


# ---------------------------------------------------------------- 交易
class Position(SQLModel, table=True):
    """持仓快照."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    qty: int = Field(default=0)  # 股
    cost: float = Field(default=0.0)  # 持仓成本(加权)
    status: str = Field(default="holding", index=True)  # holding / closed
    updated_at: str = Field(default_factory=_now)


class SignalRecord(SQLModel, table=True):
    """信号记录."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    type: str = Field(index=True)  # BUY_FIRST/BUY_ADD/SELL_REDUCE/SELL_STOP/T_BUY/T_SELL
    direction: str = Field(default="")  # buy/sell
    strength: float = Field(default=0.0)  # 0-100
    reason: str = Field(default="")
    indicators_json: str = Field(default="{}")


class Plan(SQLModel, table=True):
    """交易计划."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    action: str = Field(default="")  # buy_add / sell_reduce / stop / hold / t_trade
    content: str = Field(default="")  # 人话指引
    status: str = Field(default="pending", index=True)  # pending / done / ignored


class Trade(SQLModel, table=True):
    """成交记录(手动回填)."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    action: str = Field(index=True)  # buy/sell
    price: float = Field(default=0.0)
    qty: int = Field(default=0)
    amount: float = Field(default=0.0)
    reason: str = Field(default="")
    signal_strength: float = Field(default=0.0)
    plan_id: int | None = Field(default=None)
    pnl: float = Field(default=0.0)
    score: float | None = Field(default=None)
    note: str = Field(default="")


class RiskState(SQLModel, table=True):
    """风控状态(单行)."""

    id: int | None = Field(default=1, primary_key=True)
    day_loss_tripped: bool = Field(default=False)  # 日亏损熔断
    defense_mode: bool = Field(default=False)  # 防守模式(回撤)
    consecutive_losses: int = Field(default=0)
    last_trade_pnl: float = Field(default=0.0)
    day_pnl: float = Field(default=0.0)
    updated_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------- 复盘与评分
class AiReview(SQLModel, table=True):
    """AI 复盘记录."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    range: str = Field(default="")  # 复盘范围, 如 week:2026-08-03
    content: str = Field(default="")  # 复盘正文(规则诊断 + LLM)
    suggestions_json: str = Field(default="[]")  # [{text, status}]
    model: str = Field(default="")
    rule_result_json: str = Field(default="{}")


class Score(SQLModel, table=True):
    """评分."""

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(index=True)  # trade / week / month / health
    scope_id: str = Field(default="", index=True)  # 关联 id 或周期标识
    dims_json: str = Field(default="{}")  # 各维度得分
    total: float = Field(default=0.0)
    created_at: str = Field(default_factory=_now)


class DailyStat(SQLModel, table=True):
    """每日净值/回撤快照."""

    date: str = Field(primary_key=True)
    equity: float = Field(default=0.0)
    drawdown: float = Field(default=0.0)
    day_pnl: float = Field(default=0.0)
