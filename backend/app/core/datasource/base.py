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


@dataclass
class Fundamental:
    """统一季度基本面快照(可选能力 'fundamental').

    字段全部可空: 不同源覆盖度不同, 缺失即 None, 由业务层按需降级.
    百分比字段统一为 "百分数"口径(如 roe=12.5 表示 12.5%).
    """

    symbol: str
    stat_date: str = ""   # 报告期(如 2026-06-30)
    pub_date: str = ""    # 披露日
    roe: float | None = None                  # 净资产收益率(%)
    np_margin: float | None = None            # 销售净利率(%)
    gp_margin: float | None = None            # 销售毛利率(%)
    eps_ttm: float | None = None              # 每股收益 TTM
    yoy_ni: float | None = None               # 归母净利润同比(%)
    yoy_eps: float | None = None              # 每股收益同比(%)
    yoy_equity: float | None = None           # 净资产同比(%)
    liability_to_asset: float | None = None   # 资产负债率(%)
    current_ratio: float | None = None        # 流动比率
    cfo_to_np: float | None = None            # 经营现金流/净利润(盈利含金量)
    dupont_roe: float | None = None           # 杜邦 ROE
    pe_ttm: float | None = None               # 市盈率 TTM
    pb_mrq: float | None = None               # 市净率 MRQ
    ps_ttm: float | None = None               # 市销率 TTM
    is_st: bool | None = None                 # 是否 ST
    industry: str = ""                        # 行业(源自带, 如证监会行业)
    industry_source: str = ""                 # 行业分类体系名


@dataclass
class EarningsEventItem:
    """业绩预告 / 业绩快报事件(可选能力 'earnings')."""

    symbol: str
    kind: str = "forecast"       # forecast 预告 / express 快报
    pub_date: str = ""           # 披露日
    stat_date: str = ""          # 报告期
    forecast_type: str = ""      # 预增/略增/扭亏/预减/预亏/续盈...
    chg_pct_up: float | None = None    # 预告变动上限(%)
    chg_pct_down: float | None = None  # 预告变动下限(%)
    abstract: str = ""           # 摘要


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
    """所有数据源必须实现该接口.

    除 4 个必选抽象方法外, 另有一组"可选能力"(见下方 supports/扩展方法):
    只有声明了该能力的源才会被 DataSourceManager 路由到, 其余源零改动.
    """

    name: str = "base"
    label: str = ""  # 展示名

    # 可选能力声明. 取值见 CAPABILITIES; 空元组 = 只提供基础四件套
    supports: tuple[str, ...] = ()
    # 是否参与实时行情轮询(盘后型数据源应置 False, 避免占用尝试位)
    supports_realtime: bool = True

    def supports_period(self, period: str) -> bool:
        """是否支持该周期. 不支持时 Manager 会直接跳过, 不计入失败/熔断.

        盘后型数据源(如 Baostock)应关闭分钟线, 避免把隔夜陈旧分钟 K 写进缓存.
        """
        return True

    @abstractmethod
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None) -> pd.DataFrame:
        """K线. 返回统一列 DataFrame, 按 date 升序.

        secid: 可选, 显式 secid(如东财指数 "1.000001"). 提供时跳过按代码前缀推断交易所,
        用于拉取指数 K 线(普通代码前缀规则对指数会错位).
        """

    @abstractmethod
    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        """实时行情(批量)."""

    @abstractmethod
    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        """股票列表: all / sh / sz / bj."""

    @abstractmethod
    async def health_check(self) -> bool:
        """探活: 成功返回 True."""

    # ------------------------------------------------------------ 可选能力
    # 默认全部 NotImplementedError, Manager 会自动跳过 -> 现有源无需任何改动.
    async def get_index_constituents(self, index_key: str) -> list[StockInfo]:
        """指数成分股. index_key: hs300 / zz500 / sz50. 能力名 'constituents'."""
        raise NotImplementedError(f"{self.name} 不支持指数成分股")

    async def get_industry_map(self, symbols: list[str] | None = None) -> dict[str, dict[str, str]]:
        """全市场行业映射 {symbol: {name, industry, classification}}. 能力名 'industry'."""
        raise NotImplementedError(f"{self.name} 不支持行业分类")

    async def get_fundamentals(self, symbol: str, *, full: bool = False) -> Fundamental | None:
        """单只股票最近一期基本面. full=True 额外取杜邦/现金流. 能力名 'fundamental'."""
        raise NotImplementedError(f"{self.name} 不支持基本面")

    async def get_earnings_events(self, symbol: str, start_date: str, end_date: str) -> list[EarningsEventItem]:
        """区间内业绩预告 + 业绩快报. 能力名 'earnings'."""
        raise NotImplementedError(f"{self.name} 不支持业绩事件")

    async def get_index_kline_by_code(self, index_code: str, period: str = "daily", count: int = 250) -> pd.DataFrame:
        """按本源自有指数代码取指数 K 线. 能力名 'index_kline'."""
        raise NotImplementedError(f"{self.name} 不支持指数 K 线")


# 可选能力清单(仅作文档与校验用)
CAPABILITIES = ("constituents", "industry", "fundamental", "earnings", "index_kline")


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
