from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ALLOWED_SCOPE_CONFIG_KEYS = {
    "max_depth",
    "rate_limit",
    "max_pages",
    "crawl_scope",
    "exclude_filters",
    "exclude_regex",
    "field_scope",
    "concurrency",
    "parallelism",
    "crawl_duration",
    "timeout",
    "headless",
    "cdp_url",
    "chrome_ws_url",
    "no_incognito",
    "system_chrome",
    "system_chrome_path",
}


class ScopeConfigValidationError(ValueError):
    pass


def _coerce_bool(value: Any) -> bool:
    if value is True or value is False:
        return value
    if value is None:
        return False
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _is_bool_like(value: Any) -> bool:
    if value is True or value is False:
        return True
    if isinstance(value, int):
        return value in {0, 1}
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
            "0",
            "false",
            "no",
            "n",
            "off",
            "",
        }
    return False


def _require_int_in_range(
    scope_config: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    value = scope_config.get(key)
    if value is None:
        return
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScopeConfigValidationError(f"`{key}` must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ScopeConfigValidationError(f"`{key}` must be between {minimum} and {maximum}")


def _validate_ws_url(value: str, key: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ScopeConfigValidationError(f"`{key}` must be a valid ws:// or wss:// URL")


def _validate_scope_config_shape(scope_config: dict[str, Any]) -> None:
    unknown_keys = sorted(set(scope_config) - _ALLOWED_SCOPE_CONFIG_KEYS)
    if unknown_keys:
        raise ScopeConfigValidationError(f"Unknown scope_config keys: {', '.join(unknown_keys)}")

    _require_int_in_range(scope_config, "max_depth", minimum=0, maximum=64)
    _require_int_in_range(scope_config, "rate_limit", minimum=1, maximum=5000)
    _require_int_in_range(scope_config, "max_pages", minimum=0, maximum=5_000_000)
    _require_int_in_range(scope_config, "concurrency", minimum=1, maximum=2000)
    _require_int_in_range(scope_config, "parallelism", minimum=1, maximum=2000)
    _require_int_in_range(scope_config, "timeout", minimum=1, maximum=3600)

    for key in ("headless", "system_chrome", "no_incognito"):
        value = scope_config.get(key)
        if value is None:
            continue
        if not _is_bool_like(value):
            raise ScopeConfigValidationError(f"`{key}` must be a boolean-like value")

    crawl_scope = scope_config.get("crawl_scope")
    if crawl_scope is not None and not isinstance(crawl_scope, str):
        raise ScopeConfigValidationError("`crawl_scope` must be a string")

    field_scope = scope_config.get("field_scope")
    if field_scope is not None:
        if not isinstance(field_scope, str) or not field_scope.strip():
            raise ScopeConfigValidationError("`field_scope` must be a non-empty string")
        if len(field_scope) > 32:
            raise ScopeConfigValidationError("`field_scope` must be at most 32 characters")

    crawl_duration = scope_config.get("crawl_duration")
    if crawl_duration is not None:
        if not isinstance(crawl_duration, str) or not crawl_duration.strip():
            raise ScopeConfigValidationError("`crawl_duration` must be a non-empty string")
        if len(crawl_duration) > 64:
            raise ScopeConfigValidationError("`crawl_duration` must be at most 64 characters")

    exclude_filters = scope_config.get("exclude_filters")
    if exclude_filters is not None:
        if not isinstance(exclude_filters, list):
            raise ScopeConfigValidationError("`exclude_filters` must be a list of strings")
        if len(exclude_filters) > 250:
            raise ScopeConfigValidationError("`exclude_filters` cannot contain more than 250 items")
        for item in exclude_filters:
            if not isinstance(item, str) or not item.strip():
                raise ScopeConfigValidationError("`exclude_filters` must contain non-empty strings")

    exclude_regex = scope_config.get("exclude_regex")
    if exclude_regex is not None and not isinstance(exclude_regex, str):
        raise ScopeConfigValidationError("`exclude_regex` must be a string")

    cdp_url = scope_config.get("cdp_url")
    chrome_ws_url = scope_config.get("chrome_ws_url")
    if cdp_url is not None and not isinstance(cdp_url, str):
        raise ScopeConfigValidationError("`cdp_url` must be a string")
    if chrome_ws_url is not None and not isinstance(chrome_ws_url, str):
        raise ScopeConfigValidationError("`chrome_ws_url` must be a string")
    if isinstance(cdp_url, str) and cdp_url.strip():
        _validate_ws_url(cdp_url.strip(), "cdp_url")
    if isinstance(chrome_ws_url, str) and chrome_ws_url.strip():
        _validate_ws_url(chrome_ws_url.strip(), "chrome_ws_url")
    if (
        isinstance(cdp_url, str)
        and isinstance(chrome_ws_url, str)
        and cdp_url.strip()
        and chrome_ws_url.strip()
        and cdp_url.strip() != chrome_ws_url.strip()
    ):
        raise ScopeConfigValidationError(
            "`cdp_url` and `chrome_ws_url` must match when both are provided"
        )

    system_chrome_path = scope_config.get("system_chrome_path")
    if system_chrome_path is not None:
        if not isinstance(system_chrome_path, str) or not system_chrome_path.strip():
            raise ScopeConfigValidationError("`system_chrome_path` must be a non-empty string")
        if len(system_chrome_path) > 4096:
            raise ScopeConfigValidationError("`system_chrome_path` must be at most 4096 characters")


def validate_scope_config(scope_config: dict[str, Any] | None) -> None:
    """Validate cross-field constraints inside `scope_config`."""

    if not scope_config:
        logger.debug("Scope config empty; skipping validation")
        return

    _validate_scope_config_shape(scope_config)

    headless = _coerce_bool(scope_config.get("headless", True))

    cdp_url = scope_config.get("cdp_url") or scope_config.get("chrome_ws_url")
    has_cdp_url = isinstance(cdp_url, str) and bool(cdp_url.strip())

    system_chrome = _coerce_bool(scope_config.get("system_chrome"))

    system_chrome_path = scope_config.get("system_chrome_path")
    has_system_chrome_path = isinstance(system_chrome_path, str) and bool(
        system_chrome_path.strip()
    )

    if (has_cdp_url or system_chrome or has_system_chrome_path) and not headless:
        logger.warning("Invalid scope_config: chrome options require headless=true")
        raise ScopeConfigValidationError(
            "`cdp_url`/`chrome_ws_url`, `system_chrome`, and `system_chrome_path` "
            "can only be used when `headless` is enabled."
        )

    if has_cdp_url and (system_chrome or has_system_chrome_path):
        logger.warning(
            "Invalid scope_config: cdp_url is mutually exclusive with system chrome options"
        )
        raise ScopeConfigValidationError(
            "`cdp_url`/`chrome_ws_url` is mutually exclusive with "
            "`system_chrome` and `system_chrome_path`."
        )

    logger.debug("Scope config validation passed")
