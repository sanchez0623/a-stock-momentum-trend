"""core.position 包: 虚拟持仓管理(方案 §4.4, 不接实盘, 记录意图)."""

from app.core.position.manager import PositionManager, PositionManagerError

position_manager = PositionManager()

__all__ = ["PositionManager", "PositionManagerError", "position_manager"]
