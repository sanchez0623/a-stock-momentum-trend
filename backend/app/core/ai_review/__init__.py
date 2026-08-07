"""core.ai_review 包: AI 复盘引擎(方案 §4.10)."""

from app.core.ai_review.rules import diagnose
from app.core.ai_review.service import ReviewService, review_service
from app.core.ai_review.tuning import (
    COOLDOWN_DAYS,
    MAX_ACCEPT_PER_REVIEW,
    MAX_DRIFT_PCT,
    MAX_STEP_PCT,
    WHITELIST,
    apply_patch,
    evaluate_patch,
    list_changes,
    revert_change,
    suggest_from_issues,
    validate_config,
)

__all__ = [
    "diagnose", "ReviewService", "review_service",
    "evaluate_patch", "apply_patch", "revert_change", "list_changes",
    "suggest_from_issues", "validate_config", "WHITELIST",
    "MAX_STEP_PCT", "MAX_DRIFT_PCT", "COOLDOWN_DAYS", "MAX_ACCEPT_PER_REVIEW",
]
