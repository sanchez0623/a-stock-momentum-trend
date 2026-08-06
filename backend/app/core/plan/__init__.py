"""core.plan 包: 交易计划生成(方案 §4.6, 系统最核心交付物)."""

from app.core.plan.generator import PlanGenerator

plan_generator = PlanGenerator()

__all__ = ["PlanGenerator", "plan_generator"]
