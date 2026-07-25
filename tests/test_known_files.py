from __future__ import annotations

import asyncio
import gzip
from urllib.parse import urlparse

import httpx
import pytest

from app import known_files


def _xml(body: str, *, status: int = 200, headers: dict[str, str] | None = None):
    return httpx.Response(
        status,
        content=body.encode(),
        headers={"content-type": "application/xml", **(headers or {})},
    )


@pytest.mark.asyncio
async def test_discovers_nested_sitemap_seeds_and_propagates_auth_headers() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="Sitemap: /indexes/root.xml\nSitemap: https://evil.test/out.xml\n",
                headers={"content-type": "text/plain"},
            )
        if path == "/sitemap.xml":
            return httpx.Response(404)
        if path == "/indexes/root.xml":
            return _xml(
                "<sitemapindex><sitemap><loc>/maps/pages.xml</loc></sitemap></sitemapindex>"
            )
        if path == "/maps/pages.xml":
            return _xml(
                "<urlset>"
                "<url><loc>https://app.example.test/from-map</loc></url>"
                "<url><loc>https://child.app.example.test/subdomain</loc></url>"
                "<url><loc>https://evil.test/out</loc></url>"
                "</urlset>"
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = await known_files.discover_known_files(
        "https://app.example.test/start",
        headers=["Authorization: Bearer runtime-token", "X-Tenant: acme"],
        transport=httpx.MockTransport(handler),
    )

    assert result.seeds == [
        "https://app.example.test/from-map",
        "https://child.app.example.test/subdomain",
    ]
    assert result.attempts == 4
    assert [document.kind for document in result.documents] == [
        "robots",
        "unknown",
        "sitemap",
        "sitemap",
    ]
    assert {diagnostic.reason for diagnostic in result.diagnostics} == {
        "http_status",
        "out_of_scope_seed",
        "out_of_scope_sitemap",
    }
    assert all(request.headers["authorization"] == "Bearer runtime-token" for request in requests)
    assert all(request.headers["x-tenant"] == "acme" for request in requests)
    assert "runtime-token" not in str(result.evidence())


@pytest.mark.asyncio
async def test_redirect_cycle_malformed_and_out_of_scope_are_diagnostics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="Sitemap: /redirect.xml\nSitemap: /malformed.xml\n",
                headers={"content-type": "text/plain"},
            )
        if path == "/sitemap.xml":
            return _xml("<sitemapindex><sitemap><loc>/cycle-a.xml</loc></sitemap></sitemapindex>")
        if path == "/redirect.xml":
            return httpx.Response(302, headers={"location": "/pages.xml"})
        if path == "/pages.xml":
            return _xml("<urlset><url><loc>/found</loc></url></urlset>")
        if path == "/malformed.xml":
            return _xml("<urlset><url><loc>broken")
        if path == "/cycle-a.xml":
            return _xml("<sitemapindex><sitemap><loc>/cycle-b.xml</loc></sitemap></sitemapindex>")
        if path == "/cycle-b.xml":
            return _xml(
                "<sitemapindex>"
                "<sitemap><loc>/cycle-a.xml</loc></sitemap>"
                "<sitemap><loc>https://outside.test/map.xml</loc></sitemap>"
                "</sitemapindex>"
            )
        raise AssertionError(f"unexpected request: {request.url}")

    result = await known_files.discover_known_files(
        "https://example.test/",
        transport=httpx.MockTransport(handler),
    )

    assert result.seeds == ["https://example.test/found"]
    assert result.attempts == 7
    assert any(document.kind == "redirect" for document in result.documents)
    assert {diagnostic.reason for diagnostic in result.diagnostics} == {
        "cycle_or_duplicate",
        "malformed_sitemap",
        "out_of_scope_sitemap",
    }


@pytest.mark.asyncio
async def test_decoded_response_size_limit_rejects_compressed_oversize_document() -> None:
    oversized = gzip.compress(b"<urlset>" + (b" " * 512) + b"</urlset>")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-encoding": "gzip", "content-type": "application/xml"},
        )

    result = await known_files.discover_known_files(
        "https://example.test/",
        response_limit_bytes=64,
        transport=httpx.MockTransport(handler),
    )

    assert result.seeds == []
    assert result.attempts == 2
    oversized_document = next(
        document for document in result.documents if document.kind == "oversized"
    )
    assert oversized_document.url == "https://example.test/sitemap.xml"
    assert oversized_document.size_bytes == 65
    assert any(diagnostic.reason == "response_too_large" for diagnostic in result.diagnostics)


@pytest.mark.asyncio
async def test_document_budget_is_global_across_origins_and_nested_indexes() -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        index = (
            0 if path == "/sitemap.xml" else int(path.removeprefix("/map-").removesuffix(".xml"))
        )
        return _xml(
            f"<sitemapindex><sitemap><loc>/map-{index + 1}.xml</loc></sitemap></sitemapindex>"
        )

    result = await known_files.discover_known_files(
        "https://example.test/",
        origins=["https://example.test", "https://child.example.test"],
        document_limit=5,
        transport=httpx.MockTransport(handler),
    )

    assert result.attempts == 5
    assert len(requested) == 5
    assert len({urlparse(url).netloc for url in requested}) == 2
    assert result.diagnostics[-1] == known_files.KnownFileDiagnostic(
        "https://example.test/", "document_budget_exhausted", "limit=5"
    )


@pytest.mark.asyncio
async def test_incremental_subdomain_discovery_reuses_the_global_document_budget() -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return _xml(
            f"<urlset><url><loc>{request.url.scheme}://{request.url.host}/found</loc></url></urlset>"
        )

    transport = httpx.MockTransport(handler)
    initial = await known_files.discover_known_files(
        "https://example.test/",
        document_limit=3,
        transport=transport,
    )
    result = await known_files.discover_known_files(
        "https://example.test/",
        origins=["https://example.test", "https://child.example.test"],
        document_limit=3,
        transport=transport,
        existing_result=initial,
    )

    assert result.attempts == 3
    assert result.origins == ["https://example.test", "https://child.example.test"]
    assert requested == [
        "https://example.test/robots.txt",
        "https://example.test/sitemap.xml",
        "https://child.example.test/robots.txt",
    ]
    assert result.seeds == ["https://example.test/found"]
    assert result.diagnostics[-1] == known_files.KnownFileDiagnostic(
        "https://example.test/", "document_budget_exhausted", "limit=3"
    )


@pytest.mark.asyncio
async def test_pre_cancelled_discovery_does_not_issue_requests() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    cancel_event = asyncio.Event()
    cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await known_files.discover_known_files(
            "https://example.test/",
            cancel_event=cancel_event,
            transport=httpx.MockTransport(handler),
        )

    assert calls == 0
