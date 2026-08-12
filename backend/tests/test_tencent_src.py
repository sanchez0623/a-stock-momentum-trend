"""腾讯数据源解析健壮性测试(除权信息 dict 字段防护)."""

from __future__ import annotations

from app.core.datasource.tencent_src import _fnum


def test_fnum_skips_dict_and_bad_values():
    # 腾讯除权日 K 线第 7 位是 dict(如 FHcontent), 必须兜底为 0 而不抛异常
    assert _fnum({"FHcontent": "10派1.3元"}) == 0.0
    assert _fnum(["1", "2"]) == 0.0
    assert _fnum("46.870") == 46.87
    assert _fnum(12.5) == 12.5
    assert _fnum("") == 0.0
    assert _fnum(None) == 0.0

def test_tencent_volume_unit_normalized():
    """腾讯 fqkline: 主板/创业板 volume=手 -> 归一为股; 科创板(688)本就为股不乘."""
    import asyncio
    import json

    from app.core.datasource import tencent_src as ts

    def _resp(rows):
        return {"data": {"sh600519": {"qfqday": rows}}}

    async def fake_text(url):
        return json.dumps(_resp([
            ["2026-08-12", "1342.0", "1343.0", "1345.0", "1340.0", "35060", "-"],
            ["2026-08-11", "1345.0", "1346.5", "1350.0", "1340.0", "27073", "-"],
        ]))

    src = ts.TencentSource()
    src._text = fake_text
    df = asyncio.run(src.get_kline("600519", "daily", 2))
    # 手 -> 股: 35060手 = 350.6万股; amount 由 normalize 按 volume*close 估算(元)
    row = df.iloc[0]  # fake 数据中 08-12 在前
    assert int(row["volume"]) == 3_506_000
    amt = float(row["amount"])
    # normalize 按 volume×(close+open)/2 估算(元)
    expect = 3_506_000 * (1343.0 + 1342.0) / 2
    assert abs(amt - expect) < expect * 0.01

    # 科创板: 返回即股, 不乘 100
    async def fake_text2(url):
        return json.dumps({"data": {"sh688046": {"qfqday": [
            ["2026-08-12", "38.5", "38.6", "38.8", "38.4", "25781117", "-"],
        ]}}})

    src._text = fake_text2
    df2 = asyncio.run(src.get_kline("688046", "daily", 2))
    assert int(df2.iloc[0]["volume"]) == 25_781_117
