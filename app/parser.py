from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from .common import is_host_in_scope, open_text_reader
from .log_records import normalize_request_record

logger = logging.getLogger(__name__)


class CrawlArtifactsMissingError(FileNotFoundError):
    pass


class CrawlArtifactsCorruptError(ValueError):
    pass


_KATANA_FEATURE_KEYS = {
    "form",
    "forms",
    "knowledge_base",
    "knowledgebase",
    "technologies",
}


def validate_sitemap(sitemap: Any) -> dict[str, Any]:
    if not isinstance(sitemap, dict):
        raise CrawlArtifactsCorruptError("Sitemap must be an object")
    entries = sitemap.get("entries")
    tree = sitemap.get("tree")
    if not isinstance(entries, list):
        raise CrawlArtifactsCorruptError("Sitemap entries must be a list")
    if any(not isinstance(entry, dict) for entry in entries):
        raise CrawlArtifactsCorruptError("Sitemap entries must contain objects")
    if not isinstance(tree, dict):
        raise CrawlArtifactsCorruptError("Sitemap tree must be an object")

    def _validate_tree_node(node: Any) -> None:
        if not isinstance(node, dict):
            raise CrawlArtifactsCorruptError("Sitemap tree node must be an object")
        children = node.get("children")
        pages = node.get("pages")
        if not isinstance(children, dict) or not isinstance(pages, list):
            raise CrawlArtifactsCorruptError("Sitemap tree has an invalid shape")
        if any(not isinstance(page, dict) for page in pages):
            raise CrawlArtifactsCorruptError("Sitemap tree pages must contain objects")
        for child in children.values():
            _validate_tree_node(child)

    _validate_tree_node(tree)
    return sitemap


def _build_tree(entries: list[dict[str, Any]]) -> dict[str, Any]:
    tree: dict[str, Any] = {"children": {}, "pages": []}
    for entry in entries:
        parsed = urlparse(entry["url"])
        path = parsed.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        node = tree
        for segment in segments:
            node = node["children"].setdefault(segment, {"children": {}, "pages": []})
        node["pages"].append(entry)
    return tree


def build_sitemap(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_sitemap({"entries": entries, "tree": _build_tree(entries)})


def _valid_scoped_http_url(url: str, target_url: str | None) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and is_host_in_scope(parsed.hostname, target_url)
    )


def _katana_features(value: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = str(key).lower()
                if normalized in _KATANA_FEATURE_KEYS and normalized not in found:
                    found[normalized] = child
                elif isinstance(child, dict | list):
                    visit(child)
        elif isinstance(current, list):
            for child in current:
                if isinstance(child, dict | list):
                    visit(child)

    visit(value)
    return found


def parse_katana_evidence(
    log_path: str,
    *,
    lane: str,
    target_url: str | None = None,
) -> list[dict[str, Any]]:
    """Read bounded Katana metadata needed for attribution and candidate selection."""

    if not os.path.exists(log_path):
        raise CrawlArtifactsMissingError(f"Katana crawl artifact not found: {log_path}")
    records: list[dict[str, Any]] = []
    with open_text_reader(log_path) as handle:
        for line in handle:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            normalized = normalize_request_record(data)
            if normalized is None or not _valid_scoped_http_url(normalized["url"], target_url):
                continue
            record = {
                "lane": lane,
                "method": str(normalized["method"]).upper(),
                "url": normalized["url"],
                "status": normalized["status"],
                "content_type": normalized["content_type"],
                "timestamp": normalized["timestamp"],
            }
            request = data.get("request")
            if isinstance(request, dict):
                for field in ("tag", "attribute", "source"):
                    value = request.get(field)
                    if isinstance(value, str) and value:
                        record[field] = value
            features = _katana_features(data)
            if features:
                record["features"] = features
            records.append(record)
    return records


def _merge_entry(
    entries: OrderedDict[tuple[str, str], dict[str, Any]],
    candidate: dict[str, Any],
    *,
    target_url: str,
) -> None:
    method = str(candidate.get("method") or "").upper()
    url = str(candidate.get("url") or "")
    if not method or not _valid_scoped_http_url(url, target_url):
        return
    normalized = {
        "method": method,
        "url": url,
        "status": candidate.get("status"),
        "content_type": candidate.get("content_type"),
        "timestamp": candidate.get("timestamp"),
    }
    key = (method, url)
    current = entries.get(key)
    if current is None:
        entries[key] = normalized
        return
    for field in ("status", "content_type", "timestamp"):
        if current[field] is None and normalized[field] is not None:
            current[field] = normalized[field]


def merge_baseline_sitemap(
    *,
    target_url: str,
    katana_sitemaps: list[dict[str, Any]],
    browser_evidence: dict[str, Any],
    known_file_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Publish one exact (method, URL) aggregate without crawl-policy deduplication."""

    merged: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    for sitemap in katana_sitemaps:
        validate_sitemap(sitemap)
        for entry in sitemap["entries"]:
            _merge_entry(merged, entry, target_url=target_url)

    documents = known_file_evidence.get("documents")
    if isinstance(documents, list):
        for document in documents:
            if not isinstance(document, dict):
                continue
            _merge_entry(
                merged,
                {
                    "method": "GET",
                    "url": document.get("url"),
                    "status": document.get("status"),
                    "content_type": document.get("content_type"),
                    "timestamp": document.get("observed_at"),
                },
                target_url=target_url,
            )

    responses_by_request: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    responses = browser_evidence.get("responses")
    if isinstance(responses, list):
        for response in responses:
            if not isinstance(response, dict) or not isinstance(response.get("url"), str):
                continue
            responses_by_request[
                (
                    response.get("session_id"),
                    response.get("request_id"),
                    response["url"],
                )
            ] = response
    requests = browser_evidence.get("requests")
    if isinstance(requests, list):
        for request in requests:
            if not isinstance(request, dict) or not isinstance(request.get("url"), str):
                continue
            response = responses_by_request.get(
                (
                    request.get("session_id"),
                    request.get("request_id"),
                    request["url"],
                ),
                {},
            )
            _merge_entry(
                merged,
                {
                    "method": request.get("method"),
                    "url": request["url"],
                    "status": response.get("status"),
                    "content_type": response.get("mime_type"),
                    "timestamp": request.get("observed_at"),
                },
                target_url=target_url,
            )
    return build_sitemap(list(merged.values()))


def parse_katana_log(
    log_path: str,
    target_url: str | None = None,
) -> dict[str, Any]:
    logger.info("Parsing Katana crawl artifact path=%s", log_path)
    if not os.path.exists(log_path):
        logger.warning("Katana crawl artifact is missing path=%s", log_path)
        raise CrawlArtifactsMissingError(f"Katana crawl artifact not found: {log_path}")

    deduped: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    saw_nonempty_line = False
    decoded_json_objects = 0
    with open_text_reader(log_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            saw_nonempty_line = True
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON log line in %s", log_path)
                continue
            if not isinstance(data, dict):
                continue
            decoded_json_objects += 1
            record = normalize_request_record(data)
            if record is None:
                continue
            if record["method"].upper() == "CONNECT":
                continue

            host = urlparse(record["url"]).hostname
            if not is_host_in_scope(host, target_url):
                logger.debug("Skipping out-of-scope parsed URL host=%s", host)
                continue

            key = (record["method"], record["url"])
            entry = {
                "method": record["method"],
                "url": record["url"],
                "status": record["status"],
                "content_type": record["content_type"],
                "timestamp": record["timestamp"],
            }
            previous = deduped.get(key)
            if previous is None or previous["status"] is None or entry["status"] is not None:
                deduped[key] = entry

    if saw_nonempty_line and decoded_json_objects == 0:
        raise CrawlArtifactsCorruptError(
            f"Katana crawl artifact contains no valid JSON: {log_path}"
        )

    return build_sitemap(list(deduped.values()))
