#!/usr/bin/env python3
"""Dependency-free smoke test for the isolated external target package."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True, timeout=240)


def request(url: str, *, method: str = "GET", data: object | None = None) -> tuple[int, bytes]:
    body = None if data is None else json.dumps(data).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def validate_manifests() -> None:
    required = {
        "seed_url", "health_probe", "reset_operation", "auth_reference",
        "allowed_origins", "allowed_ports", "baseline_exclusions",
        "expected_deterministic_surface", "target_version",
    }
    for path in sorted((ROOT / "manifests").glob("*.json")):
        document = json.loads(path.read_text())
        if path.name == "crawlground-inventory.json":
            assert len(document["controls"]) == 59
            assert len(document["controls"]) == len(set(document["controls"]))
            continue
        missing = required - document.keys()
        assert not missing, f"{path.name} missing {sorted(missing)}"
        assert document["target_version"], f"{path.name} has no version pin"


def smoke() -> None:
    status, _ = request("http://127.0.0.1:13000/rest/admin/application-version")
    assert status == 200, f"Juice Shop health returned {status}"
    status, body = request("http://127.0.0.1:18080/parabank/")
    assert status == 200 and b"ParaBank" in body, f"ParaBank health returned {status}"

    run(str(SCRIPTS / "reset-crawlground.sh"))
    status, _ = request(
        "http://127.0.0.1:13456/set-tool",
        method="POST",
        data={"name": "external-smoke"},
    )
    assert status == 200
    status, _ = request("http://127.0.0.1:13456/score/links/01-anchor-href")
    assert status == 200
    status, body = request("http://127.0.0.1:13456/results.json")
    results = json.loads(body)
    marker = next(test for test in results["tests"] if test["id"] == "links.01-anchor-href")
    assert status == 200 and marker["tools"]["external-smoke"]["scored"] is True
    run(str(SCRIPTS / "reset-crawlground.sh"))
    _, body = request("http://127.0.0.1:13456/results.json")
    results = json.loads(body)
    assert results["summary"]["scored"] == 0, "CrawlGround reset retained smoke scores"

    run(str(SCRIPTS / "reset-juice-shop.sh"))
    run(str(SCRIPTS / "reset-parabank.sh"))
    status, body = request("http://127.0.0.1:18080/parabank/services/bank/login/john/demo")
    assert status == 200 and b"customer" in body.lower(), (
        "ParaBank reset did not restore seeded login"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-start", action="store_true")
    args = parser.parse_args()
    validate_manifests()
    if not args.skip_start:
        run(str(SCRIPTS / "up.sh"))
    run(str(SCRIPTS / "health.sh"))
    smoke()
    print("External target smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
