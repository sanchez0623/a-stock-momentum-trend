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
