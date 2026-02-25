from __future__ import annotations

import os
import re
from typing import Any

_ENV_TEMPLATE = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")
_TOTP_TEMPLATE = re.compile(r"\{\{totp:([A-Za-z_][A-Za-z0-9_]*)\}\}")


def resolve_secrets(auth_config: dict) -> dict:
    """Replace {{env:VAR}} and {{totp:VAR}} in all string values.

    Returns a new dict. The resolved config should never be persisted.
    """

    def _resolve_value(value: Any) -> Any:
        if isinstance(value, str):
            return _resolve_string(value)
        if isinstance(value, dict):
            return {str(k): _resolve_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_resolve_value(item) for item in value]
        return value

    def _resolve_string(value: str) -> str:
        def _env_repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise ValueError(f"Missing environment variable for secret template: {var}")
            return os.environ[var]

        def _totp_repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise ValueError(f"Missing environment variable for TOTP template: {var}")
            secret = os.environ[var]
            try:
                import pyotp
            except ImportError as exc:  # pragma: no cover
                raise ValueError("pyotp is required for {{totp:...}} templates") from exc
            return pyotp.TOTP(secret).now()

        resolved = _ENV_TEMPLATE.sub(_env_repl, value)
        resolved = _TOTP_TEMPLATE.sub(_totp_repl, resolved)
        return resolved

    if not isinstance(auth_config, dict):
        raise TypeError("auth_config must be a dict")
    return _resolve_value(auth_config)
