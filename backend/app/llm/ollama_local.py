"""Provider: a local Ollama instance (fully private, no cloud).

Preset over the OpenAI-compatible endpoint Ollama exposes. Config:
OLLAMA_LOCAL_BASE_URL (default http://127.0.0.1:11434/v1, relative to the
backend host), OLLAMA_LOCAL_MODEL (e.g. "llama3.3"), optional _DEEP_MODEL.
First call after idle may be slow while Ollama loads the model.
"""
from __future__ import annotations

from ..config import settings
from .openai_compat import chat

SUPPORTS_WEB_SEARCH = False


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    model = settings.ollama_local_model if fast else \
        (settings.ollama_local_deep_model or settings.ollama_local_model)
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    return await chat("ollama-local", settings.ollama_local_base_url, "",
                      model, system_prompt, user_prompt, eff_timeout)
