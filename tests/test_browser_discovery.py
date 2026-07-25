from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import cast

import pytest

from app import browser_discovery, discovery_model
from app.cdp_observer import NetworkRequest, NetworkResponse


def _control(
    ref: str,
    *,
    text: str,
    tag: str = "button",
    type_: str = "button",
    role: str = "",
    disabled: bool = False,
    href: str = "",
) -> browser_discovery.DiscoveryControl:
    return browser_discovery.DiscoveryControl(
        ref=ref,
        identity=f"identity-{ref}",
        tag=tag,
        type=type_,
        role=role,
        name="",
        label="",
        text=text,
        href=href,
        form_method="",
        form_action="",
        disabled=disabled,
    )


def _state(*controls: browser_discovery.DiscoveryControl) -> browser_discovery.DiscoveryPageState:
    return browser_discovery.DiscoveryPageState(
        url="https://example.com/workflow",
        title="Workflow",
        visible_text="Workflow controls",
        controls=controls,
        fingerprint="state-1",
    )


def test_select_candidates_preserves_katana_observations_and_ranks_features() -> None:
    candidates = browser_discovery.select_candidates(
        target_url="https://example.com",
        landing_url="https://example.com/dashboard",
        katana_records=[
            {
                "method": "GET",
                "url": "https://example.com/plain",
                "status": 200,
                "content_type": "text/html",
            },
            {
                "method": "GET",
                "url": "https://example.com/form",
                "status": 200,
                "content_type": "text/html; charset=utf-8",
                "features": {"forms": [{"method": "POST"}]},
            },
            {
                "method": "GET",
                "url": "https://example.com/form",
                "status": 200,
                "content_type": "text/html",
                "features": {"knowledgebase": {"type": "form"}},
            },
            {
                "method": "GET",
                "url": "https://example.com/image.png",
                "status": 200,
                "content_type": "image/png",
            },
            {
                "method": "GET",
                "url": "https://example.com/app/projects",
                "status": None,
                "content_type": None,
                "tag": "js",
                "attribute": "regex",
            },
            {
                "method": "GET",
                "url": "https://example.com/api/projects",
                "status": None,
                "content_type": None,
                "tag": "js",
                "attribute": "regex",
            },
            {
                "method": "GET",
                "url": "https://example.com/form.action",
                "status": None,
                "content_type": None,
                "tag": "js",
                "attribute": "regex",
            },
        ],
    )

    assert [(candidate.url, candidate.reason) for candidate in candidates] == [
        ("https://example.com", "target"),
        ("https://example.com/dashboard", "authenticated-landing"),
        ("https://example.com/form", "katana-classification"),
        ("https://example.com/form", "katana-form"),
        ("https://example.com/app/projects", "katana-client-route"),
        ("https://example.com/plain", "katana-html"),
    ]


def test_deterministic_action_clicks_only_unambiguous_buttons() -> None:
    state = _state(
        _control("f0e1", text="Disabled", disabled=True),
        _control("f0e2", text="Reveal tools"),
    )

    action = browser_discovery.deterministic_action(state, set())

    assert action == browser_discovery.DiscoveryAction(
        kind=browser_discovery.DiscoveryActionKind.click,
        target="f0e2",
        source="deterministic",
    )
    assert (
        browser_discovery.deterministic_action(
            state,
            {(state.url, "identity-f0e2")},
        )
        is None
    )


def test_deterministic_action_defers_ambiguous_controls() -> None:
    state = _state(
        _control("f0e0", text="First"),
        _control("f0e1", text="Second"),
    )

    assert browser_discovery.deterministic_action(state, set()) is None

    competing_submit = _state(
        _control("f0e0", text="Reveal"),
        _control("f0e1", text="Validate", type_="submit"),
    )
    assert browser_discovery.deterministic_action(competing_submit, set()) is None


def test_model_eligibility_excludes_link_only_states() -> None:
    link_only = _state(
        _control(
            "f0e0",
            text="Next day",
            tag="a",
            type_="",
            href="https://example.com/calendar/tomorrow",
        )
    )
    form_state = _state(_control("f0e1", text="Validate", type_="submit"))

    assert not browser_discovery.has_model_eligible_control(link_only, set())
    assert browser_discovery.has_model_eligible_control(form_state, set())


def test_same_document_hash_candidates_include_client_routes_only() -> None:
    state = replace(
        _state(
            _control(
                "f0e0",
                text="Templates",
                tag="a",
                type_="",
                href="https://example.com/#/templates",
            ),
            _control(
                "f0e1",
                text="Templates duplicate",
                tag="a",
                type_="",
                href="#/templates",
            ),
            _control(
                "f0e2",
                text="Heading",
                tag="a",
                type_="",
                href="#heading",
            ),
            _control(
                "f0e3",
                text="Other document",
                tag="a",
                type_="",
                href="/other#/settings",
            ),
            _control(
                "f0e4",
                text="External",
                tag="a",
                type_="",
                href="https://other.example/#/admin",
            ),
        ),
        url="https://example.com/#/workbench",
    )

    candidates = browser_discovery.same_document_hash_candidates(state)

    assert [candidate.url for candidate in candidates] == [
        "https://example.com/#/templates",
        "https://example.com/#heading",
    ]
    assert all(candidate.reason == "same-document-hash" for candidate in candidates)
    assert all(candidate.source_ui_fingerprint for candidate in candidates)


def test_model_state_omits_controls_already_acted_on() -> None:
    state = _state(
        _control("f0e0", text="Already used"),
        _control("f0e1", text="Still available"),
    )

    filtered = browser_discovery.state_without_acted_controls(
        state,
        {(state.url, "identity-f0e0")},
    )

    assert [control.ref for control in filtered.controls] == ["f0e1"]
    assert filtered.fingerprint == state.fingerprint


def test_model_state_omits_ordinary_links_even_when_page_has_controls() -> None:
    state = _state(
        _control(
            "f0e0",
            text="Actions",
            tag="a",
            type_="",
            href="https://example.com/actions",
        ),
        _control("f0e1", text="Validate", type_="submit"),
    )

    filtered = browser_discovery.state_without_acted_controls(state, set())

    assert [control.ref for control in filtered.controls] == ["f0e1"]


def test_model_state_omits_select_without_an_alternative_value() -> None:
    fixed_select = replace(
        _control("f0e0", text="Team", tag="select", type_=""),
        selected_value="Platform",
        options=("Platform",),
    )
    changeable_select = replace(
        _control("f0e1", text="Status", tag="select", type_=""),
        selected_value="Active",
        options=("Active", "Paused"),
    )
    submit = _control("f0e2", text="Validate", type_="submit")

    filtered = browser_discovery.state_without_acted_controls(
        _state(fixed_select, changeable_select, submit),
        set(),
    )

    assert [control.ref for control in filtered.controls] == ["f0e1", "f0e2"]


def test_state_fingerprint_tracks_select_and_disabled_state() -> None:
    control = _control("f0e0", text="Mode", tag="select", type_="", disabled=True)
    initial = browser_discovery._state_fingerprint(
        "https://example.com/workflow",
        "Workflow",
        "Choose a mode",
        [control],
    )
    changed = browser_discovery._state_fingerprint(
        "https://example.com/workflow",
        "Workflow",
        "Choose a mode",
        [replace(control, disabled=False, selected_value="bounded")],
    )

    assert initial != changed


def test_priority_controls_prefer_current_workflow_step_over_tabs() -> None:
    state = _state(
        _control("f0e0", text="Account operations", role="tab"),
        _control("f0e1", text="Validate onboarding", type_="submit"),
        _control("f0e2", text="Logout", type_="submit"),
    )

    assert browser_discovery.priority_control_refs(state) == ("f0e1",)


@pytest.mark.asyncio
async def test_scripted_adapter_uses_structured_control_labels_once() -> None:
    adapter = browser_discovery.ScriptedDiscoveryAdapter(
        [
            browser_discovery.ScriptedAction(
                "Validate onboarding",
                url_pattern=r"/workflow$",
            )
        ]
    )
    state = _state(_control("f0e0", text="Validate onboarding", type_="submit"))

    action = await adapter.next_action(state, [])

    assert action is not None
    assert action.target == "f0e0"
    assert action.source == "scripted-model"
    assert await adapter.next_action(state, []) is None


@pytest.mark.asyncio
async def test_live_adapter_uses_schema_and_current_element_refs() -> None:
    class FakeResponse:
        async def text(self) -> str:
            return '{"kind":"click","target":"f0e0","value":null}'

    class FakeModel:
        def __init__(self) -> None:
            self.calls = []

        def prompt(self, prompt: str, **kwargs):
            self.calls.append((prompt, kwargs))
            return FakeResponse()

    model = FakeModel()
    adapter = discovery_model.LiveLLMDiscoveryAdapter(model, key="model-key", max_turns=1)
    adapter.set_discovery_context(
        known_endpoints=["GET https://example.com/known"],
        remaining_budgets={"actions": 4, "pages": 2, "states": 3, "seconds": 10.0},
    )
    state = replace(
        _state(_control("f0e0", text="Reveal workflow")),
        frames=(
            browser_discovery.DiscoveryFrameState(
                ref="f0",
                url="https://example.com/workflow",
                title="Workflow frame",
                visible_text="Frame controls",
                main=True,
            ),
            browser_discovery.DiscoveryFrameState(
                ref="f1",
                url="https://example.com/embedded",
                title="Embedded wizard",
                visible_text="Wizard step one",
                main=False,
            ),
        ),
    )

    action = await adapter.next_action(state, [])

    assert action == browser_discovery.DiscoveryAction(
        kind=browser_discovery.DiscoveryActionKind.click,
        target="f0e0",
        source="live-model",
    )
    prompt, kwargs = model.calls[0]
    assert "Reveal workflow" in prompt
    assert "GET https://example.com/known" in prompt
    assert '"url": "https://example.com/embedded"' in prompt
    assert '"title": "Embedded wizard"' in prompt
    assert '"visible_text": "Wizard step one"' in prompt
    assert '"actions": 4' in prompt
    assert kwargs["schema"] == discovery_model._MODEL_DECISION_SCHEMA
    assert "title" not in json.dumps(kwargs["schema"])
    assert kwargs["key"] == "model-key"
    assert "untrusted target data" in kwargs["system"]
    assert "invitations" not in kwargs["system"]
    assert "create/update/delete" not in kwargs["system"]
    assert adapter.model_budget_exhausted is True
    assert await adapter.next_action(state, []) is None


@pytest.mark.asyncio
async def test_live_adapter_rejects_malformed_model_output() -> None:
    class FakeResponse:
        async def text(self) -> str:
            return "not-json"

    class FakeModel:
        def prompt(self, *args, **kwargs):
            return FakeResponse()

    adapter = discovery_model.LiveLLMDiscoveryAdapter(FakeModel())

    with pytest.raises(ValueError):
        await adapter.next_action(_state(_control("f0e0", text="Open")), [])


@pytest.mark.asyncio
async def test_live_adapter_infers_only_unambiguous_select_value() -> None:
    class FakeResponse:
        async def text(self) -> str:
            return '{"kind":"select","target":"f0e0","value":null}'

    class FakeModel:
        def prompt(self, *_args, **_kwargs):
            return FakeResponse()

    control = replace(
        _control("f0e0", text="Choose", tag="select", type_=""),
        options=("", "bounded"),
    )
    adapter = discovery_model.LiveLLMDiscoveryAdapter(FakeModel())

    action = await adapter.next_action(_state(control), [])

    assert action is not None
    assert action.kind == browser_discovery.DiscoveryActionKind.select
    assert action.value == "bounded"


@pytest.mark.asyncio
async def test_live_adapter_rejects_ambiguous_select_without_value() -> None:
    class FakeResponse:
        async def text(self) -> str:
            return '{"kind":"select","target":"f0e0","value":null}'

    class FakeModel:
        def prompt(self, *_args, **_kwargs):
            return FakeResponse()

    control = replace(
        _control("f0e0", text="Choose", tag="select", type_=""),
        options=("first", "second"),
    )
    adapter = discovery_model.LiveLLMDiscoveryAdapter(FakeModel())

    with pytest.raises(ValueError, match="valid option"):
        await adapter.next_action(_state(control), [])


@pytest.mark.parametrize(
    ("decision", "control", "message"),
    [
        (
            discovery_model._ModelDecision(kind="click", target="f0e0", value=None),
            _control("f0e0", text="Owner", tag="select", type_=""),
            "non-click control",
        ),
        (
            discovery_model._ModelDecision(kind="select", target="f0e0", value="active"),
            replace(
                _control("f0e0", text="Status", tag="select", type_=""),
                selected_value="active",
                options=("active", "review"),
            ),
            "would not change",
        ),
        (
            discovery_model._ModelDecision(kind="fill", target="f0e0", value="text"),
            _control("f0e0", text="Submit", type_="submit"),
            "non-text control",
        ),
    ],
)
def test_live_adapter_rejects_semantically_invalid_actions(
    decision: discovery_model._ModelDecision,
    control: browser_discovery.DiscoveryControl,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        discovery_model.LiveLLMDiscoveryAdapter._action_from_decision(
            _state(control),
            decision,
        )


def test_live_adapter_requires_current_workflow_priority_control() -> None:
    state = _state(
        _control("f0e0", text="Settings", role="tab"),
        _control("f0e1", text="Validate onboarding", type_="submit"),
    )

    with pytest.raises(ValueError, match="priority control"):
        discovery_model.LiveLLMDiscoveryAdapter._action_from_decision(
            state,
            discovery_model._ModelDecision(kind="click", target="f0e0", value=None),
        )


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"click","target":"f0e0","value":null}',
        ('{"type":"object","properties":{"kind":"click","target":"f0e0","value":null}}'),
        '{"kind":"click","target":"f0e0","value":null,"title":"Kind"}',
    ],
)
def test_parse_model_decision_normalizes_observed_schema_wrappers(payload: str) -> None:
    assert discovery_model._parse_model_decision(payload) == discovery_model._ModelDecision(
        kind="click",
        target="f0e0",
        value=None,
    )


@pytest.mark.asyncio
async def test_live_adapter_retries_schema_echo_within_turn_budget() -> None:
    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        async def text(self) -> str:
            return self._text

    class FakeModel:
        def __init__(self) -> None:
            self.responses = [
                (
                    '{"type":"object","properties":'
                    '{"kind":{"type":"string"},"target":{"type":"string"},'
                    '"value":{"type":["string","null"]}}}'
                ),
                '{"kind":"click","target":"f0e0","value":null}',
            ]

        def prompt(self, *_args, **_kwargs):
            return FakeResponse(self.responses.pop(0))

    adapter = discovery_model.LiveLLMDiscoveryAdapter(FakeModel(), max_turns=3)

    action = await adapter.next_action(_state(_control("f0e0", text="Load")), [])

    assert action is not None
    assert action.target == "f0e0"
    assert adapter.turns == 2


@pytest.mark.asyncio
async def test_discovery_round_records_stale_model_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(
        _control("f0e0", text="Open first"),
        _control("f0e1", text="Open second"),
    )

    class FakePage:
        url = state.url
        frames = []

        def __init__(self) -> None:
            self.closed = False

        async def goto(self, *args, **kwargs) -> None:
            return None

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

    class StaleAdapter:
        async def next_action(self, state, history):
            return browser_discovery.DiscoveryAction(
                kind=browser_discovery.DiscoveryActionKind.click,
                target="f0e99",
            )

    async def capture(page):
        return state

    async def settle(page):
        return None

    monkeypatch.setattr(browser_discovery, "capture_page_state", capture)
    monkeypatch.setattr(browser_discovery, "_settle", settle)
    result = await browser_discovery.run_discovery_round(
        context=FakeContext(),
        target_url="https://example.com",
        candidates=[
            browser_discovery.DiscoveryCandidate("https://example.com/workflow", 100, "fixture")
        ],
        observer=None,
        adapter=cast(browser_discovery.DiscoveryAdapter, StaleAdapter()),
        cancel_event=asyncio.Event(),
        max_actions=5,
        max_pages=1,
        max_states=5,
        processed_states=set(),
    )

    assert result.action_count == 0
    assert result.diagnostics == [
        {
            "kind": "stale-action-reference",
            "candidate_url": "https://example.com/workflow",
            "target": "f0e99",
        }
    ]


@pytest.mark.asyncio
async def test_discovery_round_counts_only_runtime_verified_workflows_and_new_page_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _state(_control("f0e0", text="Reveal workflow"))
    revealed = browser_discovery.DiscoveryPageState(
        url="https://example.com/revealed",
        title="Revealed",
        visible_text="Revealed functionality",
        controls=(),
        fingerprint="state-2",
    )

    class FakePage:
        frames = []

        def __init__(self, context) -> None:
            self.context = context
            self.closed = False

        async def goto(self, *args, **kwargs) -> None:
            return None

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages = []

        async def new_page(self) -> FakePage:
            page = FakePage(self)
            self.pages.append(page)
            return page

    captured = [initial, revealed]

    async def capture(page):
        return captured.pop(0)

    async def execute(page, action):
        return page

    async def settle(page):
        return None

    monkeypatch.setattr(browser_discovery, "capture_page_state", capture)
    monkeypatch.setattr(browser_discovery, "execute_action", execute)
    monkeypatch.setattr(browser_discovery, "_settle", settle)

    result = await browser_discovery.run_discovery_round(
        context=FakeContext(),
        target_url="https://example.com",
        candidates=[browser_discovery.DiscoveryCandidate(initial.url, 100, "fixture")],
        observer=None,
        adapter=browser_discovery.NullDiscoveryAdapter(),
        cancel_event=asyncio.Event(),
        max_actions=5,
        max_pages=1,
        max_states=5,
        processed_states=set(),
        known_get_urls={initial.url},
    )

    assert result.workflow_count == 1
    assert result.stable_get_seeds == [revealed.url]
    assert result.actions[0]["verified_effect"] is True
    assert result.actions[0]["state_changed"] is True


@pytest.mark.asyncio
async def test_discovery_round_queues_distinct_hash_routes_and_rejects_scroll_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def route_state(url: str, panel: str) -> browser_discovery.DiscoveryPageState:
        controls = (
            _control(
                "f0e0",
                text="Workbench",
                tag="a",
                type_="",
                href="https://example.com/#/workbench",
            ),
            _control(
                "f0e1",
                text="Templates",
                tag="a",
                type_="",
                href="https://example.com/#/templates",
            ),
            _control(
                "f0e2",
                text="Datasets",
                tag="a",
                type_="",
                href="https://example.com/#/datasets",
            ),
            _control(
                "f0e3",
                text="Heading",
                tag="a",
                type_="",
                href="https://example.com/#heading",
            ),
        )
        return browser_discovery.DiscoveryPageState(
            url=url,
            title="SPA",
            visible_text=f"{panel} panel",
            controls=controls,
            fingerprint=f"state-{panel}",
        )

    class FakePage:
        frames = []

        def __init__(self, context) -> None:
            self.context = context
            self.url = "about:blank"
            self.closed = False

        async def goto(self, url: str, **_kwargs) -> None:
            self.url = url

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages = []

        async def new_page(self) -> FakePage:
            page = FakePage(self)
            self.pages.append(page)
            return page

    async def capture(page: FakePage) -> browser_discovery.DiscoveryPageState:
        if page.url.endswith("#/templates"):
            return route_state(page.url, "Templates")
        if page.url.endswith("#/datasets"):
            return route_state(page.url, "Datasets")
        return route_state(page.url, "Workbench")

    async def settle(page) -> None:
        del page

    monkeypatch.setattr(browser_discovery, "capture_page_state", capture)
    monkeypatch.setattr(browser_discovery, "_settle", settle)

    result = await browser_discovery.run_discovery_round(
        context=FakeContext(),
        target_url="https://example.com/",
        candidates=[
            browser_discovery.DiscoveryCandidate(
                "https://example.com/",
                100,
                "target",
            )
        ],
        observer=None,
        adapter=browser_discovery.NullDiscoveryAdapter(),
        cancel_event=asyncio.Event(),
        max_actions=10,
        max_pages=10,
        max_states=10,
        processed_states=set(),
        known_get_urls={"https://example.com/"},
    )

    assert result.candidate_count == 5
    assert result.processed_pages == 5
    assert result.state_count == 3
    assert result.stable_get_seeds == [
        "https://example.com/#/templates",
        "https://example.com/#/datasets",
    ]
    assert {
        item["url"]
        for item in result.diagnostics
        if item["kind"] == "same-document-fragment-no-ui-change"
    } == {
        "https://example.com/#/workbench",
        "https://example.com/#heading",
    }


@pytest.mark.asyncio
async def test_candidate_navigation_and_subresources_do_not_become_enrichment_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = browser_discovery.PassiveCDPObserver("ws://unused")
    state = browser_discovery.DiscoveryPageState(
        url="https://example.com/workflow",
        title="Workflow",
        visible_text="No controls",
        controls=(),
        fingerprint="state-no-actions",
    )

    class FakePage:
        frames = []

        def __init__(self, context) -> None:
            self.context = context
            self.closed = False

        async def goto(self, *args, **kwargs) -> None:
            for request_id, url, resource_type in (
                ("document", state.url, "Document"),
                ("script", "https://example.com/app.js", "Script"),
            ):
                observer.requests.append(
                    NetworkRequest(
                        method="GET",
                        url=url,
                        session_id="session",
                        target_type="page",
                        target_url=state.url,
                        request_id=request_id,
                        frame_id=None,
                        loader_id=None,
                        resource_type=resource_type,
                        initiator_type="other",
                        initiator_url=None,
                        epoch="discovery",
                        observed_at="2026-01-01T00:00:00Z",
                    )
                )
                observer.responses.append(
                    NetworkResponse(
                        url=url,
                        status=200,
                        session_id="session",
                        target_type="page",
                        request_id=request_id,
                        resource_type=resource_type,
                        mime_type="text/html",
                        epoch="discovery",
                        observed_at="2026-01-01T00:00:00Z",
                    )
                )

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages = []

        async def new_page(self) -> FakePage:
            page = FakePage(self)
            self.pages.append(page)
            return page

    async def capture(page):
        return state

    async def settle(page):
        return None

    monkeypatch.setattr(browser_discovery, "capture_page_state", capture)
    monkeypatch.setattr(browser_discovery, "_settle", settle)

    result = await browser_discovery.run_discovery_round(
        context=FakeContext(),
        target_url="https://example.com",
        candidates=[browser_discovery.DiscoveryCandidate(state.url, 100, "fixture")],
        observer=observer,
        adapter=browser_discovery.NullDiscoveryAdapter(),
        cancel_event=asyncio.Event(),
        max_actions=5,
        max_pages=1,
        max_states=5,
        processed_states=set(),
    )

    assert len(result.request_evidence) == 2
    assert result.stable_get_seeds == []
    assert result.workflow_count == 0


def test_network_evidence_joins_by_session_request_and_url() -> None:
    observer = browser_discovery.PassiveCDPObserver("ws://unused")
    observer.requests.extend(
        [
            NetworkRequest(
                method="GET",
                url="https://example.com/api/stable",
                session_id="session-1",
                target_type="page",
                target_url="https://example.com",
                request_id="request-1",
                frame_id=None,
                loader_id=None,
                resource_type="Fetch",
                initiator_type="script",
                initiator_url="https://example.com/app.js",
                epoch="discovery",
                observed_at="2026-01-01T00:00:00Z",
            ),
            NetworkRequest(
                method="GET",
                url="https://outside.test/api",
                session_id="session-1",
                target_type="page",
                target_url="https://example.com",
                request_id="request-2",
                frame_id=None,
                loader_id=None,
                resource_type="Fetch",
                initiator_type="script",
                initiator_url="https://example.com/app.js",
                epoch="discovery",
                observed_at="2026-01-01T00:00:01Z",
            ),
        ]
    )
    observer.responses.extend(
        [
            NetworkResponse(
                url="https://example.com/api/stable",
                status=200,
                session_id="session-1",
                target_type="page",
                request_id="request-1",
                resource_type="Fetch",
                mime_type="application/json",
                epoch="discovery",
                observed_at="2026-01-01T00:00:00Z",
            ),
            NetworkResponse(
                url="https://outside.test/api",
                status=200,
                session_id="session-1",
                target_type="page",
                request_id="request-2",
                resource_type="Fetch",
                mime_type="application/json",
                epoch="discovery",
                observed_at="2026-01-01T00:00:01Z",
            ),
        ]
    )

    requests, responses = browser_discovery._network_evidence(
        observer,
        0,
        0,
    )

    assert len(requests) == 2
    assert len(responses) == 2


def test_action_network_effect_returns_only_new_in_scope_documents() -> None:
    observer = browser_discovery.PassiveCDPObserver("ws://unused")
    for request_id, url, resource_type in (
        ("candidate", "https://example.com/workflow", "Document"),
        ("asset", "https://example.com/app.js", "Script"),
        ("known", "https://example.com/known", "Document"),
        ("new", "https://example.com/revealed", "Document"),
        ("xhr", "https://example.com/api/revealed", "Fetch"),
        ("outside", "https://outside.test/page", "Document"),
    ):
        observer.requests.append(
            NetworkRequest(
                method="GET",
                url=url,
                session_id="session-1",
                target_type="page",
                target_url="https://example.com/workflow",
                request_id=request_id,
                frame_id=None,
                loader_id=None,
                resource_type=resource_type,
                initiator_type="script",
                initiator_url="https://example.com/workflow",
                epoch="discovery",
                observed_at="2026-01-01T00:00:00Z",
            )
        )
        observer.responses.append(
            NetworkResponse(
                url=url,
                status=200,
                session_id="session-1",
                target_type="page",
                request_id=request_id,
                resource_type=resource_type,
                mime_type="text/html",
                epoch="discovery",
                observed_at="2026-01-01T00:00:00Z",
            )
        )

    known = {
        "https://example.com/workflow",
        "https://example.com/known",
    }
    seeds, functional_request_count = browser_discovery._action_network_effect(
        observer,
        1,
        1,
        "https://example.com",
        known,
    )

    assert seeds == ["https://example.com/revealed"]
    assert functional_request_count == 3
