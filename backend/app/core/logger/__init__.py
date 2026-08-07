"""core.logger 包: 交易日志(方案 §4.8, 双写 SQLite + CSV)."""

from app.core.logger.trades import TradeLogger

trade_logger = TradeLogger()

__all__ = ["TradeLogger", "trade_logger"]
