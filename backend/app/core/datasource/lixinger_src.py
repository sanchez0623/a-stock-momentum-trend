"""理杏仁开放平台数据源(申万2021行业分级 + 日线 K 线兜底).

设计:
- 申万2021 行业分级: 只被 classification 模块显式直连(get_sw_classification).
- 日线 K 线(cn/company/candlestick, 前复权): 参与 manager 通用取数路由,
  仅支持 daily 周期, 作为前面免费源全部失败时的稳定兜底(付费 API, 请求数受控).
- 无实时行情/股票列表: 抛 NotImplementedError(manager 自动跳过且不计入失败),
  supports_realtime=False 确保不参与实时行情循环.
- 限流: 平台 1000次/分 / 36次/秒, 超限返回 429. 本类串行 + 最小请求间隔,
  429/5xx 指数退避重试(用户在需求中强调限额, 见 .env LIXINGER_TOKEN).
- token 来自配置 数据源.lixinger.token(由 .env LIXINGER_TOKEN 注入), 不硬编码.

实测接口(2026-08-10, 均 POST + JSON body, headers 需 Content-Type: application/json
与 accept-encoding 含 gzip):
- cn/industry {source: "sw_2021"}           -> 全部行业(level: one/two/three, 511条)
- cn/industry/constituents/sw_2021 {date}   -> 全部行业成分股(一次全量, 去重5548只)
  两步共 2 次请求即可构建全市场申万2021三级映射, 远低于限额.
- cn/company/candlestick {type, stockCode, startDate, endDate} -> 日线K线
  type 为复权类型: ex_rights 不复权 / lxr_fc_rights 理杏仁前复权 /
  fc_rights 前复权 / bc_rights 后复权(非周期!).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

import pandas as pd

from app.core.datasource.base import KLINE_COLUMNS, DataSourceInterface, Quote, StockInfo, normalize_kline

logger = logging.getLogger(__name__)

BASE_URL = "https://open.lixinger.com/api/cn"

# 申万2021 行业分级代码规则: 6 位代码, 前2位=一级, 前4位=二级, 全6位=三级
# (如 110000 农林牧渔 / 110100 种植业 / 110101 种子)


class LixingerError(RuntimeError):
    """理杏仁 API 调用失败(含 HTTP 状态与响应摘要)."""


def build_sw_map(industries: list[dict[str, Any]], constituents: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """纯函数: 行业列表 + 全行业成分股 -> {symbol: {sw_l1, sw_l2, sw_l3}}.

    industries:   [{stockCode, name, level}]  (cn/industry, source=sw_2021)
    constituents: [{stockCode, constituents: [{stockCode, stockName}]}]  (全部行业, 一次全量)
    只取 level=three 的行业条目(一级/二级成分是其超集, 避免重复), 父级名称按代码前缀推导.
    """
    ind_map: dict[str, dict[str, str]] = {}
    for it in industries:
        code = str(it.get("stockCode", ""))
        if len(code) == 6:
            ind_map[code] = {"level": str(it.get("level", "")), "name": str(it.get("name", "")).strip()}

    def parent_name(code: str) -> str:
        return ind_map.get(code, {}).get("name", "")

    out: dict[str, dict[str, str]] = {}
    for item in constituents:
        ind_code = str(item.get("stockCode", ""))
        meta = ind_map.get(ind_code)
        if meta is None or meta["level"] != "three" or not meta["name"]:
            continue
        l1 = parent_name(ind_code[:2] + "0000")
        l2 = parent_name(ind_code[:4] + "00")
        for c in item.get("constituents", []):
            sym = str(c.get("stockCode", ""))
            if len(sym) == 6:
                # 罕见情况: 一只股票可能出现在多个三级行业, 保留首次归属
                out.setdefault(sym, {"sw_l1": l1, "sw_l2": l2, "sw_l3": meta["name"]})
    return out


class LixingerSource(DataSourceInterface):
    """理杏仁源: 申万2021 行业映射 + 日线 K 线(前复权兜底)."""

    name = "lixinger"
    label = "理杏仁"
    # 无实时行情: 不参与实时价循环, 避免 NotImplementedError 被记失败而误熔断
    supports_realtime: bool = False

    def __init__(self, token: str = "", interval_sec: float = 0.1, retry: int = 3) -> None:
        self.token = (token or "").strip()
        self._interval = interval_sec   # 最小请求间隔(秒), 默认 10 次/秒 < 36次/秒限额
        self._retry = retry
        self._last_ts = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------ HTTP 层
    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """POST + 限速 + 429/5xx 指数退避重试. 返回 (status, 解析后的响应对象)."""
        if not self.enabled:
            return 400, {"error": {"message": "LIXINGER_TOKEN 未配置"}}
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "accept-encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0 (compatible; a-stock-momentum-trend)",
        }
        last_body: Any = None
        for attempt in range(self._retry):
            # 串行限速: 距上次请求不足间隔时等待
            async with self._lock:
                wait = self._interval - (time.monotonic() - self._last_ts)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_ts = time.monotonic()

            def _do() -> tuple[int, Any]:
                req = urllib.request.Request(f"{BASE_URL}/{path}", data=data, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        body = r.read()
                        if r.headers.get("Content-Encoding") == "gzip":
                            body = gzip.decompress(body)
                        return r.status, json.loads(body.decode())
                except urllib.error.HTTPError as e:
                    raw = e.read()
                    try:
                        if e.headers.get("Content-Encoding") == "gzip":
                            raw = gzip.decompress(raw)
                        return e.code, json.loads(raw.decode())
                    except Exception:  # noqa: BLE001
                        return e.code, raw[:300].decode(errors="replace")

            try:
                status, last_body = await asyncio.to_thread(_do)
            except Exception as exc:  # noqa: BLE001
                status, last_body = -1, f"{type(exc).__name__}: {exc}"
            if status not in (429, 500, 502, 503, 504) or attempt == self._retry - 1:
                return status, last_body
            await asyncio.sleep(2 ** attempt + 0.5)  # 指数退避
        return status, last_body

    # ------------------------------------------------------------ K线(日频, 前复权)
    def supports_period(self, period: str) -> bool:
        """理杏仁仅提供日频 K 线, 其余周期由 manager 直接跳过(不计失败)."""
        return period == "daily"

    async def get_kline(self, symbol: str, period: str = "daily", count: int = 120, secid: str | None = None) -> pd.DataFrame:
        """日线 K 线(cn/company/candlestick, type=fc_rights).

        startDate 按 count 反推(2 倍自然日 + 缓冲, 保证非交易日也有足够数据),
        每次 1 只股票 1 次请求, 串行限速(默认 10 次/秒 < 36 次/秒限额).
        返回统一列 DataFrame, date 升序, 最多 count 条.
        注意: 实测 type=fc_rights 与 ex_rights 返回相同价格(未复权口径, 与 baostock 一致);
        volume 单位为股, 统一转为手(÷100); date 裁剪为 YYYY-MM-DD.
        """
        if period != "daily" or not symbol:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        start = (dt.date.today() - dt.timedelta(days=int(count) * 2 + 10)).isoformat()
        status, body = await self._post("company/candlestick", {
            "token": self.token,
            "stockCode": symbol,
            "type": "fc_rights",  # 文档为前复权; 实测与不复权同价, 兜底场景可接受
            "startDate": start,
            "endDate": dt.date.today().isoformat(),
        })
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return pd.DataFrame(columns=KLINE_COLUMNS)
        rows = [r for r in body["data"] if isinstance(r, dict)]
        if not rows:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        # 单位与格式统一: volume 股->手(÷100), date 去时区后缀取前 10 位
        for r in rows:
            r["volume"] = (r.get("volume") or 0) / 100
            r["date"] = str(r.get("date", ""))[:10]
        df = normalize_kline(pd.DataFrame(rows))
        if df.empty:
            return df
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(int(count)).reset_index(drop=True)

    # ------------------------------------------------------------ 基础能力(不参与路由)
    async def get_realtime_quote(self, symbols: list[str]) -> list[Quote]:
        raise NotImplementedError(f"{self.name} 暂不提供实时行情")

    async def get_stock_list(self, market: str = "all") -> list[StockInfo]:
        raise NotImplementedError(f"{self.name} 暂不提供股票列表")

    async def health_check(self) -> bool:
        """探活: 拉 1 条股票列表验证 token 与网络(每分钟一次, 限额内)."""
        if not self.enabled:
            return False
        try:
            status, body = await self._post("company", {
                "token": self.token, "pageIndex": 1, "pageSize": 1,
            })
            return status == 200 and bool(body.get("data"))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ 申万2021 行业分级
    async def get_sw_classification(self) -> dict[str, dict[str, str]]:
        """申万2021 全市场三级映射 {symbol: {sw_l1, sw_l2, sw_l3}}.

        两步共 2 次请求:
        1) cn/industry  {source: "sw_2021"}         -> 全部行业(含 level)
        2) cn/industry/constituents/sw_2021 {date}  -> 全部行业成分股(一次全量)
        """
        # 1) 行业列表
        status, body = await self._post("industry", {"token": self.token, "source": "sw_2021"})
        if status != 200 or not isinstance(body, dict) or not body.get("data"):
            raise LixingerError(f"行业列表拉取失败 status={status}: {body if not isinstance(body, dict) else body.get('error')}")
        industries: list[dict[str, Any]] = body["data"]

        # 2) 全行业成分股(取最近交易日)
        today = dt.date.today().strftime("%Y-%m-%d")
        status, body = await self._post("industry/constituents/sw_2021", {
            "token": self.token, "date": today,
        })
        if status != 200 or not isinstance(body, dict) or not body.get("data"):
            raise LixingerError(f"行业成分股拉取失败 status={status}: {body if not isinstance(body, dict) else body.get('error')}")
        constituents: list[dict[str, Any]] = body["data"]

        out = build_sw_map(industries, constituents)
        logger.info("理杏仁申万2021映射构建完成: 行业 %d 个, 覆盖股票 %d 只", len(industries), len(out))
        return out
