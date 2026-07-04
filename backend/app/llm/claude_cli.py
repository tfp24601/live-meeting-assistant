"""Provider: the `claude` CLI, billed to a Claude subscription (Pro/Max).

Requires `claude` installed and logged in on THIS host (`claude login`, pick
the subscription option) — auth is machine-level, there is no per-request key.

Pattern notes:
- ANTHROPIC_API_KEY is stripped from the child env; if the CLI sees it, it
  switches to API-key billing and an old request schema.
- system prompt as a CLI arg, user prompt on stdin, --output-format json.
- stdout may carry notice lines before the JSON, so we locate it.
- tools must ALSO be pre-authorized via --allowedTools: headless mode has no
  permission prompt, so a tool that is merely enabled gets denied on use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from ..config import settings
from . import LLMError

log = logging.getLogger("lma.llm.claude_cli")

SUPPORTS_WEB_SEARCH = True

_FALLBACK_BINS = ("/usr/local/bin/claude", os.path.expanduser("~/.local/bin/claude"))


def _resolve_bin() -> str:
    found = shutil.which(settings.claude_bin)
    if found:
        return found
    for candidate in _FALLBACK_BINS:
        if os.access(candidate, os.X_OK):
            return candidate
    raise LLMError(f"claude binary not found (looked for {settings.claude_bin!r})")


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _extract_result(stdout: str) -> dict:
    idx = stdout.find('{"type"')
    if idx < 0:
        idx = stdout.find("{")
    if idx < 0:
        raise LLMError(f"no JSON in claude output: {stdout[:200]!r}")
    return json.loads(stdout[idx:])


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    model = settings.suggest_model if fast else settings.deep_model
    effort = settings.suggest_effort if fast else settings.deep_effort
    tools = "WebSearch" if web_search else ""
    argv = [
        _resolve_bin(), "-p",
        "--model", model,
        "--effort", effort,
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--tools", tools,
        "--no-session-persistence",
    ]
    if tools:
        argv += ["--allowedTools", tools]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(user_prompt.encode()), timeout=eff_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"claude timed out after {eff_timeout}s")

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace").strip()
    if stderr:
        log.debug("claude stderr: %s", stderr[:500])
    if proc.returncode != 0:
        raise LLMError(f"claude exited {proc.returncode}: {stderr[:300] or stdout[:300]}")

    payload = _extract_result(stdout)
    if payload.get("is_error"):
        raise LLMError(f"claude error result: {str(payload.get('result'))[:300]}")
    return str(payload.get("result", "")), {
        "provider": "claude-cli", "model": model,
        "duration_ms": payload.get("duration_ms"),
    }
