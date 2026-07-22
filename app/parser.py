from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from . import db
from .common import is_host_in_scope, open_text_reader
from .log_records import normalize_request_record

logger = logging.getLogger(__name__)


class CrawlArtifactsMissingError(FileNotFoundError):
    pass


class CrawlArtifactsCorruptError(ValueError):
    pass


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


def parse_log(
    job_id: str,
    target_url: str | None = None,
    *,
    require_artifacts: bool = False,
) -> dict[str, Any]:
    log_path = os.path.join(db.LOG_DIR, f"{job_id}.jsonl")
    katana_log_path = log_path + ".katana"
    log_files = [path for path in (log_path, katana_log_path) if os.path.exists(path)]
    logger.info("Parsing crawl logs for job_id=%s", job_id)
    if not log_files:
        logger.warning("No crawl logs found for job_id=%s", job_id)
        if require_artifacts:
            raise CrawlArtifactsMissingError(f"No crawl artifacts found for job {job_id}")
        return {"entries": [], "tree": {"children": {}, "pages": []}}

    deduped: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    saw_nonempty_line = False
    decoded_json_objects = 0
    for path in log_files:
        logger.debug("Reading crawl log file path=%s", path)
        with open_text_reader(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                saw_nonempty_line = True
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON log line in %s", path)
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

    if require_artifacts and saw_nonempty_line and decoded_json_objects == 0:
        raise CrawlArtifactsCorruptError(f"Crawl artifacts for job {job_id} contain no valid JSON")

    entries = list(deduped.values())
    return validate_sitemap(
        {
            "entries": entries,
            "tree": _build_tree(entries),
        }
    )
