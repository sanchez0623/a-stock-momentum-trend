"""core.ai_review 包: AI 复盘引擎(方案 §4.10)."""

from app.core.ai_review.rules import diagnose
from app.core.ai_review.service import ReviewService, review_service

__all__ = ["diagnose", "ReviewService", "review_service"]
