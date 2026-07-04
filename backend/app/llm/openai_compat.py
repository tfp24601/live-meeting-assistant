"""Provider: any OpenAI-compatible chat endpoint.

Covers Ollama (http://host:11434/v1), OpenRouter, LM Studio, vLLM, GLM/Z.ai,
etc. Config: OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY, OPENAI_COMPAT_MODEL
(+ _DEEP_MODEL optional).
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from . import LLMError

log = logging.getLogger("lma.llm.openai_compat")

SUPPORTS_WEB_SEARCH = False


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    if not settings.openai_compat_base_url or not settings.openai_compat_model:
        raise LLMError("openai-compatible provider needs OPENAI_COMPAT_BASE_URL and OPENAI_COMPAT_MODEL")
    model = settings.openai_compat_model if fast else \
        (settings.openai_compat_deep_model or settings.openai_compat_model)
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    headers = {}
    if settings.openai_compat_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_compat_api_key}"
    async with httpx.AsyncClient(timeout=eff_timeout) as client:
        r = await client.post(
            settings.openai_compat_base_url.rstrip("/") + "/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            headers=headers,
        )
    if r.status_code != 200:
        raise LLMError(f"openai-compatible api {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response shape: {e!r}")
    return text, {"provider": "openai-compatible", "model": model}
