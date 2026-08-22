"""盘后 AI 日报 - LLM 链(复用 ai_review.chain 的组件).

只做「把规则结果翻译成人话 + 明日行动优先级 + 历史记忆归因」,
所有数值(价格/仓位/止损线)来自规则引擎, LLM 不产生任何数值决策。
langchain 缺失/LLM 失败由 service 层降级规则模板日报。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# 日报 prompt 版本(修改下方提示词时必须递增; 随 DailyReport.prompt_version 落库)
REPORT_PROMPT_V = "report-2026-08-14-v1"


class DailyReportOutput(BaseModel):
    """日报结构化输出."""

    market_summary: str = Field(description="今日市况一句话(择时闸门结果)")
    trade_summary: str = Field(description="今日操作回顾(买卖/盈亏/纪律), 无操作写'无'")
    holdings_review: list[str] = Field(description="持仓逐一点评, 每只 ≤2 句; 无持仓则空数组")
    signals_today: list[str] = Field(description="今日信号说明, 无则空数组")
    tomorrow_watch: list[str] = Field(description="明日关注 2-5 条: 持仓止损/止盈距离 + 自选信号线索")
    risk_notes: list[str] = Field(description="风险提示(风控状态/仓位/连亏), 无则空数组")
    discipline_score: int = Field(description="纪律评分 0-100")

    @field_validator("discipline_score")
    @classmethod
    def _clamp_score(cls, v: int) -> int:
        return max(0, min(100, int(v)))


async def run_report_chain(materials: dict[str, Any], llm_cfg: dict[str, Any],
                           memory_lines: str = "") -> tuple[DailyReportOutput, str]:
    """执行日报 LLM 链, 返回 (DailyReportOutput, model).

    materials: service 组装的素材(市况/操作/持仓/信号/关注/风控/纪律诊断)。
    memory_lines: 复盘记忆检索结果(相似问题效果归因), 为空不注入。
    """
    from langchain_core.output_parsers import PydanticOutputParser

    from app.core.ai_review.chain import _call_parsed, build_chain_llm
    from app.core.ai_review.llm import LLMError

    if not llm_cfg.get("api_key"):
        raise LLMError("未配置 LLM API Key")

    memory_block = ""
    if memory_lines:
        memory_block = (
            "\n\n历史复盘记忆(相似问题的采纳结果与效果, 供归因参考):\n"
            f"{memory_lines}\n"
            "规则: 若某参数调整已被采纳过且无效, 日报中不要重复建议; 若有效可引用其效果。"
        )

    llm = build_chain_llm(llm_cfg)
    parser = PydanticOutputParser(pydantic_object=DailyReportOutput)
    out = await _call_parsed(
        system="你是一位严谨的 A 股动量/趋势交易系统日报编辑。"
               "你只负责把规则引擎算好的数据翻译成人话并排优先级, "
               "绝对不要编造价格/仓位/止损等任何数值, 不要给出调参建议(那是复盘的事)。",
        user=(
            "基于以下素材生成收盘后的「交易日报」。要求: 简洁可执行, 面向明天开盘前阅读。\n\n"
            f"{materials['text']}\n\n"
            f"{memory_block}\n\n"
            f"{parser.get_format_instructions()}"
        ),
        llm=llm,
        parser=parser,
        feature="daily_report",
        prompt_version=REPORT_PROMPT_V,
    )
    return out, llm.model_name
