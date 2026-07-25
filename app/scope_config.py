from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_SCOPE_CONFIG_KEYS = {
    "max_depth",
    "rate_limit",
    "crawl_scope",
    "exclude_filters",
    "exclude_regex",
    "field_scope",
    "concurrency",
    "parallelism",
    "crawl_duration",
    "timeout",
}


class ScopeConfigValidationError(ValueError):
    pass


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


def _validate_scope_config_shape(scope_config: dict[str, Any]) -> None:
    unknown_keys = sorted(set(scope_config) - _ALLOWED_SCOPE_CONFIG_KEYS)
    if unknown_keys:
        raise ScopeConfigValidationError(f"Unknown scope_config keys: {', '.join(unknown_keys)}")

    _require_int_in_range(scope_config, "max_depth", minimum=0, maximum=64)
    _require_int_in_range(scope_config, "rate_limit", minimum=1, maximum=5000)
    _require_int_in_range(scope_config, "concurrency", minimum=1, maximum=2000)
    _require_int_in_range(scope_config, "parallelism", minimum=1, maximum=2000)
    _require_int_in_range(scope_config, "timeout", minimum=1, maximum=3600)

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


def validate_scope_config(scope_config: dict[str, Any] | None) -> None:
    """Validate cross-field constraints inside `scope_config`."""

    if not scope_config:
        logger.debug("Scope config empty; skipping validation")
        return

    _validate_scope_config_shape(scope_config)

    logger.debug("Scope config validation passed")
