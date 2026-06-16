from __future__ import annotations


def format_cookie_header(cookies: list[dict]) -> str | None:
    pairs: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None
