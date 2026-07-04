"""Pluggable LLM providers.

Selected via LLM_PROVIDER: claude-cli (default) | anthropic-api |
openai-compatible | custom-command. All providers expose:

    async generate(system_prompt, user_prompt, *, fast=True,
                   timeout=None, web_search=False) -> (text, meta)

fast=True is the continuous suggestion loop (favor speed); fast=False is the
deep-dive tier. web_search=True is only honored by providers that support it
(check supports_web_search() first).
"""
from __future__ import annotations

from importlib import import_module

from ..config import settings


class LLMError(RuntimeError):
    pass


_MODULES = {
    "claude-cli": ".claude_cli",
    "anthropic-api": ".anthropic_api",
    "openai-compatible": ".openai_compat",
    "custom-command": ".custom_cmd",
}


def _provider():
    name = settings.llm_provider
    mod = _MODULES.get(name)
    if mod is None:
        raise LLMError(f"unknown LLM_PROVIDER {name!r} (choose from {sorted(_MODULES)})")
    return import_module(mod, package=__name__)


def provider_name() -> str:
    return settings.llm_provider


def supports_web_search() -> bool:
    try:
        return bool(getattr(_provider(), "SUPPORTS_WEB_SEARCH", False))
    except LLMError:
        return False


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    return await _provider().generate(
        system_prompt, user_prompt, fast=fast, timeout=timeout, web_search=web_search,
    )
