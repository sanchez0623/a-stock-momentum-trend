"""理杏仁开放平台 API 探针: 验证 token 可用性与接口连通性.

用法:
    $env:LIXINGER_TOKEN="your-token"; python lixinger_probe.py [basic|kline|all]

说明:
    - 域名: https://open.lixinger.com/api/cn/... (api-key 文档路径直接对应接口路径)
    - 请求: POST + JSON body, headers 必须带 Content-Type: application/json
      与 accept-encoding 含 gzip; 限流 1000次/分 / 36次/秒, 超限返回 429
    - 本脚本含指数退避重试(429/5xx), 可作接入时的网络层参考
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("LIXINGER_TOKEN", "")
BASE = "https://open.lixinger.com/api/cn"


def _post(path: str, payload: dict, retries: int = 3) -> tuple[int, object]:
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(f"{BASE}/{path}", data=data, headers={
            "Content-Type": "application/json",
            "accept-encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0",
        })
        try:
            r = urllib.request.urlopen(req, timeout=20)
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return r.status, json.loads(body.decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt + 0.5)  # 指数退避
                continue
            body = e.read()
            try:
                if e.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return e.code, json.loads(body.decode())
            except Exception:  # noqa: BLE001
                return e.code, body[:300].decode(errors="replace")
        except Exception as e:  # noqa: BLE001
            return -1, f"{type(e).__name__}: {e}"
    return -1, "retry exhausted"


def show(title: str, path: str, payload: dict) -> None:
    code, body = _post(path, payload)
    tag = "OK" if code == 200 else f"HTTP {code}"
    print(f"\n===== {title} [{path}] -> {tag}")
    if isinstance(body, dict):
        d = body.get("data")
        if isinstance(d, list) and d:
            print(f"  rows: {len(d)} | sample: {json.dumps(d[0], ensure_ascii=False)[:400]}")
        elif isinstance(d, dict):
            print(f"  sample: {json.dumps(d, ensure_ascii=False)[:400]}")
        elif d is not None:
            print(f"  data: {json.dumps(d, ensure_ascii=False)[:200]}")
        else:
            print(f"  msg: {body.get('message') or body.get('msg') or ''}")
    else:
        print(f"  {str(body)[:300]}")


def main() -> None:
    if not TOKEN:
        print("请先设置环境变量 LIXINGER_TOKEN")
        sys.exit(1)
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    P = {"token": TOKEN}

    if which in ("all", "basic"):
        show("股票列表+基本信息(分页)", "company", {**P, "pageIndex": 1, "pageSize": 2,
             "metricsList": ["stockCode", "name", "exchange", "ipoDate"]})
        show("日K线", "company/candlestick", {**P, "stockCode": "600519", "type": "daily",
             "startDate": "2026-08-01", "endDate": "2026-08-07",
             "metricsList": ["date", "open", "high", "low", "close", "volume", "amount"]})
        show("估值指标", "company/fundamental/non_financial", {**P, "stockCodes": ["600519"],
             "metricsList": ["pe_ttm", "pb", "ps_ttm", "dyr"], "date": "2026-08-07"})
        show("财报科目", "company/fs/non_financial", {**P, "stockCodes": ["600519"],
             "metricsList": ["q.ps.toi.t", "q.ps.np.t", "q.bs.ta.t"],
             "startDate": "2026-01-01", "endDate": "2026-08-07"})
        show("指数K线", "index/candlestick", {**P, "stockCode": "000300", "type": "normal",
             "startDate": "2026-08-01", "endDate": "2026-08-07",
             "metricsList": ["date", "open", "high", "low", "close"]})
        show("指数成分股", "index/constituents", {**P, "indexCode": "000300",
             "metricsList": ["stockCode", "stockName"]})
    if which in ("all", "extra"):
        show("所属行业", "company/industries", {**P, "stockCode": "600519",
             "metricsList": ["industryName"]})
        show("所属指数", "company/indices", {**P, "stockCode": "600519",
             "metricsList": ["indexName"]})
        show("波动率", "company/volatility", {**P, "stockCode": "600519", "volatilityDays": 20,
             "startDate": "2026-07-01", "endDate": "2026-08-07", "metricsList": ["volatility"]})
        show("大宗交易", "company/block-deal", {**P, "stockCode": "600519",
             "startDate": "2026-01-01", "endDate": "2026-08-07",
             "metricsList": ["tradingPrice", "tradingVolume", "discountRate"]})
        show("股东人数", "company/shareholders-num", {**P, "stockCode": "600519",
             "startDate": "2026-01-01", "endDate": "2026-08-07",
             "metricsList": ["num", "shareholdersNumberChangeRate"]})
        show("融资融券", "company/margin-trading-and-securities-lending", {**P,
             "stockCode": "600519", "startDate": "2026-07-01", "endDate": "2026-08-07",
             "metricsList": ["financingBalance", "financingPurchaseAmount"]})
        show("龙虎榜", "company/trading-abnormal", {**P, "stockCode": "600519",
             "startDate": "2026-01-01", "endDate": "2026-08-07",
             "metricsList": ["date", "reason", "netBuyAmount"]})
        show("公告", "company/announcement", {**P, "stockCode": "600519",
             "startDate": "2026-07-01", "endDate": "2026-08-07",
             "metricsList": ["linkText", "date"]})
        show("分红", "company/dividend", {**P, "stockCode": "600519",
             "metricsList": ["dividendYield", "exRightDate"]})
        show("指数成分权重", "index/constituent-weightings", {**P, "indexCode": "000300",
             "startDate": "2026-08-01", "endDate": "2026-08-07",
             "metricsList": ["stockCode", "weight"]})
        show("指数波动率", "index/volatility", {**P, "stockCode": "000300", "volatilityDays": 20,
             "startDate": "2026-07-01", "endDate": "2026-08-07", "metricsList": ["volatility"]})


if __name__ == "__main__":
    main()
