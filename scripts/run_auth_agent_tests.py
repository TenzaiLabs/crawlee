from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from app import auth_agent


@dataclass(frozen=True)
class AuthAgentSiteCase:
    name: str
    target_url: str
    auth_config: dict[str, Any] | None
    probe_path: str
    mode: str
    expected_blocked_paths: tuple[str, ...] = ()


@dataclass
class AuthAgentSiteResult:
    name: str
    mode: str
    passed: bool
    status_code: int | None = None
    landing_url: str | None = None
    header_count: int = 0
    blocked_url_count: int = 0
    error: str | None = None


def _headers_from_lines(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    cookies: list[str] = []
    for line in lines:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        clean_name = name.strip()
        clean_value = value.strip()
        if clean_name.lower() == "cookie":
            cookies.append(clean_value)
        else:
            headers[clean_name] = clean_value
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


def _with_port(base: str, port: int) -> str:
    return f"{base.rstrip('/')}:{port}"


def build_cases(*, gateway: bool = False) -> list[AuthAgentSiteCase]:
    if gateway:
        ports = {
            "site-a-static": 9101,
            "site-b-login-flask": 9102,
            "site-c-registration-express": 9103,
            "site-d-complex-auth-go": 9104,
            "site-e-crawl-trap-ruby": 9105,
            "site-f-spa-deno": 9106,
            "auth-a-simple-form": 9201,
            "auth-b-http-basic": 9202,
            "auth-c-complex-form": 9203,
            "auth-d-interactive-captcha": 9204,
            "auth-e-delay-login": 9205,
            "auth-f-ocr-captcha": 9206,
            "auth-g-multi-step": 9207,
            "auth-h-new-window": 9208,
            "auth-i-iframe": 9209,
            "auth-j-xsrf-token": 9210,
            "auth-k-dynamic-fields": 9211,
            "auth-l-security-question": 9212,
            "auth-m-totp-mfa": 9213,
            "auth-o-bearer-token": 9215,
        }
    else:
        ports = {
            "site-a-static": 8001,
            "site-b-login-flask": 8002,
            "site-c-registration-express": 8003,
            "site-d-complex-auth-go": 8004,
            "site-e-crawl-trap-ruby": 8005,
            "site-f-spa-deno": 8006,
            "auth-a-simple-form": 8101,
            "auth-b-http-basic": 8102,
            "auth-c-complex-form": 8103,
            "auth-d-interactive-captcha": 8104,
            "auth-e-delay-login": 8105,
            "auth-f-ocr-captcha": 8106,
            "auth-g-multi-step": 8107,
            "auth-h-new-window": 8108,
            "auth-i-iframe": 8109,
            "auth-j-xsrf-token": 8110,
            "auth-k-dynamic-fields": 8111,
            "auth-l-security-question": 8112,
            "auth-m-totp-mfa": 8113,
            "auth-o-bearer-token": 8115,
        }

    def url(name: str) -> str:
        return _with_port("http://localhost", ports[name])

    public_cases = [
        AuthAgentSiteCase("site-a-static", url("site-a-static"), None, "/", "public"),
        AuthAgentSiteCase(
            "site-c-registration-express",
            url("site-c-registration-express"),
            None,
            "/",
            "public",
        ),
        AuthAgentSiteCase(
            "site-e-crawl-trap-ruby",
            url("site-e-crawl-trap-ruby"),
            None,
            "/",
            "public",
        ),
        AuthAgentSiteCase("site-f-spa-deno", url("site-f-spa-deno"), None, "/", "public"),
    ]

    llm_no_auth_cases = [
        AuthAgentSiteCase(
            "site-a-static-llm-no-auth",
            url("site-a-static"),
            {
                "login_url": url("site-a-static"),
                "instructions": (
                    "This site may not have an authentication screen. If no login form is "
                    "present and the target page is already accessible, verify and finish."
                ),
            },
            "/",
            "llm_no_auth",
        )
    ]

    manual_cases = [
        AuthAgentSiteCase(
            "auth-b-http-basic",
            url("auth-b-http-basic"),
            {"headers": ["Authorization: Basic dXNlcjpwYXNz"]},
            "/app/overview",
            "manual_headers",
        ),
        AuthAgentSiteCase(
            "auth-o-bearer-token",
            url("auth-o-bearer-token"),
            {"headers": ["Authorization: Bearer t0k3nId"]},
            "/app/overview",
            "manual_headers",
        ),
    ]

    llm_cases = [
        AuthAgentSiteCase(
            "site-b-login-flask",
            url("site-b-login-flask"),
            {
                "login_url": urljoin(url("site-b-login-flask"), "/login"),
                "credentials": {"username": "demo", "password": "password"},
            },
            "/dashboard",
            "llm",
        ),
        AuthAgentSiteCase(
            "site-d-complex-auth-go",
            url("site-d-complex-auth-go"),
            {
                "login_url": urljoin(url("site-d-complex-auth-go"), "/login"),
                "credentials": {"username": "admin", "password": "swordfish"},
                "instructions": "There are two submit buttons. Click Sign In, not Register.",
            },
            "/app",
            "llm",
        ),
    ]

    llm_cases.append(
        AuthAgentSiteCase(
            "auth-a-simple-form-dynamic-exclusion",
            url("auth-a-simple-form"),
            {
                "login_url": urljoin(url("auth-a-simple-form"), "/login"),
                "credentials": {
                    "email": "auth-a-simple-form@auth.local",
                    "password": "pa$$w0rd",
                },
                "instructions": (
                    "After login, record /app/danger/close-account as a blocked URL "
                    "because it closes the account. Do not click it."
                ),
            },
            "/app/overview",
            "llm",
            ("/app/danger/close-account",),
        )
    )

    auth_patterns = [
        ("auth-a-simple-form", {}, ""),
        (
            "auth-c-complex-form",
            {},
            "Fill tenant and region if required. Use tenant north and region us-east.",
        ),
        (
            "auth-d-interactive-captcha",
            {"challenge_code": "588357"},
            "Enter challenge code 588357.",
        ),
        ("auth-e-delay-login", {}, "The login may take a moment after submit."),
        ("auth-f-ocr-captcha", {"captcha_code": "4319"}, "Enter captcha code 4319."),
        (
            "auth-g-multi-step",
            {},
            "This is multi-step: submit e-mail first, then password.",
        ),
        (
            "auth-h-new-window",
            {},
            "The login opens in a popup/new tab; continue there.",
        ),
        ("auth-i-iframe", {}, "The login form is inside an iframe."),
        ("auth-j-xsrf-token", {}, "Submit the visible form; preserve hidden XSRF fields."),
        (
            "auth-k-dynamic-fields",
            {},
            "Field names are dynamic; use labels, types, and visible text.",
        ),
        (
            "auth-l-security-question",
            {"security_answer": "42"},
            "Fill the security question answer with 42.",
        ),
        (
            "auth-m-totp-mfa",
            {"totp_secret": "I65VU7K5ZQL7WB4E"},
            "When prompted for MFA/TOTP, call get_totp_code('totp_secret') and enter the code.",
        ),
    ]
    for name, extra_credentials, instructions in auth_patterns:
        credentials: dict[str, Any] = {
            "email": f"{name}@auth.local",
            "password": "pa$$w0rd",
        }
        credentials.update(extra_credentials)
        config: dict[str, Any] = {
            "login_url": urljoin(url(name), "/login"),
            "credentials": credentials,
        }
        if instructions:
            config["instructions"] = instructions
        llm_cases.append(AuthAgentSiteCase(name, url(name), config, "/app/overview", "llm"))

    return public_cases + llm_no_auth_cases + manual_cases + llm_cases


async def _probe(
    client: httpx.AsyncClient,
    case: AuthAgentSiteCase,
    headers: dict[str, str] | None = None,
) -> int:
    response = await client.get(
        urljoin(case.target_url, case.probe_path),
        headers=headers,
        follow_redirects=False,
    )
    return response.status_code


async def run_case(case: AuthAgentSiteCase, *, timeout: float) -> AuthAgentSiteResult:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if case.mode == "public":
                status_code = await _probe(client, case)
                return AuthAgentSiteResult(
                    name=case.name,
                    mode=case.mode,
                    passed=200 <= status_code < 400,
                    status_code=status_code,
                )

            if case.mode == "manual_headers":
                headers = _headers_from_lines(case.auth_config.get("headers", []))  # type: ignore[union-attr]
                status_code = await _probe(client, case, headers=headers)
                return AuthAgentSiteResult(
                    name=case.name,
                    mode=case.mode,
                    passed=200 <= status_code < 300,
                    status_code=status_code,
                    header_count=len(headers),
                )

        if case.auth_config is None:
            return AuthAgentSiteResult(
                name=case.name,
                mode=case.mode,
                passed=False,
                error="missing auth_config",
            )

        result = await auth_agent.authenticate(case.target_url, case.auth_config, asyncio.Event())
        headers = _headers_from_lines(result.headers)
        async with httpx.AsyncClient(timeout=timeout) as client:
            status_code = await _probe(client, case, headers=headers)
        blocked_urls = result.blocked_urls or []
        missing_blocked_paths = [
            path
            for path in case.expected_blocked_paths
            if not any(
                urljoin(case.target_url, path) == blocked_url for blocked_url in blocked_urls
            )
        ]
        return AuthAgentSiteResult(
            name=case.name,
            mode=case.mode,
            passed=200 <= status_code < 300 and not missing_blocked_paths,
            status_code=status_code,
            landing_url=result.landing_url,
            header_count=len(result.headers),
            blocked_url_count=len(blocked_urls),
            error=(
                f"missing blocked URL(s): {', '.join(missing_blocked_paths)}"
                if missing_blocked_paths
                else None
            ),
        )
    except Exception as exc:
        return AuthAgentSiteResult(
            name=case.name,
            mode=case.mode,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )


async def run_cases(cases: list[AuthAgentSiteCase], *, timeout: float) -> list[AuthAgentSiteResult]:
    results: list[AuthAgentSiteResult] = []
    for case in cases:
        print(f"auth-agent-test {case.name} ({case.mode}) ...", flush=True)
        result = await run_case(case, timeout=timeout)
        status = "PASS" if result.passed else "FAIL"
        detail = result.error or f"status={result.status_code} landing={result.landing_url}"
        print(f"  {status} {detail}", flush=True)
        results.append(result)
    return results


def _select_cases(
    cases: list[AuthAgentSiteCase],
    *,
    names: list[str],
    modes: list[str],
) -> list[AuthAgentSiteCase]:
    selected = cases
    if names:
        wanted = set(names)
        selected = [case for case in selected if case.name in wanted]
    if modes:
        wanted_modes = set(modes)
        selected = [case for case in selected if case.mode in wanted_modes]
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the auth agent against local test websites.")
    parser.add_argument("--gateway", action="store_true", help="Use nginx gateway ports 9101-9215.")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run one named case. Repeatable.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=["public", "llm_no_auth", "manual_headers", "llm"],
        default=[],
        help="Run cases by mode. Repeatable.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP probe timeout seconds.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    cases = _select_cases(build_cases(gateway=args.gateway), names=args.case, modes=args.mode)
    if not cases:
        print("No cases selected.")
        return 2

    results = await run_cases(cases, timeout=args.timeout)
    failed = [result for result in results if not result.passed]
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2, sort_keys=True))
    print(f"auth-agent-test summary: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
