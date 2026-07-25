from __future__ import annotations

from typing import Any

import pytest

from app import discovery_model


class _FakeModel:
    key: str | None = None


@pytest.mark.parametrize(
    ("configured_model", "expected_model"),
    [(None, "gpt-5.4-mini"), ("custom-discovery-model", "custom-discovery-model")],
)
def test_build_live_discovery_adapter_uses_independent_discovery_model_default(
    monkeypatch: pytest.MonkeyPatch,
    configured_model: str | None,
    expected_model: str,
) -> None:
    captured: dict[str, Any] = {}
    fake_model = _FakeModel()

    if configured_model is None:
        monkeypatch.delenv("CRAWLER_DISCOVERY_MODEL", raising=False)
    else:
        monkeypatch.setenv("CRAWLER_DISCOVERY_MODEL", configured_model)
    monkeypatch.setenv("CRAWLER_AUTH_MODEL", "auth-model-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        discovery_model.llm,
        "get_async_model",
        lambda model_id: captured.setdefault("model_id", model_id) and fake_model,
    )

    adapter = discovery_model.build_live_discovery_adapter()

    assert captured["model_id"] == expected_model
    assert adapter._model is fake_model
    assert fake_model.key == "test-key"
