"""Runtime configuration.

Three layers, later wins: field default -> environment variable ->
settings.json (written by the settings screen). settings.json lives at the
repo root (gitignored, survives deploys) and is hot-reloadable: PUT
/api/settings writes it and re-applies immediately. Fields marked
restart=True are read once at startup by their consumers, so edits to them
only take effect after a service restart (the UI labels these).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("lma.config")

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERRIDES_PATH = Path(os.getenv("LMA_SETTINGS", REPO_ROOT / "settings.json"))

_write_lock = threading.Lock()


@dataclass(frozen=True)
class Field:
    attr: str
    env: str
    type: type
    default: object
    secret: bool = False    # masked in the settings API
    restart: bool = False   # consumer caches it at startup


FIELDS: list[Field] = [
    # identity / deployment
    Field("user_name", "USER_NAME", str, "the user"),
    Field("user_context", "USER_CONTEXT", str, ""),
    Field("public_url", "LMA_PUBLIC_URL", str, ""),
    Field("host", "LMA_HOST", str, "0.0.0.0", restart=True),
    Field("port", "LMA_PORT", int, 5005, restart=True),
    # LLM provider
    Field("llm_provider", "LLM_PROVIDER", str, "claude-cli"),
    Field("claude_bin", "CLAUDE_BIN", str, "claude"),
    Field("suggest_model", "SUGGEST_MODEL", str, "sonnet"),
    Field("suggest_effort", "SUGGEST_EFFORT", str, "low"),
    Field("deep_model", "DEEP_MODEL", str, "sonnet"),
    Field("deep_effort", "DEEP_EFFORT", str, "medium"),
    Field("deep_timeout_s", "DEEP_TIMEOUT_S", float, 120.0),
    Field("anthropic_api_key", "ANTHROPIC_API_KEY", str, "", secret=True),
    Field("anthropic_model", "ANTHROPIC_MODEL", str, "claude-sonnet-5"),
    Field("anthropic_deep_model", "ANTHROPIC_DEEP_MODEL", str, "claude-sonnet-5"),
    Field("openai_compat_base_url", "OPENAI_COMPAT_BASE_URL", str, ""),
    Field("openai_compat_api_key", "OPENAI_COMPAT_API_KEY", str, "", secret=True),
    Field("openai_compat_model", "OPENAI_COMPAT_MODEL", str, ""),
    Field("openai_compat_deep_model", "OPENAI_COMPAT_DEEP_MODEL", str, ""),
    Field("custom_llm_cmd", "CUSTOM_LLM_CMD", str, ""),
    Field("ollama_local_base_url", "OLLAMA_LOCAL_BASE_URL", str, "http://127.0.0.1:11434/v1"),
    Field("ollama_local_model", "OLLAMA_LOCAL_MODEL", str, ""),
    Field("ollama_local_deep_model", "OLLAMA_LOCAL_DEEP_MODEL", str, ""),
    Field("ollama_cloud_base_url", "OLLAMA_CLOUD_BASE_URL", str, "https://ollama.com/v1"),
    Field("ollama_cloud_api_key", "OLLAMA_CLOUD_API_KEY", str, "", secret=True),
    Field("ollama_cloud_model", "OLLAMA_CLOUD_MODEL", str, ""),
    Field("ollama_cloud_deep_model", "OLLAMA_CLOUD_DEEP_MODEL", str, ""),
    # whisper (loaded once at startup)
    Field("whisper_model", "WHISPER_MODEL", str, "small.en", restart=True),
    Field("whisper_device", "WHISPER_DEVICE", str, "cuda", restart=True),
    Field("whisper_compute_type", "WHISPER_COMPUTE_TYPE", str, "float16", restart=True),
    Field("whisper_language", "WHISPER_LANGUAGE", str, "", restart=True),
    Field("whisper_beam_size", "WHISPER_BEAM_SIZE", int, 1, restart=True),
    # endpointing (picked up per new connection)
    Field("vad_threshold", "VAD_THRESHOLD", float, 0.008),
    Field("endpoint_silence_ms", "ENDPOINT_SILENCE_MS", int, 800),
    Field("min_utt_ms", "MIN_UTT_MS", int, 350),
    Field("max_utt_ms", "MAX_UTT_MS", int, 15000),
    Field("preroll_ms", "PREROLL_MS", int, 250),
    # suggestions
    Field("suggest_enabled", "SUGGEST_ENABLED", bool, True),
    Field("suggest_min_interval_s", "SUGGEST_MIN_INTERVAL_S", float, 12.0),
    Field("suggest_min_new_chars", "SUGGEST_MIN_NEW_CHARS", int, 120),
    Field("suggest_timeout_s", "SUGGEST_TIMEOUT_S", float, 90.0),
    Field("suggest_transcript_chars", "SUGGEST_TRANSCRIPT_CHARS", int, 6000),
    # RAG
    Field("rag_enabled", "RAG_ENABLED", bool, True),
    Field("qdrant_url", "QDRANT_URL", str, "http://127.0.0.1:6333", restart=True),
    Field("rag_collection", "RAG_COLLECTION", str, "lma_knowledge", restart=True),
    Field("rag_top_k", "RAG_TOP_K", int, 5),
    Field("rag_min_score", "RAG_MIN_SCORE", float, 0.45),
    Field("rag_query_chars", "RAG_QUERY_CHARS", int, 700),
    # speaker verification
    Field("speaker_device", "SPEAKER_DEVICE", str, "cpu", restart=True),
    Field("speaker_threshold", "SPEAKER_THRESHOLD", float, 0.40),
    Field("enroll_target_s", "ENROLL_TARGET_S", float, 20.0),
    # echo suppression
    Field("echo_suppress", "ECHO_SUPPRESS", bool, True),
    Field("echo_window_s", "ECHO_WINDOW_S", float, 12.0),
    Field("echo_similarity", "ECHO_SIMILARITY", float, 0.82),
]

_BY_ENV = {f.env: f for f in FIELDS}


def _coerce(field: Field, raw: object):
    if raw is None:
        return field.default
    if field.type is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("false", "0", "no", "off", "")
    try:
        return field.type(raw)
    except (TypeError, ValueError):
        log.warning("bad value for %s: %r; using default", field.env, raw)
        return field.default


def _load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        log.warning("ignoring corrupt %s: %s", OVERRIDES_PATH, e)
        return {}


class Settings:
    sample_rate: int = 16000  # fixed by the browser worklet


settings = Settings()


def reload() -> None:
    overrides = _load_overrides()
    for f in FIELDS:
        raw = overrides.get(f.env, os.getenv(f.env))
        setattr(settings, f.attr, _coerce(f, raw))


def update_overrides(changes: dict) -> list[str]:
    """Merge changes (keyed by ENV name) into settings.json and re-apply.

    Empty-string value removes the override (falls back to env/default).
    Returns the ENV names of changed restart-required fields.
    """
    restart_needed: list[str] = []
    with _write_lock:
        overrides = _load_overrides()
        for env, value in changes.items():
            f = _BY_ENV.get(env)
            if f is None:
                raise KeyError(env)
            old = getattr(settings, f.attr)
            if value in ("", None):
                overrides.pop(env, None)
            else:
                overrides[env] = str(value) if not isinstance(value, bool) else value
            new = _coerce(f, overrides.get(env, os.getenv(env)))
            if f.restart and new != old:
                restart_needed.append(env)
        OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2) + "\n")
    reload()
    return restart_needed


def describe() -> list[dict]:
    """Settings inventory for the API; secret values masked to set/unset."""
    out = []
    for f in FIELDS:
        value = getattr(settings, f.attr)
        out.append({
            "env": f.env,
            "type": f.type.__name__,
            "value": ("__SET__" if value else "") if f.secret else value,
            "secret": f.secret,
            "restart": f.restart,
            "default": f.default,
        })
    return out


reload()
