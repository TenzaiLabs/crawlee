from __future__ import annotations

import pytest

from app import crawler


def test_build_katana_command_defaults():
    config = crawler.CrawlConfig(target_url="https://example.com")
    command = crawler.build_katana_command(config)

    assert "katana" in command[0]
    assert "-u" in command
    assert "https://example.com" in command
    assert "-proxy" in command
    assert "http://127.0.0.1:8888" in command
    assert command[command.index("-known-files") + 1] == "all"
    assert "-no-color" in command
    assert "-verbose" in command
    assert "-fs" in command
    assert "rdn" in command
    assert "-d" in command
    assert "-rl" in command
    assert "-crawl-out-scope" in command
    cos_index = command.index("-crawl-out-scope") + 1
    assert "logout" in command[cos_index]


def test_build_katana_command_overrides():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "max_depth": 2,
            "rate_limit": 5,
            "exclude_filters": ["/admin"],
            "crawl_scope": "app",
        },
        headers=["Cookie: session=abc"],
    )
    command = crawler.build_katana_command(config)

    assert command[command.index("-d") + 1] == "2"
    assert command[command.index("-rl") + 1] == "5"
    assert command[command.index("-cs") + 1] == "app"
    assert "-H" in command


def test_blocked_urls_to_exclude_patterns_normalizes_safe_same_scope_urls():
    patterns = crawler.blocked_urls_to_exclude_patterns(
        [
            "https://example.com/logout?next=/",
            "/account/delete/",
            "/account/delete",
            "javascript:alert(1)",
            "mailto:test@example.com",
            "https://evil.test/logout",
            "https://example.com/#logout",
        ],
        target_url="https://example.com",
        base_url="https://example.com/app/dashboard",
    )

    assert patterns == [
        "/logout(?:$|[/?#])",
        "/account/delete(?:$|[/?#])",
    ]


def test_build_katana_command_merges_dynamic_exclude_patterns():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "exclude_filters": ["/admin"],
            "exclude_regex": "/danger-zone",
        },
        dynamic_exclude_patterns=["/account/delete(?:$|[/?#])", "/admin"],
    )
    command = crawler.build_katana_command(config)
    exclusions = command[command.index("-crawl-out-scope") + 1]

    assert "logout" in exclusions
    assert "/admin" in exclusions
    assert "/danger-zone" in exclusions
    assert "/account/delete(?:$|[/?#])" in exclusions
    assert exclusions.count("/admin") == 1


def test_build_katana_command_headless_with_cdp_url():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "headless": True,
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
        },
    )

    command = crawler.build_katana_command(config)
    assert "-hybrid" in command
    assert command[command.index("-cwu") + 1].startswith("ws://")
    assert "-system-chrome" not in command


def test_build_katana_command_headless_with_system_chrome():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "headless": True,
            "system_chrome": True,
            "system_chrome_path": "/usr/bin/chromium",
        },
    )

    command = crawler.build_katana_command(config)
    assert "-hybrid" in command
    assert "-system-chrome" in command
    assert command[command.index("-system-chrome-path") + 1] == "/usr/bin/chromium"
    assert "-cwu" not in command


def test_build_katana_command_browser_options_require_headless():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "headless": False,
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
        },
    )
    with pytest.raises(ValueError, match="headless"):
        crawler.build_katana_command(config)


def test_build_katana_command_cdp_url_mutually_exclusive_with_system_chrome():
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "headless": True,
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
            "system_chrome": True,
        },
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        crawler.build_katana_command(config)
