"""数据源层: 统一出口.

使用:
    from app.core.datasource import data_source_manager, build_sources
    data_source_manager.setup(build_sources())
    df = await data_source_manager.get_kline("300750")
"""

from app.core.datasource.base import (
    CAPABILITIES,
    SUPPORTED_PERIODS,
    DataSourceInterface,
    EarningsEventItem,
    Fundamental,
    Quote,
    StockInfo,
    guess_market,
    normalize_kline,
)
from app.core.datasource.cache import kline_store, quote_cache
from app.core.datasource.manager import data_source_manager


def build_sources() -> list[tuple[str, type[DataSourceInterface]]]:
    """按配置构造启用的数据源工厂列表(manager.setup 使用)."""
    from app.core.config import config_manager
    from app.core.datasource import akshare_src, baostock_src, eastmoney_src, mootdx_src, tencent_src

    ds_cfg = config_manager.get().get("数据源", {})
    em = ds_cfg.get("eastmoney", {})

    def make_eastmoney() -> eastmoney_src.EastmoneySource:
        return eastmoney_src.EastmoneySource(
            interval_sec=float(em.get("interval_sec", 2.0)),
            max_workers=int(em.get("max_workers", 1)),
            retry=int(em.get("retry", 3)),
            enable_patch=bool(em.get("enable_patch", True)),
            proxy_pool=list(ds_cfg.get("proxy_pool", [])),
        )

    # baostock 排在东财之前: 免费无风控, 日线更稳; 它不支持分钟线会被自动跳过
    return [("mootdx", mootdx_src.MootdxSource),
            ("tencent", tencent_src.TencentSource),
            ("baostock", baostock_src.BaostockSource),
            ("eastmoney", make_eastmoney),
            ("akshare", akshare_src.AkshareSource)]


__all__ = [
    "DataSourceInterface",
    "Quote",
    "StockInfo",
    "Fundamental",
    "EarningsEventItem",
    "CAPABILITIES",
    "normalize_kline",
    "guess_market",
    "SUPPORTED_PERIODS",
    "kline_store",
    "quote_cache",
    "data_source_manager",
    "build_sources",
]
