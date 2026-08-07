"""core.stats 包: 历史回顾统计(方案 §4.9)."""

from app.core.stats.service import StatsService

stats = StatsService()

__all__ = ["StatsService", "stats"]
