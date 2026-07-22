from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_KEY_ENV_CANDIDATES: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "vertex": ("VERTEX_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


def detect_provider(model_id: str, auth_config: dict[str, Any]) -> str:
    provider = auth_config.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()

    model = model_id.strip().lower()
    if model.startswith(("gpt-", "o1", "o3", "o4", "openai/")) or "openai" in model:
        return "openai"
    if "claude" in model or model.startswith("anthropic/"):
        return "anthropic"
    if "openrouter" in model or model.startswith("openrouter/"):
        return "openrouter"
    if "gemini" in model or model.startswith("google/"):
        return "gemini"
    if "vertex" in model or model.startswith("vertex/"):
        return "vertex"
    return "openai"


def resolve_model_and_api_key(
    auth_config: dict[str, Any],
) -> tuple[str, str | None, str, tuple[str, ...]]:
    model_id = os.getenv("CRAWLER_AUTH_MODEL", "gpt-5.4-nano")
    provider = detect_provider(model_id, auth_config)

    direct_api_key = auth_config.get("api_key")
    if isinstance(direct_api_key, str) and direct_api_key.strip():
        return model_id, direct_api_key.strip(), provider, tuple()

    api_key_env = auth_config.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env.strip():
        key = os.getenv(api_key_env)
        if key:
            return model_id, key, provider, (api_key_env,)
        logger.warning("Auth api_key_env=%s is set but missing in environment", api_key_env)
        return model_id, None, provider, (api_key_env,)

    candidates = PROVIDER_KEY_ENV_CANDIDATES.get(provider, ("OPENAI_API_KEY",))
    for env_name in candidates:
        key = os.getenv(env_name)
        if key:
            return model_id, key, provider, candidates

    return model_id, None, provider, candidates
