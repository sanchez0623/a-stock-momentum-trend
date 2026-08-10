"""理杏仁源单测: K线请求构造/周期限制/实时开关(不联网, mock HTTP 层)."""

from __future__ import annotations

import datetime as dt

from app.core.datasource.lixinger_src import LixingerSource

COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _src(monkeypatch, response) -> tuple[LixingerSource, dict]:
    """构造带 mock _post 的源, 记录最近一次调用参数."""
    src = LixingerSource(token="t" * 36, interval_sec=0)
    calls: dict = {}

    async def fake_post(path: str, payload: dict):
        calls["path"] = path
        calls["payload"] = payload
        return 200, response

    monkeypatch.setattr(src, "_post", fake_post)
    return src, calls


async def test_get_kline_daily_builds_request(monkeypatch):
    """日线请求构造: 正确接口路径/复权类型/代码/日期范围, 返回统一列升序."""
    src, calls = _src(monkeypatch, {"code": 1, "message": "success", "data": [
        {"date": "2026-08-06T00:00:00+08:00", "open": 1.5, "high": 2.0, "low": 1.2,
         "close": 1.8, "volume": 200, "amount": 360},
        {"date": "2026-08-05T00:00:00+08:00", "open": 1.0, "high": 2.0, "low": 0.9,
         "close": 1.5, "volume": 100, "amount": 150},
    ]})
    df = await src.get_kline("600519", "daily", 120)
    assert calls["path"] == "company/candlestick"
    p = calls["payload"]
    assert p["type"] == "fc_rights"  # 前复权(文档: type 是复权类型, 非周期)
    assert p["stockCode"] == "600519"
    assert p["token"] == "t" * 36
    # startDate 按 count 反推(2 倍自然日 + 缓冲)
    assert dt.date.fromisoformat(p["startDate"]) <= dt.date.today() - dt.timedelta(days=120)
    assert list(df.columns) == COLUMNS
    assert len(df) == 2
    assert df.iloc[-1]["close"] == 1.8  # 已按 date 升序
    assert df.iloc[0]["date"] < df.iloc[1]["date"]


async def test_get_kline_tail_count(monkeypatch):
    """超过 count 的数据只保留最近 count 条."""
    rows = [
        {"date": f"2026-01-{i:02d}T00:00:00+08:00", "open": 1, "high": 1, "low": 1,
         "close": 1, "volume": 1, "amount": 1}
        for i in range(1, 21)
    ]
    src, _calls = _src(monkeypatch, {"code": 1, "data": rows})
    df = await src.get_kline("600519", "daily", 10)
    assert len(df) == 10
    # date 保留上游 ISO 格式, 取前 10 位比较日期
    assert df.iloc[0]["date"][:10] == "2026-01-11"


async def test_get_kline_non_daily_returns_empty(monkeypatch):
    """非 daily 周期直接返回空表, 不发请求(manager 也会因 supports_period 跳过)."""
    src, calls = _src(monkeypatch, {"code": 1, "data": []})
    for period in ("1m", "60m", "weekly"):
        df = await src.get_kline("600519", period, 120)
        assert df.empty, period
    assert "path" not in calls  # 未发任何请求


async def test_get_kline_failure_returns_empty(monkeypatch):
    """接口失败/无数据 -> 返回空表而不是抛错(供 manager 继续尝试下一源)."""
    src, _ = _src(monkeypatch, {"code": 1, "error": {"message": "stock not found"}})
    df = await src.get_kline("600519", "daily", 120)
    assert df.empty


def test_supports_period_only_daily():
    src = LixingerSource(token="t" * 36)
    assert src.supports_period("daily") is True
    for p in ("1m", "5m", "15m", "30m", "60m", "weekly"):
        assert src.supports_period(p) is False


def test_supports_realtime_disabled():
    """无实时行情: 声明 False, manager 不参与实时价循环(避免 NotImplementedError 记失败)."""
    assert LixingerSource.supports_realtime is False
