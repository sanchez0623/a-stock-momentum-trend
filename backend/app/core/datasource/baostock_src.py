"""Baostock 数据源(免费无注册, 日线级稳定备份 + 独家增强能力).

定位(重要):
- **不做实时行情**: Baostock 是盘后下载型数据, 盘中不更新 -> supports_realtime=False
- **不做分钟线**: 隔夜陈旧分钟 K 会污染缓存 -> supports_period 只放行 daily/weekly
- **做日线备份**: 免费、无注册、无东财那种连接级风控, 排在东财之前做日 K 兜底
- **做能力增强**(其它源都没有):
    constituents  沪深300 / 中证500 / 上证50 成分股  -> 选股 universe 预筛
    industry      证监会行业分类                      -> 行业标签/限配兜底
    fundamental   ROE/毛利/负债率/增速/杜邦 + 估值     -> 动量 + 质量
    earnings      业绩预告 / 业绩快报                 -> 事件催化
    index_kline   指数日线                            -> 大盘 regime 兜底

线程安全: baostock 是模块级全局 socket, 非线程安全.
本模块用进程级 _LOCK 把所有调用串行化, 再用 asyncio.to_thread 包装成协程,
因此并发调用是安全的(代价是吞吐受限, 属可接受: 这些数据都走后台刷新任务).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import logging
import re
import threading

import pandas as pd

from app.core.datasource.base import (
    DataSourceInterface,
    EarningsEventItem,
    Fundamental,
    Quote,
    StockInfo,
    normalize_kline,
)

logger = logging.getLogger(__name__)

try:
    import baostock as _bs

    BAOSTOCK_OK = True
except ImportError:  # pragma: no cover - 可选依赖
    _bs = None
    BAOSTOCK_OK = False

# ---------------------------------------------------------------- 常量
DAILY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "adjustflag,turn,tradestatus,pctChg,isST,peTTM,pbMRQ,psTTM,pcfNcfTTM"
)
WEEKLY_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg"
INDEX_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"

FREQ_MAP = {"daily": "d", "weekly": "w"}

# 成分股接口
CONSTITUENT_FUNCS = {
    "hs300": "query_hs300_stocks",
    "zz500": "query_zz500_stocks",
    "sz50": "query_sz50_stocks",
}

# 东财 secid -> Baostock 指数代码候选(按序尝试, 兼容配置里已有的 secid 写法)
EM_SECID_TO_BS: dict[str, tuple[str, ...]] = {
    "1.000001": ("sh.000001",),            # 上证指数
    "0.399001": ("sz.399001",),            # 深证成指
    "0.399006": ("sz.399006",),            # 创业板指
    "0.399005": ("sz.399005",),            # 中小板指
    "1.000300": ("sh.000300", "sz.399300"),  # 沪深300
    "0.000300": ("sh.000300", "sz.399300"),  # 沪深300(配置里的深市写法, 兼容)
    "1.000016": ("sh.000016",),            # 上证50
    "1.000905": ("sh.000905", "sz.399905"),  # 中证500
    "1.000852": ("sh.000852",),            # 中证1000
    "1.000688": ("sh.000688",),            # 科创50
}

# 需要 ×100 换算成"百分数"口径的字段(baostock 返回的是小数比率)
_PCT_FIELDS = ("roeAvg", "npMargin", "gpMargin", "YOYNI", "YOYEPSBasic",
               "YOYEquity", "liabilityToAsset", "dupontROE")

_LOCK = threading.RLock()
_LOGGED_IN = False


# ---------------------------------------------------------------- 代码转换
def to_bs_code(symbol: str) -> str:
    """任意形态代码 -> baostock 代码(sh.600000 / sz.000001 / bj.8xxxxx).

    支持: 6 位纯数字 / sh.600000 / sz.000001 / 600000.SH / SH600000 等写法。
    北交所(4/8 开头)baostock 无覆盖, 返回空串, 调用方应跳过。
    """
    s = (symbol or "").strip().lower()
    if not s:
        return ""
    if s.startswith(("sh.", "sz.", "bj.")):
        return s
    # 去掉可能的字母前缀 / 尾缀(.sh/.sz) / 其它点
    s = re.sub(r"^(sh|sz|bj)", "", s)
    s = re.sub(r"\.(sh|sz|bj)$", "", s)
    s = s.replace(".", "")
    if len(s) != 6 or not s.isdigit():
        return ""
    if s.startswith(("6", "9")):
        return f"sh.{s}"
    if s.startswith(("0", "2", "3")):
        return f"sz.{s}"
    if s.startswith(("4", "8")):
        return f"bj.{s}"  # 北交所, baostock 取不到, 调用方得空结果
    return ""


def from_bs_code(code: str) -> str:
    """baostock 代码 -> 6 位代码."""
    return (code or "").split(".")[-1].strip()


_VALID_BS_RE = re.compile(r"^(sh|sz|bj)\.\d{6}$")


def _valid_bs_code(code: str) -> bool:
    """baostock 是否接受该代码(9 位 sh./sz./bj. + 6 位数字). 用于调用前闸门."""
    return bool(_VALID_BS_RE.match(code or ""))


def secid_to_bs_codes(secid: str) -> tuple[str, ...]:
    """东财 secid / baostock 指数代码 -> baostock 指数代码候选列表."""
    if not secid:
        return ()
    s = secid.strip().lower()
    # 已经是 baostock 指数代码(sh.000300), 原样返回(避免被错误重写成 sz.000300)
    if s.startswith(("sh.", "sz.", "bj.")):
        return (s,)
    if s in EM_SECID_TO_BS:
        return EM_SECID_TO_BS[s]
    if "." not in s:
        return ()
    mkt, code = s.split(".", 1)
    # 通用兜底: 000xxx 归沪, 399xxx 归深
    if code.startswith("000"):
        return (f"sh.{code}",)
    if code.startswith("399"):
        return (f"sz.{code}",)
    return (f"{'sh' if mkt == '1' else 'sz'}.{code}",)


# ---------------------------------------------------------------- 底层调用
def _ensure_login() -> None:
    """懒登录. baostock 登录态是进程级全局, 只需一次."""
    global _LOGGED_IN
    if _LOGGED_IN:
        return
    # baostock 会往 stdout 打 "login success!", 吞掉保持日志干净
    with contextlib.redirect_stdout(io.StringIO()):
        r = _bs.login()
    if getattr(r, "error_code", "1") != "0":
        raise RuntimeError(f"baostock 登录失败[{r.error_code}]: {r.error_msg}")
    _LOGGED_IN = True


def _relogin() -> None:
    global _LOGGED_IN
    with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(Exception):
        _bs.logout()
    _LOGGED_IN = False
    _ensure_login()


def _rs_to_df(rs) -> pd.DataFrame:
    """ResultData -> DataFrame. error_code != 0 抛异常, 交由上层重试/降级."""
    if rs is None:
        return pd.DataFrame()
    if getattr(rs, "error_code", "1") != "0":
        raise RuntimeError(f"baostock 查询失败[{rs.error_code}]: {rs.error_msg}")
    rows: list[list[str]] = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=list(rs.fields))


def _call_sync(fn_name: str, *args, **kwargs) -> pd.DataFrame:
    """串行执行 baostock 同步调用, 失败自动重登一次再试."""
    with _LOCK:
        _ensure_login()
        fn = getattr(_bs, fn_name)
        try:
            return _rs_to_df(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock %s 首次失败, 重登后重试: %s", fn_name, exc)
            _relogin()
            return _rs_to_df(fn(*args, **kwargs))


def fetch_daily_range(symbol: str, start_date: str, end_date: str,
                      adjustflag: str = "2", secid: str | None = None) -> pd.DataFrame:
    """回测专用: 按起止日期拉取日线(同步, 内部串行锁+自动重登).

    - 与 get_kline(按 count 反推)不同: 区间精确可控, 供 backtest_kline 冻结快照补拉;
    - adjustflag: 2=前复权(回测默认口径), 3=后复权(预留);
    - secid: 东财 secid(指数, 如 0.000300), 内部转 baostock 指数代码;
    - 返回统一列 DataFrame(date/open/high/low/close/volume/amount) 升序,
      已剔除停牌占位行; 代码非法/无数据返回空 DataFrame(不抛异常, 调用方降级).
    """
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    if not start_date or not end_date or start_date >= end_date:
        return empty
    if secid:
        codes = secid_to_bs_codes(secid)
        fields = INDEX_FIELDS
    else:
        code = to_bs_code(symbol)
        codes = (code,) if code else ()
        fields = DAILY_FIELDS
    # 格式闸门: 绝不把非法代码喂给 baostock(否则它会刷屏 "股票代码应为9位")
    codes = tuple(c for c in codes if _valid_bs_code(c))
    if not codes:
        return empty
    df = pd.DataFrame()
    for code in codes:
        try:
            df = _call_sync(
                "query_history_k_data_plus", code, fields,
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag=adjustflag,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock 区间拉取失败 %s: %s", code, exc)
            df = pd.DataFrame()
        if not df.empty:
            break
    if df.empty:
        return empty
    # 停牌日 baostock 会给 volume=0 的占位行, 剔除避免污染量能指标
    if "tradestatus" in df.columns:
        df = df[df["tradestatus"].astype(str) == "1"]
    return normalize_kline(df)


def _num(v, scale: float = 1.0) -> float | None:
    """字符串 -> float. 空串/非法值返回 None(区别于 0, 避免把缺失当成真实 0)."""
    try:
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "none", "null"):
            return None
        return round(float(s) * scale, 4)
    except (TypeError, ValueError):
        return None


def _latest_quarters(n: int = 4) -> list[tuple[int, int]]:
    """由今天倒推最近 n 个可能已披露的报告期 [(year, quarter), ...].

    A 股披露节奏: Q1→4月底, Q2→8月底, Q3→10月底, Q4→次年4月底.
    这里不做精确判断, 直接从"当前自然季度"往前列, 由调用方逐个试到有数为止.
    """
    today = dt.date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out: list[tuple[int, int]] = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


class BaostockSource(DataSourceInterface):
    """Baostock 源: 日/周 K 线备份 + 成分股/行业/基本面/业绩事件/指数线."""

    name = "baostock"
    label = "Baostock"
    supports = ("constituents", "industry", "fundamental", "earnings", "index_kline")
    supports_realtime = False  # 盘后型数据源, 不参与实时行情轮询

    def __init__(self) -> None:
        if not BAOSTOCK_OK:
            raise RuntimeError("baostock 未安装: pip install baostock")

    def supports_period(self, period: str) -> bool:
        """只放行日线/周线. 分钟线由 mootdx/腾讯负责(baostock 盘中不更新)."""
        return period in FREQ_MAP

    async def _call(self, fn_name: str, *args, **kwargs) -> pd.DataFrame:
        return await asyncio.to_thread(_call_sync, fn_name, *args, **kwargs)

    # ------------------------------------------------------------ 基础四件套
    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120,
                        secid: str | None = None) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        if period not in FREQ_MAP:
            return empty

        if secid:
            codes = secid_to_bs_codes(secid)
            fields = INDEX_FIELDS
        else:
            code = to_bs_code(symbol)
            codes = (code,) if code else ()
            fields = DAILY_FIELDS if period == "daily" else WEEKLY_FIELDS
        # 格式闸门: 绝不把非法代码喂给 baostock(否则它会刷屏 "股票代码应为9位")
        codes = tuple(c for c in codes if _valid_bs_code(c))
        if not codes:
            logger.warning(
                "baostock 跳过非法代码(不会请求 baostock): symbol=%r secid=%r raw=%r",
                symbol, secid, secid_to_bs_codes(secid) if secid else to_bs_code(symbol),
            )
            return empty

        span_days = int(count * (2.0 if period == "daily" else 9.0)) + 40
        start = (dt.date.today() - dt.timedelta(days=span_days)).strftime("%Y-%m-%d")
        end = dt.date.today().strftime("%Y-%m-%d")

        df = pd.DataFrame()
        for code in codes:
            try:
                df = await self._call(
                    "query_history_k_data_plus", code, fields,
                    start_date=start, end_date=end,
                    frequency=FREQ_MAP[period],
                    adjustflag="2",  # 2=前复权, 与 akshare qfq 口径一致
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("baostock K线失败 %s: %s", code, exc)
                df = pd.DataFrame()
            if not df.empty:
                break
        if df.empty:
            return empty
        # 停牌日 baostock 会给 volume=0 的占位行, 剔除避免污染量能指标
        if "tradestatus" in df.columns:
            df = df[df["tradestatus"].astype(str) == "1"]
        return normalize_kline(df.tail(count))

    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        """Baostock 无实时行情. 返回空列表(不抛异常, 避免被误判为故障)."""
        return []

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        """某交易日全部证券(含停牌状态). 用最近交易日, 过滤掉指数与非 A 股."""
        day = await self._latest_trade_date()
        df = await self._call("query_all_stock", day=day)
        if df.empty:
            return []
        out: list[StockInfo] = []
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            sym = from_bs_code(code)
            if len(sym) != 6 or not sym.isdigit():
                continue
            # 指数代码(000xxx 在沪 / 399xxx 在深)剔除
            if code.startswith("sh.000") or code.startswith("sz.399"):
                continue
            mkt = "sh" if code.startswith("sh.") else "sz"
            if market != "all" and mkt != market:
                continue
            out.append(StockInfo(symbol=sym, name=str(r.get("code_name", "")), market=mkt))
        return out

    async def health_check(self) -> bool:
        try:
            end = dt.date.today()
            df = await self._call(
                "query_trade_dates",
                start_date=(end - dt.timedelta(days=10)).strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            return not df.empty
        except Exception:  # noqa: BLE001
            return False

    async def _latest_trade_date(self) -> str:
        """最近一个交易日(YYYY-MM-DD). 失败时退回今天."""
        end = dt.date.today()
        try:
            df = await self._call(
                "query_trade_dates",
                start_date=(end - dt.timedelta(days=20)).strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            days = df[df["is_trading_day"].astype(str) == "1"]["calendar_date"].tolist()
            if days:
                return str(days[-1])
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock 交易日历失败: %s", exc)
        return end.strftime("%Y-%m-%d")

    # ------------------------------------------------------------ 可选能力
    async def get_index_constituents(self, index_key: str) -> list[StockInfo]:
        """指数成分股: hs300 / zz500 / sz50."""
        fn = CONSTITUENT_FUNCS.get(index_key)
        if fn is None:
            raise ValueError(f"未知指数: {index_key}(支持 {list(CONSTITUENT_FUNCS)})")
        df = await self._call(fn)
        if df.empty:
            return []
        out: list[StockInfo] = []
        for _, r in df.iterrows():
            sym = from_bs_code(str(r.get("code", "")))
            if len(sym) == 6:
                out.append(StockInfo(symbol=sym, name=str(r.get("code_name", ""))))
        return out

    async def get_industry_map(self, symbols: list[str] | None = None) -> dict[str, dict[str, str]]:
        """全市场行业映射(证监会行业分类). 一次调用拿全量, 之后本地查表."""
        df = await self._call("query_stock_industry")
        if df.empty:
            return {}
        want = set(symbols) if symbols else None
        out: dict[str, dict[str, str]] = {}
        for _, r in df.iterrows():
            sym = from_bs_code(str(r.get("code", "")))
            if len(sym) != 6 or (want is not None and sym not in want):
                continue
            out[sym] = {
                "name": str(r.get("code_name", "")),
                "industry": str(r.get("industry", "")).strip(),
                "classification": str(r.get("industryClassification", "")).strip(),
            }
        return out

    async def get_fundamentals(self, symbol: str, *, full: bool = False) -> Fundamental | None:
        """最近一期已披露的季度基本面 + 最新估值.

        逐个报告期回退试探(最多 4 期), 找到有 profit 数据的那期作为基准,
        再用同一期取 growth / balance(可选 dupont / cashflow), 保证口径一致.
        """
        code = to_bs_code(symbol)
        if not code:
            return None

        profit = pd.DataFrame()
        year = quarter = 0
        for y, q in _latest_quarters(4):
            try:
                profit = await self._call("query_profit_data", code=code, year=y, quarter=q)
            except Exception as exc:  # noqa: BLE001
                logger.debug("baostock profit 失败 %s %s-Q%s: %s", code, y, q, exc)
                profit = pd.DataFrame()
            if not profit.empty:
                year, quarter = y, q
                break
        if profit.empty:
            return None

        p = profit.iloc[-1]
        fund = Fundamental(
            symbol=symbol,
            stat_date=str(p.get("statDate", "")),
            pub_date=str(p.get("pubDate", "")),
            roe=_num(p.get("roeAvg"), 100),
            np_margin=_num(p.get("npMargin"), 100),
            gp_margin=_num(p.get("gpMargin"), 100),
            eps_ttm=_num(p.get("epsTTM")),
        )

        async def _one(fn: str) -> pd.Series | None:
            try:
                d = await self._call(fn, code=code, year=year, quarter=quarter)
                return d.iloc[-1] if not d.empty else None
            except Exception as exc:  # noqa: BLE001
                logger.debug("baostock %s 失败 %s: %s", fn, code, exc)
                return None

        g = await _one("query_growth_data")
        if g is not None:
            fund.yoy_ni = _num(g.get("YOYNI"), 100)
            fund.yoy_eps = _num(g.get("YOYEPSBasic"), 100)
            fund.yoy_equity = _num(g.get("YOYEquity"), 100)

        b = await _one("query_balance_data")
        if b is not None:
            fund.liability_to_asset = _num(b.get("liabilityToAsset"), 100)
            fund.current_ratio = _num(b.get("currentRatio"))

        if full:
            c = await _one("query_cash_flow_data")
            if c is not None:
                fund.cfo_to_np = _num(c.get("CFOToNP"))
            d = await _one("query_dupont_data")
            if d is not None:
                fund.dupont_roe = _num(d.get("dupontROE"), 100)

        val = await self.get_valuation(symbol)
        if val:
            fund.pe_ttm = val.get("pe_ttm")
            fund.pb_mrq = val.get("pb_mrq")
            fund.ps_ttm = val.get("ps_ttm")
            is_st = val.get("is_st")
            fund.is_st = is_st if isinstance(is_st, bool) else None
        return fund

    async def get_valuation(self, symbol: str) -> dict[str, float | bool | None]:
        """最新交易日估值快照: peTTM / pbMRQ / psTTM / isST(一次日线查询即可)."""
        code = to_bs_code(symbol)
        if not code:
            return {}
        start = (dt.date.today() - dt.timedelta(days=20)).strftime("%Y-%m-%d")
        try:
            df = await self._call(
                "query_history_k_data_plus", code,
                "date,code,close,peTTM,pbMRQ,psTTM,isST,tradestatus",
                start_date=start, end_date=dt.date.today().strftime("%Y-%m-%d"),
                frequency="d", adjustflag="3",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock 估值失败 %s: %s", code, exc)
            return {}
        if df.empty:
            return {}
        r = df.iloc[-1]
        return {
            "pe_ttm": _num(r.get("peTTM")),
            "pb_mrq": _num(r.get("pbMRQ")),
            "ps_ttm": _num(r.get("psTTM")),
            "is_st": str(r.get("isST", "0")).strip() == "1",
        }

    async def get_earnings_events(self, symbol: str, start_date: str, end_date: str) -> list[EarningsEventItem]:
        """区间内业绩预告 + 业绩快报."""
        code = to_bs_code(symbol)
        if not code:
            return []
        out: list[EarningsEventItem] = []

        try:
            fc = await self._call("query_forecast_report", code=code,
                                  start_date=start_date, end_date=end_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock 业绩预告失败 %s: %s", code, exc)
            fc = pd.DataFrame()
        for _, r in fc.iterrows():
            out.append(EarningsEventItem(
                symbol=symbol,
                kind="forecast",
                pub_date=str(r.get("profitForcastExpPubDate", "")),
                stat_date=str(r.get("profitForcastExpStatDate", "")),
                forecast_type=str(r.get("profitForcastType", "")).strip(),
                chg_pct_up=_num(r.get("profitForcastChgPctUp")),
                chg_pct_down=_num(r.get("profitForcastChgPctDwn")),
                abstract=str(r.get("profitForcastAbstract", ""))[:200],
            ))

        try:
            ex = await self._call("query_performance_express_report", code=code,
                                  start_date=start_date, end_date=end_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("baostock 业绩快报失败 %s: %s", code, exc)
            ex = pd.DataFrame()
        for _, r in ex.iterrows():
            gr = _num(r.get("performanceExpressGRYOY"))   # 营收同比(%)
            op = _num(r.get("performanceExpressOPYOY"))   # 净利同比(%)
            out.append(EarningsEventItem(
                symbol=symbol,
                kind="express",
                pub_date=str(r.get("performanceExpPubDate", "")),
                stat_date=str(r.get("performanceExpStatDate", "")),
                forecast_type="快报",
                chg_pct_up=op,
                chg_pct_down=op,
                abstract=f"快报: 营收同比{gr if gr is not None else '-'}%, 净利同比{op if op is not None else '-'}%",
            ))
        return out

    async def get_index_kline_by_code(self, index_code: str, period: str = "daily",
                                      count: int = 250) -> pd.DataFrame:
        """按 baostock 指数代码(如 sh.000300)取指数 K 线."""
        return await self.get_kline("", period=period, count=count, secid=index_code)
