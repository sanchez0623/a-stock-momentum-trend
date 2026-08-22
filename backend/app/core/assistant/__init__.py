"""AI 助理模块: 独立可开关的编排层(LangGraph 流水线).

盘前观察清单 / 盘中信号提醒 / 盘后日报; 不侵入 screener/signals/plan 核心逻辑,
只调用现有服务; 关闭开关即完全退回纯规则手动流程。
"""

from app.core.assistant.pipeline import PHASES, build_graph, run_phase

__all__ = ["PHASES", "build_graph", "run_phase"]
