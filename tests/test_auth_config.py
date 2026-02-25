from __future__ import annotations

import pytest

from app.auth_config import AuthConfigValidationError, validate_auth_config


def test_validate_auth_config_accepts_manual_headers_mode() -> None:
    validate_auth_config(
        {
            "headers": ["Authorization: Bearer {{env:APP_TOKEN}}"],
            "success_indicator": "Dashboard",
        }
    )


def test_validate_auth_config_rejects_unknown_keys() -> None:
    with pytest.raises(AuthConfigValidationError, match="Unknown auth_config keys"):
        validate_auth_config({"headers": ["Cookie: a=b"], "unknown": "x"})


def test_validate_auth_config_accepts_plaintext_credentials() -> None:
    validate_auth_config(
        {
            "login_url": "https://example.com/login",
            "credentials": {"password": "plain-secret"},
        }
    )


def test_validate_auth_config_accepts_secret_templates() -> None:
    validate_auth_config(
        {
            "login_url": "https://example.com/login",
            "credentials": {
                "email": "{{env:APP_EMAIL}}",
                "password": "{{env:APP_PASS}}",
                "otp": "{{totp:APP_TOTP_SECRET}}",
            },
        }
    )
