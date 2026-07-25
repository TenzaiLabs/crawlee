from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ElementTree
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from .common import is_host_in_scope

DEFAULT_DOCUMENT_LIMIT = 100
DEFAULT_RESPONSE_LIMIT_BYTES = 5 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class KnownFileDocument:
    url: str
    status: int
    content_type: str | None
    size_bytes: int
    kind: str
    observed_at: str


@dataclass(frozen=True)
class KnownFileDiagnostic:
    url: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class KnownFileResult:
    seeds: list[str]
    documents: list[KnownFileDocument]
    diagnostics: list[KnownFileDiagnostic]
    attempts: int
    origins: list[str] = field(default_factory=list)

    def evidence(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "origins": list(self.origins),
            "seeds": list(self.seeds),
            "documents": [asdict(document) for document in self.documents],
            "diagnostics": [asdict(diagnostic) for diagnostic in self.diagnostics],
        }


class _ResponseTooLarge(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _headers_from_lines(headers: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers or []:
        name, separator, value = str(header).partition(":")
        if separator and name.strip():
            result[name.strip()] = value.strip()
    return result


def _check_cancelled(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


def _normalized_http_url(candidate: str, *, base_url: str) -> str | None:
    joined = urljoin(base_url, candidate.strip())
    without_fragment, _ = urldefrag(joined)
    parsed = urlparse(without_fragment)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return without_fragment


def _in_scope(url: str, target_url: str) -> bool:
    return is_host_in_scope(urlparse(url).hostname, target_url)


def in_scope_origins(target_url: str, urls: list[str]) -> list[str]:
    origins: list[str] = []
    seen: set[str] = set()
    for url in urls:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not _in_scope(url, target_url)
        ):
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in seen:
            origins.append(origin)
            seen.add(origin)
    return origins


def _document_kind(url: str, content: bytes) -> str:
    if urlparse(url).path.lower().endswith("robots.txt"):
        return "robots"
    stripped = content.lstrip()
    if stripped.startswith(b"<"):
        return "sitemap"
    return "unknown"


def _robots_sitemaps(content: bytes, base_url: str) -> list[str]:
    candidates: list[str] = []
    for raw_line in content.decode("utf-8", errors="replace").splitlines():
        name, separator, value = raw_line.partition(":")
        if not separator or name.strip().lower() != "sitemap":
            continue
        normalized = _normalized_http_url(value, base_url=base_url)
        if normalized is not None:
            candidates.append(normalized)
    return candidates


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _sitemap_urls(content: bytes, base_url: str) -> tuple[list[str], list[str]]:
    root = ElementTree.fromstring(content)
    root_name = _local_name(root.tag)
    locations: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "loc" or not element.text:
            continue
        normalized = _normalized_http_url(element.text, base_url=base_url)
        if normalized is not None:
            locations.append(normalized)
    if root_name == "sitemapindex":
        return [], locations
    if root_name == "urlset":
        return locations, []
    raise ValueError(f"unsupported sitemap root {root_name or 'missing'}")


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise _ResponseTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def discover_known_files(
    target_url: str,
    *,
    headers: list[str] | None = None,
    origins: list[str] | None = None,
    document_limit: int = DEFAULT_DOCUMENT_LIMIT,
    response_limit_bytes: int = DEFAULT_RESPONSE_LIMIT_BYTES,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    cancel_event: asyncio.Event | None = None,
    existing_result: KnownFileResult | None = None,
) -> KnownFileResult:
    if document_limit <= 0:
        raise ValueError("document_limit must be positive")
    if response_limit_bytes <= 0:
        raise ValueError("response_limit_bytes must be positive")

    target = urlparse(target_url)
    if target.scheme not in {"http", "https"} or not target.netloc:
        raise ValueError("target_url must be an absolute HTTP(S) URL")
    initial_origins = origins or [f"{target.scheme}://{target.netloc}"]
    queue: deque[str] = deque()
    diagnostics = list(existing_result.diagnostics) if existing_result is not None else []
    discovered_origins = list(existing_result.origins) if existing_result is not None else []
    if existing_result is not None and not discovered_origins:
        discovered_origins = in_scope_origins(
            target_url,
            [document.url for document in existing_result.documents],
        )
    known_origins = set(discovered_origins)
    for origin in initial_origins:
        parsed_origin = urlparse(origin)
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.netloc
            or not _in_scope(origin, target_url)
        ):
            diagnostics.append(KnownFileDiagnostic(origin, "out_of_scope_origin"))
            continue
        normalized_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        if normalized_origin in known_origins:
            continue
        discovered_origins.append(normalized_origin)
        known_origins.add(normalized_origin)
        queue.extend(
            [
                urljoin(f"{normalized_origin}/", "robots.txt"),
                urljoin(f"{normalized_origin}/", "sitemap.xml"),
            ]
        )

    attempts = existing_result.attempts if existing_result is not None else 0
    documents = list(existing_result.documents) if existing_result is not None else []
    seen_documents = {document.url for document in documents}
    seeds = list(existing_result.seeds) if existing_result is not None else []
    timeout = httpx.Timeout(request_timeout_seconds)
    async with httpx.AsyncClient(
        headers=_headers_from_lines(headers),
        timeout=timeout,
        follow_redirects=False,
        verify=False,
        transport=transport,
    ) as client:
        while queue and attempts < document_limit:
            _check_cancelled(cancel_event)
            document_url = queue.popleft()
            if document_url in seen_documents:
                diagnostics.append(KnownFileDiagnostic(document_url, "cycle_or_duplicate"))
                continue
            seen_documents.add(document_url)
            attempts += 1
            try:
                async with client.stream("GET", document_url) as response:
                    status = response.status_code
                    content_type = response.headers.get("content-type")
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        redirect_url = (
                            _normalized_http_url(location, base_url=document_url)
                            if location
                            else None
                        )
                        if redirect_url is None:
                            diagnostics.append(
                                KnownFileDiagnostic(document_url, "invalid_redirect", location)
                            )
                        elif not _in_scope(redirect_url, target_url):
                            diagnostics.append(
                                KnownFileDiagnostic(
                                    document_url,
                                    "out_of_scope_redirect",
                                    redirect_url,
                                )
                            )
                        else:
                            queue.appendleft(redirect_url)
                        documents.append(
                            KnownFileDocument(
                                url=document_url,
                                status=status,
                                content_type=content_type,
                                size_bytes=0,
                                kind="redirect",
                                observed_at=_now(),
                            )
                        )
                        continue
                    try:
                        content = await _bounded_body(response, response_limit_bytes)
                    except _ResponseTooLarge:
                        diagnostics.append(
                            KnownFileDiagnostic(
                                document_url,
                                "response_too_large",
                                f"limit={response_limit_bytes}",
                            )
                        )
                        documents.append(
                            KnownFileDocument(
                                url=document_url,
                                status=status,
                                content_type=content_type,
                                size_bytes=response_limit_bytes + 1,
                                kind="oversized",
                                observed_at=_now(),
                            )
                        )
                        continue
                    _check_cancelled(cancel_event)
            except httpx.HTTPError as exc:
                diagnostics.append(
                    KnownFileDiagnostic(document_url, "request_error", type(exc).__name__)
                )
                continue

            kind = _document_kind(document_url, content)
            documents.append(
                KnownFileDocument(
                    url=document_url,
                    status=status,
                    content_type=content_type,
                    size_bytes=len(content),
                    kind=kind,
                    observed_at=_now(),
                )
            )
            if status < 200 or status >= 300:
                diagnostics.append(KnownFileDiagnostic(document_url, "http_status", str(status)))
                continue
            if kind == "robots":
                for sitemap_url in _robots_sitemaps(content, document_url):
                    if _in_scope(sitemap_url, target_url):
                        queue.append(sitemap_url)
                    else:
                        diagnostics.append(
                            KnownFileDiagnostic(document_url, "out_of_scope_sitemap", sitemap_url)
                        )
                continue
            if kind != "sitemap":
                diagnostics.append(KnownFileDiagnostic(document_url, "unsupported_document"))
                continue
            try:
                page_urls, sitemap_urls = _sitemap_urls(content, document_url)
            except (ElementTree.ParseError, ValueError) as exc:
                diagnostics.append(KnownFileDiagnostic(document_url, "malformed_sitemap", str(exc)))
                continue
            for page_url in page_urls:
                if _in_scope(page_url, target_url):
                    seeds.append(page_url)
                else:
                    diagnostics.append(
                        KnownFileDiagnostic(document_url, "out_of_scope_seed", page_url)
                    )
            for sitemap_url in sitemap_urls:
                if _in_scope(sitemap_url, target_url):
                    queue.append(sitemap_url)
                else:
                    diagnostics.append(
                        KnownFileDiagnostic(document_url, "out_of_scope_sitemap", sitemap_url)
                    )

    if queue:
        diagnostics.append(
            KnownFileDiagnostic(target_url, "document_budget_exhausted", f"limit={document_limit}")
        )
    return KnownFileResult(
        seeds=seeds,
        documents=documents,
        diagnostics=diagnostics,
        attempts=attempts,
        origins=discovered_origins,
    )
