"""AI 复盘 - LangChain 两步链(方案: 链 + 结构化输出).

Step1 行为特征归纳(BehaviorProfile) -> Step2 基于特征生成建议(ReviewOutput),
输出用 Pydantic 结构化解析, 解析失败自动带错误信息重试一次。

护栏不在这里重复: sanitize_llm_patch / evaluate_patch 仍在 service 层统一把关。
langchain 依赖缺失时抛 LangChainUnavailable, 由 service 降级旧单步路径。
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar, cast

from pydantic import BaseModel, Field, field_validator

from app.core.ai_review.llm import DEEPSEEK_BASE, DEFAULT_MODEL, LLMError

logger = logging.getLogger(__name__)

# 两步链 prompt 版本(修改本文件内 Step1/Step2 提示词时必须递增版本号;
# 版本号随 AiReview.prompt_version 与 LlmCall.prompt_version 落库, 用于复现与回归对比)
REVIEW_PROMPT_V = "review-2step-2026-08-14-v1"

T = TypeVar("T", bound=BaseModel)

try:
    from langchain_core.output_parsers import PydanticOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    CHAIN_AVAILABLE = True
except ImportError:  # pragma: no cover - langchain 未安装
    CHAIN_AVAILABLE = False


class LangChainUnavailable(Exception):
    """langchain 依赖缺失, 调用方应降级旧路径."""


# ---------------------------------------------------------------- 结构化输出模型
class PatchInfo(BaseModel):
    """可执行参数补丁(仅允许白名单 group/key, to 须落在本次允许区间)."""

    group: str = Field(description="参数分组, 仅限可调白名单分组")
    key: str = Field(description="参数名, 仅限可调白名单字段")
    to: float = Field(description="目标值, 须在本次允许区间内")


class Suggestion(BaseModel):
    """一条建议: 纯文字, 或文字 + 可选参数补丁."""

    text: str = Field(description="一条可执行建议")
    patch: PatchInfo | None = Field(default=None, description="可选; 只在建议确实对应某个参数调整时才给")


class BehaviorProfile(BaseModel):
    """Step1 输出: 交易行为特征归纳(不含任何参数建议)."""

    behavior_summary: str = Field(description="交易行为总体特征, 100字内")
    key_patterns: list[str] = Field(description="观察到的 2-4 个关键行为模式")
    discipline_issues: list[str] = Field(description="纪律问题, 无则空数组")


class ReviewOutput(BaseModel):
    """Step2 输出: 复盘结论."""

    analysis: str = Field(description="整体问题归因与亮点, 150字内")
    suggestions: list[Suggestion] = Field(description="2-5 条建议")
    discipline_score: int = Field(description="纪律评分 0-100")

    @field_validator("discipline_score")
    @classmethod
    def _clamp_score(cls, v: int) -> int:
        return max(0, min(100, int(v)))


# ---------------------------------------------------------------- LLM 构建
# 推理模型(先输出大段思考过程再出正文)的特征名: 正文预算不足会被截断为空
REASONING_MODEL_HINTS = ("flash", "reasoner", "r1", "o1", "o3", "thinking", "pro")


def build_chain_llm(llm_cfg: dict[str, Any]) -> ChatOpenAI:
    """从 config 的 llm 配置段构建 LangChain ChatOpenAI(兼容 OpenAI 协议).

    与 llm.LLMClient 同一套 base_url/model 推断逻辑(空地址默认 DeepSeek).
    推理模型(如 deepseek-v4-flash)的 max_tokens 预算含思考过程, 按配置 2000
    会被思考吃光导致正文为空, 故自动提升下限(用户已指示至少 8192).
    """
    base_url = (llm_cfg.get("base_url") or DEEPSEEK_BASE)
    model = llm_cfg.get("model") or DEFAULT_MODEL
    if not base_url or base_url == "https://api.openai.com/v1":
        base_url = DEEPSEEK_BASE
        model = model if model not in ("gpt-4o-mini",) else DEFAULT_MODEL
    max_tokens = int(llm_cfg.get("max_tokens", 8192))
    if any(h in model.lower() for h in REASONING_MODEL_HINTS):
        max_tokens = max(max_tokens, 8192)
    return ChatOpenAI(  # type: ignore[call-arg]
        model=model,
        base_url=base_url,
        api_key=llm_cfg.get("api_key", ""),
        temperature=float(llm_cfg.get("temperature", 0.3)),
        max_tokens=max_tokens,
        timeout=float(llm_cfg.get("timeout_sec", 60)),
        max_retries=1,
    )


def _build_prompt(system: str, user: str) -> ChatPromptTemplate:
    """构造 prompt. 用 Message 对象而非元组, 绕过 f-string 模板解析
    (提示词正文含 JSON 花括号, 走模板会报 nested replacement fields)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    return ChatPromptTemplate.from_messages([
        SystemMessage(content=system),
        HumanMessage(content=user),
    ])


async def _call_parsed(system: str, user: str, llm: ChatOpenAI,
                       parser: PydanticOutputParser[T], retries: int = 1,
                       feature: str = "", prompt_version: str = "") -> T:
    """调用 LLM 并解析结构化输出; 解析失败把错误反馈给模型重试一次.

    网络/HTTP 层异常统一包装为 LLMError, 由 service 层决定降级。
    每次实际调用(含失败/重试)都会落一条 LlmCall 记账记录。
    """
    from app.core.ai_review import call_log

    prompt_hash = call_log.hash_prompt(system, user)
    provider = call_log.provider_of(
        getattr(llm, "base_url", "") or getattr(llm, "openai_api_base", "") or "")
    model = getattr(llm, "model_name", "") or getattr(llm, "model", "") or ""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        prompt = _build_prompt(system, user)
        start = call_log.started()
        try:
            # langchain 1.x: ainvoke 不接受 ChatPromptTemplate, 需先 format 为消息列表
            resp = await llm.ainvoke(prompt.format_messages())
        except Exception as exc:  # noqa: BLE001 - langchain 各类网络/鉴权异常
            call_log.record_call(feature=feature, model=model, provider=provider,
                                 prompt_hash=prompt_hash, prompt_version=prompt_version,
                                 latency_ms=call_log.elapsed_ms(start), ok=False,
                                 error=f"LLM 请求失败: {exc}")
            logger.warning("LLM 调用异常", exc_info=True, extra={"component": "llm_chain"})
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        latency = call_log.elapsed_ms(start)
        usage = call_log.extract_usage(resp)
        text = cast(str, resp.content) if hasattr(resp, "content") else str(resp)
        try:
            parsed = parser.parse(text)
        except Exception as exc:  # noqa: BLE001 - 解析失败(含 JSON/校验)
            last_err = exc
            call_log.record_call(feature=feature, model=model, provider=provider,
                                 prompt_hash=prompt_hash, prompt_version=prompt_version,
                                 input_tokens=usage["input_tokens"],
                                 output_tokens=usage["output_tokens"],
                                 total_tokens=usage["total_tokens"],
                                 latency_ms=latency, ok=False,
                                 error=f"结构化输出解析失败: {exc}")
            if attempt < retries:
                logger.warning("结构化输出解析失败, 带错误重试: %s", exc)
                user = (
                    f"{user}\n\n警告: 上一次输出解析失败({exc})。"
                    "请只输出合法 JSON, 不要输出任何其它内容。"
                )
                continue
            raise LLMError(f"LLM 结构化输出解析失败: {last_err}") from last_err
        call_log.record_call(feature=feature, model=model, provider=provider,
                             prompt_hash=prompt_hash, prompt_version=prompt_version,
                             input_tokens=usage["input_tokens"],
                             output_tokens=usage["output_tokens"],
                             total_tokens=usage["total_tokens"],
                             latency_ms=latency)
        return parsed
    raise LLMError(f"LLM 结构化输出解析失败: {last_err}") from last_err


# ---------------------------------------------------------------- 材料拼装
def _material_lines(trades: list[Any], signals: list[Any],
                    issues: list[dict], stats: dict) -> dict[str, str]:
    """把交易记录/规则诊断/阶段分布拼成 prompt 文本(与旧单步路径同源)."""
    trade_lines = "\n".join(
        f"- {t.time} {t.symbol} {t.name} {'买入' if t.action == 'buy' else '卖出'} "
        f"{t.qty}股@{t.price} 盈亏{t.pnl if t.pnl is not None else '-'} {t.reason or ''}"
        for t in trades[-40:]
    ) or "- 无"
    issue_lines = "\n".join(
        f"- [{i['level']}] {i['title']}: {i['detail']}" for i in issues
    ) or "- 无"
    stage_lines = "\n".join(
        f"- {k}: 买入{int(v['n'])}笔 / 平仓{int(v['closed'])} / 胜率{v['win_rate']}% / 盈亏{v['pnl']:.0f}元"
        for k, v in (stats.get("buy_stages") or {}).items()
    ) or "- 无(数据不足)"
    return {"trade_lines": trade_lines, "issue_lines": issue_lines, "stage_lines": stage_lines}


# ---------------------------------------------------------------- 两步链
async def run_review_chain(trades: list[Any], signals: list[Any], issues: list[dict],
                           stats: dict, llm_cfg: dict[str, Any],
                           memory_lines: str = "") -> tuple[ReviewOutput, str]:
    """执行两步链复盘, 返回 (ReviewOutput, model).

    Step1: 归纳交易行为特征(不产生建议); Step2: 基于特征 + 规则诊断 + 可调参数清单生成建议。
    memory_lines: 历史复盘记忆文本(复盘记忆 RAG 检索结果), 供 Step2 效果归因参考。
    """
    if not CHAIN_AVAILABLE:
        raise LangChainUnavailable("langchain 未安装, 请安装 langchain-core langchain-openai")
    if not llm_cfg.get("api_key"):
        raise LLMError("未配置 LLM API Key")
    llm = build_chain_llm(llm_cfg)
    mat = _material_lines(trades, signals, issues, stats)
    closed = int(stats.get("closed", 0) or 0)
    win_rate = float(stats.get("win_rate", 0) or 0)
    total_pnl = float(stats.get("total_pnl", 0) or 0)

    # ---- Step1: 行为特征归纳
    profile_parser = PydanticOutputParser(pydantic_object=BehaviorProfile)
    profile = await _call_parsed(
        system="你是一位严谨的量化交易复盘分析师。你的任务只是归纳交易行为特征, "
               "绝对不要给出任何参数调整建议。",
        user=(
            "基于以下材料归纳这份 A 股动量/趋势交易日志的行为特征:\n\n"
            f"交易记录(最近):\n{mat['trade_lines']}\n\n"
            f"规则诊断结果:\n{mat['issue_lines']}\n\n"
            f"近 {closed} 笔已平仓, 胜率 {win_rate}%, 总盈亏 {total_pnl} 元。\n"
            f"买入日的趋势阶段分布(阶段: 笔数/已平仓/胜率/盈亏):\n{mat['stage_lines']}\n\n"
            f"{profile_parser.get_format_instructions()}"
        ),
        llm=llm,
        parser=profile_parser,
        feature="ai_review.step1",
        prompt_version=REVIEW_PROMPT_V,
    )
    logger.info("两步链 Step1 完成: %s", profile.behavior_summary)

    # ---- Step2: 基于特征生成建议
    from app.core.ai_review.tuning import tunable_brief

    tunable = tunable_brief()
    memory_block = ""
    if memory_lines:
        memory_block = (
            "\n\n历史复盘记忆(相似问题的采纳结果与效果, 供归因参考):\n"
            f"{memory_lines}\n"
            "规则: 若某参数调整已被采纳过且后续胜率/盈亏未改善, 不要重复建议; "
            "若已证明有效, 可在建议中引用其效果。"
        )
    review_parser = PydanticOutputParser(pydantic_object=ReviewOutput)
    out = await _call_parsed(
        system="你是一位严谨的量化交易复盘教练, 输出简洁可执行。",
        user=(
            "基于交易行为特征分析与规则诊断, 输出复盘结论。\n\n"
            "关于 patch(可选字段, 只在建议确实对应某个参数调整时才给):\n"
            "- 只允许出现在下方「可调参数清单」中的 group/key, 其它一律不要给 patch, 否则整条建议会被判为不可执行。\n"
            "- to 必须落在该参数标注的「本次允许区间」内(系统对单次变动有 ±20% 硬上限, 超出会被截断)。\n"
            "- 风控、仓位、数据源、手续费、LLM 相关参数**禁止**给出 patch —— 它们直接决定亏损与下单量, 只能由人工修改。\n"
            "- 属于心态、纪律、执行习惯类的建议(例如\"严格执行止损\"), 不要硬凑 patch, 只给 text 即可。\n\n"
            f"可调参数清单(group / key / 当前值 / 本次允许区间):\n{tunable}\n\n"
            f"交易行为特征分析(第一步归纳):\n{profile.model_dump_json(ensure_ascii=False)}\n\n"
            f"规则诊断结果:\n{mat['issue_lines']}\n\n"
            f"近 {closed} 笔已平仓, 胜率 {win_rate}%, 总盈亏 {total_pnl} 元。\n"
            f"买入日的趋势阶段分布:\n{mat['stage_lines']}\n\n"
            f"交易记录(最近):\n{mat['trade_lines']}\n\n"
            f"{memory_block}\n\n"
            f"{review_parser.get_format_instructions()}"
        ),
        llm=llm,
        parser=review_parser,
        feature="ai_review.step2",
        prompt_version=REVIEW_PROMPT_V,
    )
    return out, llm.model_name
