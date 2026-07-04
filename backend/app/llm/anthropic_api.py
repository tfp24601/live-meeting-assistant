"""Provider: the Anthropic API directly (pay-as-you-go, ANTHROPIC_API_KEY)."""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from . import LLMError

log = logging.getLogger("lma.llm.anthropic")

SUPPORTS_WEB_SEARCH = False
API_URL = "https://api.anthropic.com/v1/messages"


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    if not settings.anthropic_api_key:
        raise LLMError("anthropic-api provider needs ANTHROPIC_API_KEY set")
    model = settings.anthropic_model if fast else settings.anthropic_deep_model
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    async with httpx.AsyncClient(timeout=eff_timeout) as client:
        r = await client.post(API_URL, json=payload, headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        })
    if r.status_code != 200:
        raise LLMError(f"anthropic api {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, {"provider": "anthropic-api", "model": model}
