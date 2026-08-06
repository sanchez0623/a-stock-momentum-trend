"""core.risk 包: 风控闸门(方案 §4.7)."""

from app.core.risk.manager import RiskManager

risk_manager = RiskManager()

__all__ = ["RiskManager", "risk_manager"]
