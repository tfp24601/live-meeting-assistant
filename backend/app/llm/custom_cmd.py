"""Provider: a user-supplied shell command — the escape hatch.

Set CUSTOM_LLM_CMD to any command that reads the user prompt on stdin and
prints the reply on stdout. The system prompt arrives in $LLM_SYSTEM_PROMPT;
$LLM_FAST is "1" for the quick loop, "0" for deep dives. Wire in whatever you
like: another vendor CLI, a local llama.cpp wrapper, an SSH hop to a GPU box.
"""
from __future__ import annotations

import asyncio
import logging
import os

from ..config import settings
from . import LLMError

log = logging.getLogger("lma.llm.custom")

SUPPORTS_WEB_SEARCH = False


async def generate(system_prompt: str, user_prompt: str, *, fast: bool = True,
                   timeout: float | None = None, web_search: bool = False):
    cmd = settings.custom_llm_cmd
    if not cmd:
        raise LLMError("custom-command provider needs CUSTOM_LLM_CMD set")
    env = dict(os.environ)
    env["LLM_SYSTEM_PROMPT"] = system_prompt
    env["LLM_FAST"] = "1" if fast else "0"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    eff_timeout = timeout or (settings.suggest_timeout_s if fast else settings.deep_timeout_s)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(user_prompt.encode()), timeout=eff_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"custom command timed out after {eff_timeout}s")
    if proc.returncode != 0:
        raise LLMError(f"custom command exited {proc.returncode}: "
                       f"{stderr_b.decode(errors='replace')[:300]}")
    return stdout_b.decode(errors="replace").strip(), {"provider": "custom-command"}
