"""理杏仁源: 申万2021行业分级构建 + 分类刷新双源切换."""

from __future__ import annotations

import pytest
from app.core.datasource.lixinger_src import build_sw_map


# ---------------------------------------------------------------- build_sw_map 纯函数
def _ind(code: str, name: str, level: str) -> dict:
    return {"stockCode": code, "name": name, "level": level}


def test_build_sw_map_hierarchy():
    """三级行业成分股 -> 正确推导 一级/二级/三级 名称; 一级/二级成分是超集应跳过."""
    industries = [
        _ind("110000", "农林牧渔", "one"),
        _ind("110100", "种植业", "two"),
        _ind("110101", "种子", "three"),
        _ind("340000", "食品饮料", "one"),
        _ind("340300", "饮料制造", "two"),
        _ind("340301", "白酒", "three"),
    ]
    constituents = [
        {"stockCode": "110101", "constituents": [{"stockCode": "002041"}]},
        {"stockCode": "340301", "constituents": [{"stockCode": "600519"}]},
        # 一级行业成分是超集, 不应重复构建/覆盖
        {"stockCode": "110000", "constituents": [{"stockCode": "002041"}, {"stockCode": "600519"}]},
    ]
    out = build_sw_map(industries, constituents)
    assert out["002041"] == {"sw_l1": "农林牧渔", "sw_l2": "种植业", "sw_l3": "种子"}
    assert out["600519"] == {"sw_l1": "食品饮料", "sw_l2": "饮料制造", "sw_l3": "白酒"}
    assert len(out) == 2


def test_build_sw_map_unknown_industry_skipped():
    """成分股条目对应不到行业列表(代码不匹配/非三级)时跳过."""
    industries = [_ind("110101", "种子", "three"), _ind("110100", "种植业", "two")]
    constituents = [
        {"stockCode": "999999", "constituents": [{"stockCode": "000001"}]},  # 行业列表无此代码
        {"stockCode": "110100", "constituents": [{"stockCode": "000002"}]},  # 二级, 跳过
    ]
    assert build_sw_map(industries, constituents) == {}


def test_build_sw_map_duplicate_keeps_first():
    """同一股票出现在多个三级行业时, 保留首次归属."""
    industries = [
        _ind("110101", "种子", "three"),
        _ind("110102", "粮食种植", "three"),
        _ind("110000", "农林牧渔", "one"),
        _ind("110100", "种植业", "two"),
    ]
    constituents = [
        {"stockCode": "110101", "constituents": [{"stockCode": "002041"}]},
        {"stockCode": "110102", "constituents": [{"stockCode": "002041"}]},
    ]
    out = build_sw_map(industries, constituents)
    assert out["002041"]["sw_l3"] == "种子"


# ---------------------------------------------------------------- refresh_classification 双源
@pytest.mark.asyncio
async def test_refresh_auto_prefers_lixinger(monkeypatch, tmp_engine):
    """auto 模式: token 配置且源未熔断 -> 走理杏仁落库, source=lixinger, 板块字段保留."""
    from app.core import classification as clf
    from app.core.config import config_manager

    config_manager.update({"数据源": {"lixinger": {"token": "fake-token"}}})

    sw = {"600519": {"sw_l1": "食品饮料", "sw_l2": "饮料制造", "sw_l3": "白酒"}}

    async def _fake_fetch(progress_cb=None):
        return sw

    monkeypatch.setattr(clf, "_fetch_lixinger_sw_raw", _fake_fetch)

    stats = await clf.refresh_classification(source="auto")
    assert stats["ok"] and stats["source"] == "lixinger"
    assert stats["total"] == 1 and stats["sw_l3_covered"] == 1

    row = clf.get_classification("600519")
    assert row is not None
    assert row.sw_l3 == "白酒" and row.source == "lixinger"
    assert row.boards_industry == "[]"  # 理杏仁不提供板块


@pytest.mark.asyncio
async def test_refresh_lixinger_only_without_token(monkeypatch, tmp_engine):
    """source=lixinger 且 token 未配置 -> 报错不回落."""
    from app.core import classification as clf
    from app.core.config import config_manager

    config_manager.update({"数据源": {"lixinger": {"token": ""}}})

    stats = await clf.refresh_classification(source="lixinger")
    assert stats["ok"] is False and stats["error"] == "lixinger_not_configured"


@pytest.mark.asyncio
async def test_refresh_auto_fallback_to_akshare(monkeypatch, tmp_engine):
    """auto 模式: 理杏仁失败 -> 自动回落 akshare 原逻辑, source=akshare."""
    from app.core import classification as clf
    from app.core.config import config_manager

    config_manager.update({"数据源": {"lixinger": {"token": "fake-token"}}})

    async def _boom(progress_cb=None):
        raise RuntimeError("network down")

    async def _fake_sw():
        return {"600519": {"sw_l1": "食品饮料", "sw_l2": "饮料制造", "sw_l3": "白酒"}}

    async def _fake_boards():
        return {}

    monkeypatch.setattr(clf, "_fetch_lixinger_sw_raw", _boom)
    monkeypatch.setattr(clf, "_fetch_shenwan_raw", _fake_sw)
    monkeypatch.setattr(clf, "_fetch_boards_raw", _fake_boards)

    stats = await clf.refresh_classification(source="auto")
    assert stats["ok"] and stats["source"] == "akshare"
    assert stats["total"] == 1

    row = clf.get_classification("600519")
    assert row is not None and row.source == "akshare"
