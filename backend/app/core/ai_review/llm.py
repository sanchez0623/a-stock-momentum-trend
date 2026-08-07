"""AI 复盘 - LLM 客户端(方案 §4.10.3, 兼容 OpenAI 协议).

可接 DeepSeek / 通义千问 / Kimi / 本地 Ollama(均兼容 /v1/chat/completions).
不依赖 openai SDK, 直接用 httpx, 超时可配, 异常抛给上层降级.
"""

from __future__ import annotations

import httpx

DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str = DEFAULT_MODEL,
                 temperature: float = 0.3, max_tokens: int = 2000, timeout_sec: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """调用 chat/completions, 返回回复文本."""
        if not self.configured:
            raise LLMError("未配置 LLM API Key")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM 请求超时({self.timeout_sec}s)") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM 返回 {exc.response.status_code}: {exc.response.text[:120]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应格式异常: {str(data)[:120]}") from exc


def build_client_from_config(llm_cfg: dict) -> LLMClient:
    """从 config_manager 的 llm 配置段构建客户端."""
    base_url = (llm_cfg.get("base_url") or DEEPSEEK_BASE)
    model = llm_cfg.get("model") or DEFAULT_MODEL
    # 若 base_url 为空且配了 api_key, 默认指向 DeepSeek(国内可用)
    if not base_url or base_url == "https://api.openai.com/v1":
        base_url = DEEPSEEK_BASE
        model = model if model not in ("gpt-4o-mini",) else DEFAULT_MODEL
    return LLMClient(
        base_url=base_url,
        api_key=llm_cfg.get("api_key", ""),
        model=model,
        temperature=float(llm_cfg.get("temperature", 0.3)),
        max_tokens=int(llm_cfg.get("max_tokens", 2000)),
        timeout_sec=float(llm_cfg.get("timeout_sec", 60)),
    )
