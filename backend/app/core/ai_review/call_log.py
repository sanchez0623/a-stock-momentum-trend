"""LLM 调用记账(第 0 期: token/成本/延迟/降级可见).

每次实际 LLM 调用(含失败)落一条 LlmCall:
- usage 提取: 兼容 OpenAI 兼容 chat/completions JSON 与 langchain AIMessage 两种形态
- prompt_hash: sha256(system + user) 前 16 位, 用于识别重复调用与 prompt 变更
- 记账失败只记日志, 绝不抛给调用方(观测不得影响主流程)
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from app import db
from app.models.models import LlmCall

logger = logging.getLogger(__name__)


def hash_prompt(system: str, user: str) -> str:
    """prompt 内容指纹(sha256 前 16 位), 同内容稳定; 用于识别 prompt 变更/重复调用."""
    raw = f"{system}\n<|sep|>\n{user}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def provider_of(base_url: str) -> str:
    """从 base_url 提取主机名作为 provider 标识(api.deepseek.com / localhost 等)."""
    base = (base_url or "").strip()
    if "://" in base:
        base = base.split("://", 1)[1]
    base = base.split("/", 1)[0]
    return base.split(":")[0] or "unknown"


def extract_usage(resp: Any) -> dict[str, int]:
    """从响应提取 token 用量; 提取不到返回全 0(不抛异常).

    兼容两种形态:
    - OpenAI 兼容 chat/completions JSON: data["usage"] = {prompt_tokens, completion_tokens, total_tokens}
    - langchain AIMessage: usage_metadata = {input_tokens, output_tokens, total_tokens}
      或 response_metadata["token_usage"] = {prompt_tokens, completion_tokens, total_tokens}
    """
    usage: dict[str, Any] = {}
    if isinstance(resp, dict):
        usage = resp.get("usage") or {}
    else:
        meta = getattr(resp, "usage_metadata", None) or {}
        if meta:
            usage = {
                "prompt_tokens": meta.get("input_tokens", 0),
                "completion_tokens": meta.get("output_tokens", 0),
                "total_tokens": meta.get("total_tokens", 0),
            }
        else:
            rm = getattr(resp, "response_metadata", None) or {}
            usage = rm.get("token_usage") or {}
    try:
        return {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    except (TypeError, ValueError):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def record_call(*, feature: str, model: str = "", provider: str = "",
                prompt_hash: str = "", prompt_version: str = "",
                input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
                latency_ms: int = 0, degraded: bool = False,
                ok: bool = True, error: str = "") -> None:
    """落一条 LLM 调用记录(独立短会话, 不掺入调用方事务).

    任何异常只记日志, 不抛给调用方(LLM 主流程的失败已有上层降级逻辑)。
    """
    try:
        row = LlmCall(
            feature=feature, model=model, provider=provider,
            prompt_hash=prompt_hash, prompt_version=prompt_version,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, latency_ms=latency_ms,
            degraded=degraded, ok=ok, error=(error or "")[:500],
        )
        with db.session_scope() as s:
            s.add(row)
            s.commit()
    except Exception as exc:  # noqa: BLE001 - 记账失败不影响主流程
        logger.warning("LLM 调用记账失败(feature=%s): %s", feature, exc)


def started() -> float:
    """调用起点(与 elapsed_ms 配合计算耗时)."""
    return time.monotonic()


def elapsed_ms(start: float) -> int:
    """从起点到当前的毫秒数."""
    return int(round((time.monotonic() - start) * 1000))
