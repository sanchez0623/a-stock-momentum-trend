"""core.signals 包: 五类信号引擎(方案 §4.3).

信号优先级: SELL_STOP > SELL_REDUCE > BUY_ADD > BUY_FIRST > T_BUY/T_SELL
每个信号: type / symbol / direction / strength(0-100) / reason(人话) / indicators_snapshot
"""

from app.core.signals.engine import Signal, SignalEngine, signal_types

__all__ = ["Signal", "SignalEngine", "signal_types"]
