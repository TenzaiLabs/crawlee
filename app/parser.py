from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from .common import is_host_in_scope, open_text_reader
from .db import LOG_DIR
from .log_records import normalize_request_record

logger = logging.getLogger(__name__)


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


def parse_log(job_id: str, target_url: str | None = None) -> dict[str, Any]:
    log_path = os.path.join(LOG_DIR, f"{job_id}.jsonl")
    katana_log_path = log_path + ".katana"
    log_files = [path for path in (log_path, katana_log_path) if os.path.exists(path)]
    logger.info("Parsing crawl logs for job_id=%s", job_id)
    if not log_files:
        logger.warning("No crawl logs found for job_id=%s", job_id)
        return {"entries": [], "tree": {"children": {}, "pages": []}}

    deduped: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    for path in log_files:
        logger.debug("Reading crawl log file path=%s", path)
        with open_text_reader(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON log line in %s", path)
                    continue

                if not isinstance(data, dict):
                    continue
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
                deduped[key] = {
                    "method": record["method"],
                    "url": record["url"],
                    "status": record["status"],
                    "content_type": record["content_type"],
                    "timestamp": record["timestamp"],
                }

    entries = list(deduped.values())
    return {
        "entries": entries,
        "tree": _build_tree(entries),
    }
