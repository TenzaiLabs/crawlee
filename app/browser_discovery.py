from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .cdp_observer import PassiveCDPObserver
from .common import is_host_in_scope

_CONTROL_SELECTOR = "a[href],button,input,select,textarea,[role=button],[role=tab],[role=link]"
_REF_ATTRIBUTE = "data-tenzai-discovery-ref"


class DiscoveryActionKind(StrEnum):
    click = "click"
    fill = "fill"
    select = "select"
    press = "press"


@dataclass(frozen=True)
class DiscoveryControl:
    ref: str
    identity: str
    tag: str
    type: str
    role: str
    name: str
    label: str
    text: str
    href: str
    form_method: str
    form_action: str
    disabled: bool
    selected_value: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryFrameState:
    ref: str
    url: str
    title: str
    visible_text: str
    main: bool


@dataclass(frozen=True)
class DiscoveryPageState:
    url: str
    title: str
    visible_text: str
    controls: tuple[DiscoveryControl, ...]
    fingerprint: str
    frames: tuple[DiscoveryFrameState, ...] = ()


@dataclass(frozen=True)
class DiscoveryAction:
    kind: DiscoveryActionKind
    target: str
    value: str | None = None
    source: str = "model"


@dataclass(frozen=True)
class DiscoveryCandidate:
    url: str
    score: int
    reason: str


@dataclass
class DiscoveryRoundResult:
    states: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    stable_get_seeds: list[str] = field(default_factory=list)
    request_evidence: list[dict[str, Any]] = field(default_factory=list)
    response_evidence: list[dict[str, Any]] = field(default_factory=list)
    processed_pages: int = 0
    action_count: int = 0
    state_count: int = 0
    workflow_count: int = 0
    budget_exhausted: bool = False
    model_budget_exhausted: bool = False
    model_failure_count: int = 0


class DiscoveryAdapter(Protocol):
    @property
    def model_budget_exhausted(self) -> bool: ...

    async def next_action(
        self,
        state: DiscoveryPageState,
        history: Sequence[dict[str, Any]],
    ) -> DiscoveryAction | None: ...


class NullDiscoveryAdapter:
    @property
    def model_budget_exhausted(self) -> bool:
        return False

    async def next_action(
        self,
        state: DiscoveryPageState,
        history: Sequence[dict[str, Any]],
    ) -> DiscoveryAction | None:
        del state, history
        return None


_PRIORITY_CONTROL_MARKERS = (
    "build",
    "inspect",
    "load",
    "open",
    "preview",
    "search",
    "show",
    "validate",
)


def _is_actionable_control(control: DiscoveryControl) -> bool:
    if control.disabled or not (
        control.tag in {"button", "input", "select", "textarea"}
        or control.role in {"button", "tab"}
    ):
        return False
    if control.tag == "select":
        return any(option and option != control.selected_value for option in control.options)
    return True


def priority_control_refs(state: DiscoveryPageState) -> tuple[str, ...]:
    refs: list[str] = []
    for control in state.controls:
        if not _is_actionable_control(control) or control.role == "tab":
            continue
        text = " ".join((control.label, control.text, control.name)).casefold()
        if any(marker in text for marker in _PRIORITY_CONTROL_MARKERS):
            refs.append(control.ref)
    return tuple(refs)


@dataclass(frozen=True)
class ScriptedAction:
    label: str
    kind: DiscoveryActionKind = DiscoveryActionKind.click
    value: str | None = None
    url_pattern: str | None = None


class ScriptedDiscoveryAdapter:
    """Deterministic adapter for production-interface tests, never selected at runtime."""

    def __init__(self, actions: Sequence[ScriptedAction]) -> None:
        self._remaining = list(actions)

    @property
    def model_budget_exhausted(self) -> bool:
        return False

    async def next_action(
        self,
        state: DiscoveryPageState,
        history: Sequence[dict[str, Any]],
    ) -> DiscoveryAction | None:
        del history
        for index, action in enumerate(self._remaining):
            if action.url_pattern and re.search(action.url_pattern, state.url) is None:
                continue
            expected = action.label.casefold()
            for control in state.controls:
                labels = (control.label, control.text, control.name)
                if not any(expected == value.strip().casefold() for value in labels if value):
                    continue
                self._remaining.pop(index)
                return DiscoveryAction(
                    kind=action.kind,
                    target=control.ref,
                    value=action.value,
                    source="scripted-model",
                )
        return None


def _candidate_score(record: dict[str, Any]) -> tuple[int, str]:
    features = record.get("features")
    if isinstance(features, dict):
        if "knowledgebase" in features or "knowledge_base" in features:
            return 50, "katana-classification"
        if "forms" in features or "form" in features:
            return 40, "katana-form"
    return 20, "katana-html"


def _is_katana_client_route(record: dict[str, Any]) -> bool:
    if record.get("status") is not None or str(record.get("tag") or "").lower() != "js":
        return False
    parsed = urlparse(str(record.get("url") or ""))
    path = parsed.path.rstrip("/") or "/"
    segments = {segment.casefold() for segment in path.split("/") if segment}
    if segments & {"api", "graphql"}:
        return False
    final_segment = path.rsplit("/", 1)[-1]
    return "." not in final_segment or final_segment.casefold().endswith((".html", ".htm"))


def select_candidates(
    *,
    target_url: str,
    landing_url: str | None,
    katana_records: Sequence[dict[str, Any]],
) -> list[DiscoveryCandidate]:
    """Rank candidate observations without changing Katana's URL population."""

    candidates = [DiscoveryCandidate(target_url, 100, "target")]
    if landing_url:
        candidates.append(DiscoveryCandidate(landing_url, 90, "authenticated-landing"))
    for record in katana_records:
        if str(record.get("method") or "").upper() != "GET":
            continue
        status = record.get("status")
        url = record.get("url")
        if not isinstance(url, str):
            continue
        if _is_katana_client_route(record):
            candidates.append(DiscoveryCandidate(url, 30, "katana-client-route"))
            continue
        if not isinstance(status, int) or status < 200 or status >= 400:
            continue
        content_type = str(record.get("content_type") or "").lower()
        if "html" not in content_type:
            continue
        score, reason = _candidate_score(record)
        candidates.append(DiscoveryCandidate(url, score, reason))
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def _normalized_text(value: str, *, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _state_fingerprint(
    url: str,
    title: str,
    visible_text: str,
    controls: Sequence[DiscoveryControl],
    frames: Sequence[DiscoveryFrameState] = (),
) -> str:
    payload = {
        "url": url,
        "title": title,
        "text": _normalized_text(visible_text),
        "frames": [
            {
                "url": frame.url,
                "title": frame.title,
                "text": _normalized_text(frame.visible_text),
            }
            for frame in frames
        ],
        "controls": [
            {
                "identity": control.identity,
                "disabled": control.disabled,
                "selected_value": control.selected_value,
            }
            for control in controls
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def capture_page_state(page: Any) -> DiscoveryPageState:
    controls: list[DiscoveryControl] = []
    frames: list[DiscoveryFrameState] = []
    visible_text_parts: list[str] = []
    for frame_index, frame in enumerate(page.frames):
        try:
            payload = await frame.evaluate(
                r"""
                ({selector, refAttribute, frameIndex}) => {
                  const visible = (element) => {
                    const style = getComputedStyle(element);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' ||
                        style.opacity === '0' || element.getAttribute('aria-hidden') === 'true') {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const clean = (value) => (value || '').toString().replace(/\s+/g, ' ').trim();
                  const roots = [document];
                  const elements = [];
                  for (let rootIndex = 0; rootIndex < roots.length; rootIndex += 1) {
                    const root = roots[rootIndex];
                    for (const element of root.querySelectorAll('*')) {
                      if (element.shadowRoot) roots.push(element.shadowRoot);
                    }
                    for (const element of root.querySelectorAll(selector)) elements.push(element);
                  }
                  const identities = new Map();
                  const controls = elements.filter(visible).slice(0, 160).map((element, index) => {
                    const ref = `f${frameIndex}e${index}`;
                    element.setAttribute(refAttribute, ref);
                    const form = element.form || element.closest('form');
                    const labels = element.labels
                      ? Array.from(element.labels).map(label => clean(label.innerText))
                      : [];
                    const closestLabel = element.closest('label');
                    if (closestLabel) labels.push(clean(closestLabel.innerText));
                    if (element.getAttribute('aria-label')) {
                      labels.push(element.getAttribute('aria-label'));
                    }
                    const descriptor = [
                      location.href,
                      element.tagName.toLowerCase(),
                      element.getAttribute('type') || '',
                      element.getAttribute('role') || '',
                      element.getAttribute('name') || '',
                      element.id || '',
                      clean(element.innerText || element.value || ''),
                      element.getAttribute('href') || '',
                      form ? (form.getAttribute('method') || 'get') : '',
                      form ? (form.getAttribute('action') || location.href) : '',
                    ].join('|');
                    const ordinal = identities.get(descriptor) || 0;
                    identities.set(descriptor, ordinal + 1);
                    return {
                      ref,
                      identity: `${descriptor}|${ordinal}`,
                      tag: element.tagName.toLowerCase(),
                      type: element.getAttribute('type') || '',
                      role: element.getAttribute('role') || '',
                      name: element.getAttribute('name') || '',
                      label: labels.filter(Boolean).join(' | ').slice(0, 200),
                      text: clean(element.innerText || element.value || '').slice(0, 200),
                      href: element.href || '',
                      formMethod: form ? (form.method || 'get').toUpperCase() : '',
                      formAction: form ? form.action : '',
                      disabled: Boolean(
                        element.disabled || element.getAttribute('aria-disabled') === 'true'
                      ),
                      selectedValue: element.tagName.toLowerCase() === 'select'
                        ? element.value
                        : '',
                      options: element.tagName.toLowerCase() === 'select'
                        ? Array.from(element.options).slice(0, 30).map(option => option.value)
                        : [],
                    };
                  });
                  return {
                    controls,
                    title: document.title || '',
                    text: clean(document.body ? document.body.innerText : '').slice(0, 4000),
                  };
                }
                """,
                {
                    "selector": _CONTROL_SELECTOR,
                    "refAttribute": _REF_ATTRIBUTE,
                    "frameIndex": frame_index,
                },
            )
        except PlaywrightError, PlaywrightTimeoutError:
            continue
        if not isinstance(payload, dict):
            continue
        text = payload.get("text")
        frame_text = _normalized_text(text) if isinstance(text, str) else ""
        if isinstance(text, str) and text:
            visible_text_parts.append(text)
        frames.append(
            DiscoveryFrameState(
                ref=f"f{frame_index}",
                url=str(frame.url or ""),
                title=str(payload.get("title") or ""),
                visible_text=frame_text,
                main=frame_index == 0,
            )
        )
        frame_controls = payload.get("controls")
        if not isinstance(frame_controls, list):
            continue
        for item in frame_controls:
            if not isinstance(item, dict):
                continue
            raw_identity = str(item.get("identity") or "")
            controls.append(
                DiscoveryControl(
                    ref=str(item.get("ref") or ""),
                    identity=hashlib.sha256(raw_identity.encode()).hexdigest(),
                    tag=str(item.get("tag") or ""),
                    type=str(item.get("type") or "").lower(),
                    role=str(item.get("role") or "").lower(),
                    name=str(item.get("name") or ""),
                    label=str(item.get("label") or ""),
                    text=str(item.get("text") or ""),
                    href=str(item.get("href") or ""),
                    form_method=str(item.get("formMethod") or "").upper(),
                    form_action=str(item.get("formAction") or ""),
                    disabled=bool(item.get("disabled")),
                    selected_value=str(item.get("selectedValue") or ""),
                    options=tuple(str(value) for value in item.get("options") or []),
                )
            )
    title = ""
    with contextlib.suppress(PlaywrightError, PlaywrightTimeoutError):
        title = await page.title()
    visible_text = _normalized_text(" ".join(visible_text_parts))
    url = str(page.url or "")
    return DiscoveryPageState(
        url=url,
        title=title,
        visible_text=visible_text,
        controls=tuple(controls),
        fingerprint=_state_fingerprint(url, title, visible_text, controls, frames),
        frames=tuple(frames),
    )


def deterministic_action(
    state: DiscoveryPageState,
    acted_controls: set[tuple[str, str]],
) -> DiscoveryAction | None:
    interactive = [
        control
        for control in state.controls
        if _is_actionable_control(control) and (state.url, control.identity) not in acted_controls
    ]
    eligible = [
        control
        for control in interactive
        if (
            (control.tag == "button" and control.type not in {"submit", "reset"})
            or control.role == "tab"
        )
    ]
    if len(interactive) != 1 or len(eligible) != 1:
        return None
    return DiscoveryAction(
        kind=DiscoveryActionKind.click,
        target=eligible[0].ref,
        source="deterministic",
    )


def has_model_eligible_control(
    state: DiscoveryPageState,
    acted_controls: set[tuple[str, str]],
) -> bool:
    """Return whether this state has UI behavior beyond ordinary links.

    Normal anchors are already Katana's crawl surface. Sending link-only pages
    to the model can only reproduce that work (and can turn a crawl-trap link
    into a new enrichment seed), so browser guidance is reserved for controls
    that require interaction semantics.
    """

    return any(
        _is_actionable_control(control) and (state.url, control.identity) not in acted_controls
        for control in state.controls
    )


def state_without_acted_controls(
    state: DiscoveryPageState,
    acted_controls: set[tuple[str, str]],
) -> DiscoveryPageState:
    return replace(
        state,
        controls=tuple(
            control
            for control in state.controls
            if (state.url, control.identity) not in acted_controls
            and _is_actionable_control(control)
        ),
    )


async def _settle(page: Any) -> None:
    with contextlib.suppress(PlaywrightError, PlaywrightTimeoutError):
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
    with contextlib.suppress(PlaywrightError, PlaywrightTimeoutError):
        await page.wait_for_timeout(300)


def _frame_index(ref: str) -> int:
    match = re.fullmatch(r"f(\d+)e\d+", ref)
    if match is None:
        raise ValueError("Discovery action target is not a current element reference")
    return int(match.group(1))


async def execute_action(page: Any, action: DiscoveryAction) -> Any:
    frame_index = _frame_index(action.target)
    if frame_index >= len(page.frames):
        raise ValueError("Discovery action references a stale frame")
    frame = page.frames[frame_index]
    locator = frame.locator(f'[{_REF_ATTRIBUTE}="{action.target}"]').first
    if action.kind == DiscoveryActionKind.click:
        await locator.click(timeout=10_000)
    elif action.kind == DiscoveryActionKind.fill:
        await locator.fill(str(action.value or ""), timeout=10_000)
    elif action.kind == DiscoveryActionKind.select:
        await locator.select_option(value=str(action.value or ""), timeout=10_000)
    elif action.kind == DiscoveryActionKind.press:
        await locator.press(str(action.value or "Enter"), timeout=10_000)
    await _settle(page)
    open_pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
    if open_pages and open_pages[-1] is not page:
        page = open_pages[-1]
        await _settle(page)
    return page


def _network_evidence(
    observer: PassiveCDPObserver | None,
    request_start: int,
    response_start: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if observer is None:
        return [], []
    requests = [asdict(item) for item in observer.requests[request_start:]]
    responses = [asdict(item) for item in observer.responses[response_start:]]
    return requests, responses


def _action_network_effect(
    observer: PassiveCDPObserver | None,
    request_start: int,
    response_start: int,
    target_url: str,
    known_get_urls: set[str],
) -> tuple[list[str], int]:
    """Return new document seeds and successful requests from one action window."""

    if observer is None:
        return [], 0
    response_status: dict[tuple[str | None, str | None, str], int] = {}
    for response in observer.responses[response_start:]:
        response_status[(response.session_id, response.request_id, response.url)] = response.status
    seeds: list[str] = []
    functional_request_count = 0
    for request in observer.requests[request_start:]:
        status = response_status.get((request.session_id, request.request_id, request.url))
        parsed = urlparse(request.url)
        if (
            status is None
            or status < 200
            or status >= 400
            or parsed.scheme not in {"http", "https"}
            or not is_host_in_scope(parsed.hostname, target_url)
        ):
            continue
        resource_type = str(request.resource_type or "").casefold()
        if resource_type in {"document", "fetch", "xhr"}:
            functional_request_count += 1
        if (
            request.method == "GET"
            and resource_type == "document"
            and request.url not in known_get_urls
        ):
            known_get_urls.add(request.url)
            seeds.append(request.url)
    return seeds, functional_request_count


def _new_page_seed(
    url: str,
    *,
    previous_url: str,
    target_url: str,
    known_get_urls: set[str],
) -> str | None:
    parsed = urlparse(url)
    if (
        not url
        or url == previous_url
        or url in known_get_urls
        or parsed.scheme not in {"http", "https"}
        or not is_host_in_scope(parsed.hostname, target_url)
    ):
        return None
    known_get_urls.add(url)
    return url


async def run_discovery_round(
    *,
    context: Any,
    target_url: str,
    candidates: Sequence[DiscoveryCandidate],
    observer: PassiveCDPObserver | None,
    adapter: DiscoveryAdapter,
    cancel_event: asyncio.Event,
    max_actions: int,
    max_pages: int,
    max_states: int,
    processed_states: set[tuple[str, str]],
    known_get_urls: set[str] | None = None,
) -> DiscoveryRoundResult:
    result = DiscoveryRoundResult()
    request_start = len(observer.requests) if observer is not None else 0
    response_start = len(observer.responses) if observer is not None else 0
    acted_controls: set[tuple[str, str]] = set()
    known_seed_urls = set(known_get_urls or ())

    for candidate in candidates:
        if result.processed_pages >= max_pages or result.action_count >= max_actions:
            result.budget_exhausted = True
            break
        if cancel_event.is_set():
            raise asyncio.CancelledError
        page = await context.new_page()
        opened_pages = {page}
        candidate_had_verified_effect = False
        next_state: DiscoveryPageState | None = None
        try:
            await page.goto(candidate.url, wait_until="domcontentloaded", timeout=30_000)
            await _settle(page)
            result.processed_pages += 1
            while result.action_count < max_actions and result.state_count < max_states:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                state = next_state or await capture_page_state(page)
                next_state = None
                state_key = (state.url, state.fingerprint)
                if state_key in processed_states:
                    break
                processed_states.add(state_key)
                result.state_count += 1
                result.states.append(
                    {
                        "candidate_url": candidate.url,
                        "url": state.url,
                        "title": state.title,
                        "fingerprint": state.fingerprint,
                        "control_count": len(state.controls),
                        "reason": candidate.reason,
                    }
                )
                action = deterministic_action(state, acted_controls)
                if action is None and has_model_eligible_control(state, acted_controls):
                    try:
                        action = await adapter.next_action(
                            state_without_acted_controls(state, acted_controls),
                            result.actions,
                        )
                    except Exception as exc:
                        result.diagnostics.append(
                            {
                                "kind": "model-decision-failed",
                                "candidate_url": candidate.url,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        )
                        result.model_failure_count += 1
                        action = None
                if action is None:
                    break
                control = next(
                    (item for item in state.controls if item.ref == action.target),
                    None,
                )
                if control is None:
                    result.diagnostics.append(
                        {
                            "kind": "stale-action-reference",
                            "candidate_url": candidate.url,
                            "target": action.target,
                        }
                    )
                    break
                if (state.url, control.identity) in acted_controls:
                    result.diagnostics.append(
                        {
                            "kind": "repeated-action-reference",
                            "candidate_url": candidate.url,
                            "target": action.target,
                        }
                    )
                    break
                acted_controls.add((state.url, control.identity))
                action_record = {
                    "candidate_url": candidate.url,
                    "state_fingerprint": state.fingerprint,
                    "kind": action.kind.value,
                    "target_identity": control.identity,
                    "source": action.source,
                    "outcome": "completed",
                }
                action_request_start = len(observer.requests) if observer is not None else 0
                action_response_start = len(observer.responses) if observer is not None else 0
                try:
                    page = await execute_action(page, action)
                    opened_pages.update(page.context.pages)
                    next_state = await capture_page_state(page)
                    action_seeds, functional_request_count = _action_network_effect(
                        observer,
                        action_request_start,
                        action_response_start,
                        target_url,
                        known_seed_urls,
                    )
                    result.stable_get_seeds.extend(action_seeds)
                    page_seed = _new_page_seed(
                        next_state.url,
                        previous_url=state.url,
                        target_url=target_url,
                        known_get_urls=known_seed_urls,
                    )
                    if page_seed is not None:
                        result.stable_get_seeds.append(page_seed)
                    state_changed = next_state.fingerprint != state.fingerprint
                    verified_effect = state_changed or functional_request_count > 0
                    candidate_had_verified_effect |= verified_effect
                    action_record.update(
                        {
                            "resulting_url": next_state.url,
                            "resulting_state_fingerprint": next_state.fingerprint,
                            "state_changed": state_changed,
                            "functional_request_count": functional_request_count,
                            "verified_effect": verified_effect,
                        }
                    )
                except (PlaywrightError, PlaywrightTimeoutError, ValueError) as exc:
                    action_record["outcome"] = "failed"
                    action_record["error_type"] = type(exc).__name__
                    action_record["verified_effect"] = False
                result.actions.append(action_record)
                result.action_count += 1
            if candidate_had_verified_effect:
                result.workflow_count += 1
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            result.diagnostics.append(
                {
                    "kind": "candidate-navigation-failed",
                    "url": candidate.url,
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            for opened_page in opened_pages:
                if not opened_page.is_closed():
                    with contextlib.suppress(PlaywrightError):
                        await opened_page.close()

    if result.action_count >= max_actions:
        result.budget_exhausted = True
    if result.state_count >= max_states:
        result.budget_exhausted = True
    requests, responses = _network_evidence(
        observer,
        request_start,
        response_start,
    )
    result.request_evidence = requests
    result.response_evidence = responses
    result.model_budget_exhausted = bool(getattr(adapter, "model_budget_exhausted", False))
    return result
