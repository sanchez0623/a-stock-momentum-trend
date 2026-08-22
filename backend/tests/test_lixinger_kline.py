"""理杏仁源: 日线K线兜底(单位转换/日期裁剪/周期限制)."""

from __future__ import annotations

import pytest
from app.core.datasource.lixinger_src import LixingerSource


def _src() -> LixingerSource:
    return LixingerSource(token="fake-token")


@pytest.mark.asyncio
async def test_get_kline_units_and_date(monkeypatch):
    """volume 股->手(÷100), date 去时区后缀; 结果升序且最多 count 条."""
    src = _src()

    async def _fake_post(path: str, payload: dict):
        assert path == "company/candlestick"
        assert payload["type"] == "fc_rights" and payload["stockCode"] == "600519"
        return 200, {"data": [
            {"date": "2026-08-06T00:00:00+08:00", "open": 10, "high": 11, "low": 9,
             "close": 10.5, "volume": 1000000, "amount": 10500000},
            {"date": "2026-08-07T00:00:00+08:00", "open": 10.6, "high": 11, "low": 10.2,
             "close": 10.8, "volume": 2000000, "amount": 21600000},
        ]}

    monkeypatch.setattr(src, "_post", _fake_post)
    df = await src.get_kline("600519", "daily", count=120)
    assert len(df) == 2
    assert df.iloc[-1]["date"] == "2026-08-07"           # 时区后缀已裁剪
    assert df.iloc[-1]["volume"] == 20000.0              # 2000000 股 -> 20000 手
    assert df.iloc[0]["date"] < df.iloc[1]["date"]       # 升序


@pytest.mark.asyncio
async def test_get_kline_only_daily(monkeypatch):
    """非 daily 周期不请求上游, 直接返回空."""
    src = _src()
    called = False

    async def _fake_post(path: str, payload: dict):
        nonlocal called
        called = True
        return 200, {"data": []}

    monkeypatch.setattr(src, "_post", _fake_post)
    empty = await src.get_kline("600519", "weekly", count=120)
    assert empty.empty and not called


@pytest.mark.asyncio
async def test_get_kline_failure_returns_empty(monkeypatch):
    """上游失败/异常响应 -> 空 DataFrame(不抛出, 供 manager 降级)."""
    src = _src()

    async def _fake_post(path: str, payload: dict):
        return 429, {"error": {"message": "too many"}}

    monkeypatch.setattr(src, "_post", _fake_post)
    df = await src.get_kline("600519", "daily", count=120)
    assert df.empty


@pytest.mark.asyncio
async def test_get_kline_tail_count(monkeypatch):
    """返回条数不超过 count(倒序截取后再升序)."""
    src = _src()
    rows = [{"date": f"2026-07-{i:02d}T00:00:00+08:00", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 1000, "amount": 1500} for i in range(1, 30)]

    async def _fake_post(path: str, payload: dict):
        return 200, {"data": rows}

    monkeypatch.setattr(src, "_post", _fake_post)
    df = await src.get_kline("600519", "daily", count=10)
    assert len(df) == 10
    assert df.iloc[0]["date"] < df.iloc[-1]["date"]
