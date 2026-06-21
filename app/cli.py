from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .auth_config import AuthConfigValidationError, validate_auth_config
from .auth_secrets import resolve_secrets
from .scope_config import ScopeConfigValidationError, validate_scope_config

logger = logging.getLogger(__name__)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _load_scope_config_from_json_input(args: argparse.Namespace) -> dict[str, Any]:
    raw_json = getattr(args, "scope_config_json", None)
    scope_config_file = getattr(args, "scope_config_file", None)

    if raw_json and scope_config_file:
        raise ValueError("--scope-config-json and --scope-config-file are mutually exclusive")

    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid --scope-config-json value: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--scope-config-json must decode to a JSON object")
        return {str(key): value for key, value in parsed.items()}

    if scope_config_file:
        path = Path(scope_config_file)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Unable to read --scope-config-file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in --scope-config-file: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--scope-config-file must contain a JSON object")
        return {str(key): value for key, value in parsed.items()}

    return {}


def _load_auth_config_from_json_input(args: argparse.Namespace) -> dict[str, Any]:
    raw_json = getattr(args, "auth_config_json", None)
    auth_config_file = getattr(args, "auth_config_file", None)

    if raw_json and auth_config_file:
        raise ValueError("--auth-config-json and --auth-config-file are mutually exclusive")

    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid --auth-config-json value: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--auth-config-json must decode to a JSON object")
        return {str(key): value for key, value in parsed.items()}

    if auth_config_file:
        path = Path(auth_config_file)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Unable to read --auth-config-file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in --auth-config-file: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--auth-config-file must contain a JSON object")
        return {str(key): value for key, value in parsed.items()}

    return {}


def _build_auth_config(args: argparse.Namespace) -> dict[str, Any] | None:
    auth_config = _load_auth_config_from_json_input(args)
    headers = getattr(args, "auth_header", None)
    if headers:
        auth_config["headers"] = list(headers)
    login_url = getattr(args, "auth_login_url", None)
    if login_url:
        auth_config["login_url"] = login_url
    return auth_config or None


def _build_scope_config(args: argparse.Namespace) -> dict[str, Any] | None:
    scope_config = _load_scope_config_from_json_input(args)
    if getattr(args, "headless", False):
        scope_config["headless"] = True
    if getattr(args, "cdp_url", None):
        scope_config["cdp_url"] = args.cdp_url
    if getattr(args, "system_chrome", False):
        scope_config["system_chrome"] = True
    if getattr(args, "system_chrome_path", None):
        scope_config["system_chrome_path"] = args.system_chrome_path
    return scope_config or None


async def create_job(
    client: httpx.AsyncClient,
    target_url: str,
    scope_config: dict[str, Any] | None = None,
    auth_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.info("CLI create_job target_url=%s", target_url)
    payload: dict[str, Any] = {"target_url": target_url}
    if scope_config:
        payload["scope_config"] = scope_config
    if auth_config:
        payload["auth_config"] = auth_config
    response = await client.post("/jobs", json=payload)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise SystemExit(f"API error {response.status_code}: {detail}")
    return response.json()


async def get_job(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    logger.debug("CLI get_job job_id=%s", job_id)
    response = await client.get(f"/jobs/{job_id}")
    response.raise_for_status()
    return response.json()


async def list_jobs(client: httpx.AsyncClient) -> dict[str, Any]:
    logger.debug("CLI list_jobs")
    response = await client.get("/jobs")
    response.raise_for_status()
    return response.json()


async def cancel_job(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    logger.info("CLI cancel_job job_id=%s", job_id)
    response = await client.post(f"/jobs/{job_id}/cancel")
    response.raise_for_status()
    return response.json()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tenzai Crawler CLI",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run tenzai-crawler create https://example.com\n"
            "  uv run tenzai-crawler status <job_id>\n"
            "  uv run tenzai-crawler cancel <job_id>\n"
            "  uv run tenzai-crawler --base-url http://host:8000 status <job_id>"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Request timeout in seconds (default: 30)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a crawl job")
    create_parser.add_argument("target_url", help="Target URL to crawl")
    create_parser.add_argument(
        "--scope-config-json",
        default=None,
        help="Full scope_config JSON object string for advanced options",
    )
    create_parser.add_argument(
        "--scope-config-file",
        default=None,
        help="Path to a JSON file containing a full scope_config object",
    )
    create_parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Enable headless browser crawling "
            "(required for --cdp-url/--system-chrome/--system-chrome-path)"
        ),
    )
    create_parser.add_argument(
        "--cdp-url",
        dest="cdp_url",
        default=None,
        help="CDP WebSocket URL to reuse an existing Chrome instance (requires --headless)",
    )
    create_parser.add_argument(
        "--system-chrome",
        action="store_true",
        help=(
            "Use system Chrome/Chromium for headless crawling "
            "(requires --headless; mutually exclusive with --cdp-url)"
        ),
    )
    create_parser.add_argument(
        "--system-chrome-path",
        default=None,
        help=(
            "Path to Chrome/Chromium binary "
            "(requires --headless; mutually exclusive with --cdp-url)"
        ),
    )
    create_parser.add_argument(
        "--auth-config-json",
        default=None,
        help="Full auth_config JSON object string for advanced options",
    )
    create_parser.add_argument(
        "--auth-config-file",
        default=None,
        help="Path to a JSON file containing a full auth_config object",
    )
    create_parser.add_argument(
        "--auth-header",
        action="append",
        default=None,
        help="Auth header in 'Name: Value' format (repeatable)",
    )
    create_parser.add_argument(
        "--auth-login-url",
        dest="auth_login_url",
        default=None,
        help="Login URL for AI-auth mode",
    )

    subparsers.add_parser("list", help="List active and queued jobs")

    status_parser = subparsers.add_parser("status", help="Get job status")
    status_parser.add_argument("job_id", help="Job identifier")

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a job")
    cancel_parser.add_argument("job_id", help="Job identifier")

    return parser


async def _run(
    args: argparse.Namespace,
    scope_config: dict[str, Any] | None,
    auth_config: dict[str, Any] | None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
        if args.command == "create":
            return await create_job(
                client, args.target_url, scope_config=scope_config, auth_config=auth_config
            )
        if args.command == "list":
            return await list_jobs(client)
        if args.command == "status":
            return await get_job(client, args.job_id)
        return await cancel_job(client, args.job_id)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    scope_config = None
    auth_config = None
    if args.command == "create":
        try:
            scope_config = _build_scope_config(args)
            validate_scope_config(scope_config)
        except (ScopeConfigValidationError, ValueError) as exc:
            logger.warning("CLI scope config validation failed: %s", exc)
            parser.error(str(exc))
        try:
            auth_config = _build_auth_config(args)
            validate_auth_config(auth_config)
            if auth_config:
                auth_config = resolve_secrets(auth_config)
        except (AuthConfigValidationError, ValueError) as exc:
            logger.warning("CLI auth config validation failed: %s", exc)
            parser.error(str(exc))

    payload = asyncio.run(_run(args, scope_config, auth_config))
    _print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
