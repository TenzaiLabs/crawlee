from __future__ import annotations

import argparse

import pytest

from app import cli


def _scope_args(**overrides):
    base = {
        "scope_config_json": None,
        "scope_config_file": None,
        "headless": False,
        "cdp_url": None,
        "system_chrome": False,
        "system_chrome_path": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _auth_args(**overrides):
    base = {
        "auth_config_json": None,
        "auth_config_file": None,
        "auth_header": None,
        "auth_login_url": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_build_scope_config_from_json_string() -> None:
    args = _scope_args(scope_config_json='{"max_depth":2,"exclude_filters":["/logout"]}')

    scope_config = cli._build_scope_config(args)

    assert scope_config == {"max_depth": 2, "exclude_filters": ["/logout"]}


def test_build_scope_config_from_json_file(tmp_path) -> None:
    path = tmp_path / "scope.json"
    path.write_text('{"rate_limit":5,"max_pages":100}', encoding="utf-8")
    args = _scope_args(scope_config_file=str(path))

    scope_config = cli._build_scope_config(args)

    assert scope_config == {"rate_limit": 5, "max_pages": 100}


def test_build_scope_config_merges_flag_overrides() -> None:
    args = _scope_args(scope_config_json='{"headless":false}', headless=True, system_chrome=True)

    scope_config = cli._build_scope_config(args)

    assert scope_config == {"headless": True, "system_chrome": True}


def test_build_scope_config_rejects_both_json_sources() -> None:
    args = _scope_args(scope_config_json='{"max_depth":1}', scope_config_file="scope.json")

    with pytest.raises(ValueError, match="mutually exclusive"):
        cli._build_scope_config(args)


def test_build_scope_config_rejects_non_object_json() -> None:
    args = _scope_args(scope_config_json='["not","an","object"]')

    with pytest.raises(ValueError, match="JSON object"):
        cli._build_scope_config(args)


def test_build_scope_config_rejects_invalid_json() -> None:
    args = _scope_args(scope_config_json="{bad json")

    with pytest.raises(ValueError, match="Invalid --scope-config-json"):
        cli._build_scope_config(args)


# --- auth_config tests ---


def test_build_auth_config_from_json_string() -> None:
    args = _auth_args(auth_config_json='{"login_url":"https://example.com/login","max_steps":10}')

    auth_config = cli._build_auth_config(args)

    assert auth_config == {"login_url": "https://example.com/login", "max_steps": 10}


def test_build_auth_config_from_json_file(tmp_path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"headers":["Authorization: Bearer {{env:TOKEN}}"]}', encoding="utf-8")
    args = _auth_args(auth_config_file=str(path))

    auth_config = cli._build_auth_config(args)

    assert auth_config == {"headers": ["Authorization: Bearer {{env:TOKEN}}"]}


def test_build_auth_config_from_header_flags() -> None:
    args = _auth_args(auth_header=["Authorization: Bearer tok", "Cookie: session=abc"])

    auth_config = cli._build_auth_config(args)

    assert auth_config == {"headers": ["Authorization: Bearer tok", "Cookie: session=abc"]}


def test_build_auth_config_login_url_flag() -> None:
    args = _auth_args(auth_login_url="https://example.com/login")

    auth_config = cli._build_auth_config(args)

    assert auth_config == {"login_url": "https://example.com/login"}


def test_build_auth_config_merges_flags_over_json() -> None:
    args = _auth_args(
        auth_config_json='{"instructions":"Fill the form"}',
        auth_login_url="https://example.com/login",
    )

    auth_config = cli._build_auth_config(args)

    assert auth_config == {
        "instructions": "Fill the form",
        "login_url": "https://example.com/login",
    }


def test_build_auth_config_returns_none_when_empty() -> None:
    args = _auth_args()

    auth_config = cli._build_auth_config(args)

    assert auth_config is None


def test_build_auth_config_rejects_both_json_sources() -> None:
    args = _auth_args(auth_config_json='{"max_steps":1}', auth_config_file="auth.json")

    with pytest.raises(ValueError, match="mutually exclusive"):
        cli._build_auth_config(args)


def test_build_auth_config_rejects_non_object_json() -> None:
    args = _auth_args(auth_config_json='["not","an","object"]')

    with pytest.raises(ValueError, match="JSON object"):
        cli._build_auth_config(args)


def test_build_auth_config_rejects_invalid_json() -> None:
    args = _auth_args(auth_config_json="{bad json")

    with pytest.raises(ValueError, match="Invalid --auth-config-json"):
        cli._build_auth_config(args)
