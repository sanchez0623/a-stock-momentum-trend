"""core.screener 包: 选股器(方案 §4.5)."""

from app.core.screener.engine import StockScreener, score_indicators
from app.core.screener.tasks import ScanTaskManager

screener = StockScreener()
scan_tasks = ScanTaskManager()

__all__ = ["StockScreener", "score_indicators", "screener", "scan_tasks"]
