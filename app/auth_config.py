from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_ALLOWED_AUTH_CONFIG_KEYS = {
    "headers",
    "login_url",
    "credentials",
    "instructions",
    "success_indicator",
    "max_steps",
    "provider",
    "api_key_env",
}
_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY_PATTERN = re.compile(r"(?i)(password|token|secret|api[_-]?key|otp|passcode)")


class AuthConfigValidationError(ValueError):
    pass


def _validate_headers(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise AuthConfigValidationError("`auth_config.headers` must be a list of header strings")
    if len(value) > 100:
        raise AuthConfigValidationError("`auth_config.headers` cannot exceed 100 entries")
    for header in value:
        if not isinstance(header, str) or not header.strip():
            raise AuthConfigValidationError(
                "`auth_config.headers` entries must be non-empty strings"
            )
        if len(header) > 4096:
            raise AuthConfigValidationError("`auth_config.headers` entries are too long")
        if ":" not in header:
            raise AuthConfigValidationError(
                "`auth_config.headers` entries must use 'Name: Value' format"
            )


def _validate_login_url(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise AuthConfigValidationError("`auth_config.login_url` must be a non-empty string")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AuthConfigValidationError("`auth_config.login_url` must be a valid http(s) URL")


def _validate_credentials(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise AuthConfigValidationError("`auth_config.credentials` must be an object")
    if len(value) > 50:
        raise AuthConfigValidationError("`auth_config.credentials` cannot exceed 50 entries")
    for key, entry in value.items():
        key_name = str(key)
        if not key_name.strip():
            raise AuthConfigValidationError("`auth_config.credentials` keys must be non-empty")
        if not isinstance(entry, (str, int, float, bool)) and entry is not None:
            raise AuthConfigValidationError(
                "`auth_config.credentials` values must be scalar JSON values"
            )
        if isinstance(entry, str) and len(entry) > 2048:
            raise AuthConfigValidationError("`auth_config.credentials` values are too long")


def _validate_string_field(value: Any, field_name: str, *, max_len: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise AuthConfigValidationError(f"`auth_config.{field_name}` must be a non-empty string")
    if len(value) > max_len:
        raise AuthConfigValidationError(
            f"`auth_config.{field_name}` must be at most {max_len} characters"
        )


def _validate_max_steps(value: Any) -> None:
    if value is None:
        return
    try:
        steps = int(value)
    except (TypeError, ValueError) as exc:
        raise AuthConfigValidationError("`auth_config.max_steps` must be an integer") from exc
    if steps < 1 or steps > 500:
        raise AuthConfigValidationError("`auth_config.max_steps` must be between 1 and 500")


def _validate_api_key_env(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise AuthConfigValidationError("`auth_config.api_key_env` must be a non-empty string")
    if not _ENV_VAR_PATTERN.match(value.strip()):
        raise AuthConfigValidationError(
            "`auth_config.api_key_env` must be a valid environment variable name"
        )


def validate_auth_config(auth_config: dict[str, Any] | None) -> None:
    """Validate user-provided auth config before persisting it."""

    if not auth_config:
        return
    if not isinstance(auth_config, dict):
        raise AuthConfigValidationError("`auth_config` must be an object")

    if "api_key" in auth_config:
        raise AuthConfigValidationError(
            "`auth_config.api_key` is not allowed; use `api_key_env` and environment variables"
        )

    unknown_keys = sorted(set(auth_config) - _ALLOWED_AUTH_CONFIG_KEYS)
    if unknown_keys:
        raise AuthConfigValidationError(f"Unknown auth_config keys: {', '.join(unknown_keys)}")

    _validate_headers(auth_config.get("headers"))
    _validate_login_url(auth_config.get("login_url"))
    _validate_credentials(auth_config.get("credentials"))
    _validate_string_field(auth_config.get("instructions"), "instructions", max_len=8000)
    _validate_string_field(auth_config.get("success_indicator"), "success_indicator", max_len=2048)
    _validate_max_steps(auth_config.get("max_steps"))
    _validate_string_field(auth_config.get("provider"), "provider", max_len=64)
    _validate_api_key_env(auth_config.get("api_key_env"))
