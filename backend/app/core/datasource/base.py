"""数据源统一接口与公共模型.

设计要点(见方案 §3.1):
- 业务只依赖 DataSourceInterface, 不直接碰任何上游 SDK
- K线 DataFrame 统一列: [date, open, high, low, close, volume, amount]
- 周期统一映射: 1m/5m/15m/30m/60m/daily/weekly
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

# 统一周期 <-> 上游周期 由各源自行映射, 本常量仅作校验
SUPPORTED_PERIODS = ("1m", "5m", "15m", "30m", "60m", "daily", "weekly")

KLINE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


@dataclass
class Quote:
    """统一实时行情."""

    symbol: str
    name: str = ""
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0  # 手
    amount: float = 0.0  # 元
    change: float = 0.0
    change_pct: float = 0.0
    timestamp: str = ""

    @property
    def is_valid(self) -> bool:
        return self.price > 0


@dataclass
class StockInfo:
    """统一股票基础信息."""

    symbol: str
    name: str
    market: str = ""  # sh / sz / bj
    industry: str = ""  # 申万行业(东财 f100, 部分源可能为空)


def normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """把任意上游 DataFrame 规整为统一列 [date, open, high, low, close, volume, amount].

    - date 统一为 str
    - 缺失 amount 时用 volume*close 估算
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=KLINE_COLUMNS)
    out = pd.DataFrame()
    col_map = {"datetime": "date", "日期": "date", "时间": "date",
               "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
               "成交量": "volume", "成交额": "amount"}
    for src_col, std_col in col_map.items():
        if std_col not in df.columns and src_col in df.columns:
            df = df.rename(columns={src_col: std_col})
    for col in KLINE_COLUMNS:
        out[col] = df[col] if col in df.columns else float("nan")
    out["date"] = out["date"].astype(str)
    if out["amount"].isna().all() or out["amount"].eq(0).all():
        out["amount"] = out["volume"] * (out["close"] + out["open"]) / 2
    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["close"]).reset_index(drop=True)


def guess_market(symbol: str) -> str:
    """按 A 股代码规则猜测市场: 6/9 沪, 0/2/3 深, 4/8 北."""
    if not symbol:
        return ""
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("0", "2", "3")):
        return "sz"
    if symbol.startswith(("4", "8")):
        return "bj"
    return ""


class DataSourceInterface(ABC):
    """所有数据源必须实现该接口."""

    name: str = "base"
    label: str = ""  # 展示名

    @abstractmethod
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120) -> pd.DataFrame:
        """K线. 返回统一列 DataFrame, 按 date 升序."""

    @abstractmethod
    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        """实时行情(批量)."""

    @abstractmethod
    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        """股票列表: all / sh / sz / bj."""

    @abstractmethod
    async def health_check(self) -> bool:
        """探活: 成功返回 True."""


@dataclass
class HealthState:
    """数据源健康状态(方案 §3.2)."""

    consecutive_failures: int = 0
    last_success_time: float = 0.0
    avg_latency_ms: float = 0.0
    circuit_open_until: float = 0.0  # unix 时间戳, 0 表示未熔断
    request_count: int = 0
    success_count: int = 0

    def record(self, ok: bool, latency_ms: float, *, circuit_after: int = 3, circuit_secs: int = 600) -> None:
        self.request_count += 1
        if ok:
            self.success_count += 1
            self.consecutive_failures = 0
            self.last_success_time = latency_ms  # 实际用当前时间
            if self.avg_latency_ms <= 0:
                self.avg_latency_ms = latency_ms
            else:
                self.avg_latency_ms = self.avg_latency_ms * 0.7 + latency_ms * 0.3
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= circuit_after:
                import time

                self.circuit_open_until = time.time() + circuit_secs

    def is_circuit_open(self, now: float | None = None) -> bool:
        import time

        now = now or time.time()
        return self.circuit_open_until > now
