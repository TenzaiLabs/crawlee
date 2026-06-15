from __future__ import annotations

import pytest

from app.auth_secrets import resolve_secrets


def test_resolve_secrets_totp_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyotp

    monkeypatch.setattr(pyotp.TOTP, "now", lambda self: "654321")

    resolved = resolve_secrets({"credentials": {"otp": "{{totp_seed:JBSWY3DPEHPK3PXP}}"}})

    assert resolved["credentials"]["otp"] == "654321"
