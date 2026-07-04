"""Provider: Ollama Cloud (hosted big open models: GLM, DeepSeek, Qwen, ...).

Preset over Ollama's OpenAI-compatible cloud endpoint. Config:
OLLAMA_CLOUD_API_KEY (from https://ollama.com), OLLAMA_CLOUD_MODEL
(e.g. "glm-4.6" — check ollama.com for current model names), optional
_DEEP_MODEL and _BASE_URL (default https://ollama.com/v1).
"""
from __future__ import annotations

from ..config import settings
from . import LLMError
from .openai_compat import chat

SUPPORTS_WEB_SEARCH = False


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    if not settings.ollama_cloud_api_key:
        raise LLMError("ollama-cloud provider needs OLLAMA_CLOUD_API_KEY set")
    model = settings.ollama_cloud_model if fast else \
        (settings.ollama_cloud_deep_model or settings.ollama_cloud_model)
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    return await chat("ollama-cloud", settings.ollama_cloud_base_url,
                      settings.ollama_cloud_api_key, model,
                      system_prompt, user_prompt, eff_timeout)
