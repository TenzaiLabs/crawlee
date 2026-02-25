from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent


@dataclass
class SiteConfig:
    name: str
    workdir: Path
    command: list[str]
    env: dict[str, str]
    sitemap_path: Path
    startup_timeout: float = 15.0


def wait_for_port(host: str, port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


def request_url(method: str, url: str, headers: dict[str, str] | None = None) -> int:
    req = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code


def verify_sitemap(sitemap_path: Path) -> list[str]:
    data = json.loads(sitemap_path.read_text())
    entries = data.get("entries", [])
    failures: list[str] = []
    for entry in entries:
        method = entry.get("method")
        url = entry.get("url")
        expected_status = entry.get("status")
        headers = entry.get("headers")
        if not method or not url or expected_status is None:
            failures.append(f"Invalid entry in {sitemap_path}: {entry}")
            continue
        try:
            status = request_url(method, url, headers=headers)
        except Exception as exc:  # pragma: no cover - runtime
            failures.append(f"{method} {url} failed: {exc}")
            continue
        if status != expected_status:
            failures.append(f"{method} {url} returned {status}, expected {expected_status}")
    return failures


def run_site(config: SiteConfig) -> list[str]:
    executable = config.command[0]
    if shutil.which(executable) is None:
        return [f"Missing runtime for {executable}. Install it to run {config.name}."]
    env = os.environ.copy()
    env.update(config.env)
    if config.name == "site-e-crawl-trap-ruby":
        if shutil.which("bundle") is None:
            return ["Missing runtime for bundle. Install bundler to run site-e-crawl-trap-ruby."]
        bundle_path = config.workdir / "vendor" / "bundle"
        bundle_path.mkdir(parents=True, exist_ok=True)
        env["BUNDLE_PATH"] = str(bundle_path)
        bundle_install = subprocess.run(
            ["bundle", "install"],
            cwd=config.workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if bundle_install.returncode != 0:
            message = bundle_install.stderr.strip() or bundle_install.stdout.strip() or "bundle install failed"
            return [f"Bundle install failed: {message}"]
    process = subprocess.Popen(
        config.command,
        cwd=config.workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        wait_for_port("127.0.0.1", int(config.env["PORT"]), config.startup_timeout)
        return verify_sitemap(config.sitemap_path)
    except RuntimeError as exc:
        output = ""
        try:
            if process.stdout is not None:
                collected, _ = process.communicate(timeout=1)
                output = collected.decode("utf-8", errors="replace") if collected else ""
        except Exception:
            output = ""
        message = str(exc)
        if output:
            message = f"{message}. Output: {output.strip()}"
        return [message]
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return []
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return []
            process.wait(timeout=5)


def main(configs: Iterable[SiteConfig]) -> int:
    overall_failures: list[str] = []
    for config in configs:
        print(f"Checking {config.name}...")
        failures = run_site(config)
        if failures:
            overall_failures.append(f"{config.name} failures:")
            overall_failures.extend([f"  - {failure}" for failure in failures])
        else:
            print(f"  {config.name} ok")
    if overall_failures:
        print("\n".join(overall_failures))
        return 1
    return 0


def auth_pattern_configs(flask_python: str) -> list[SiteConfig]:
    patterns = [
        ("auth-a-simple-form", 8101),
        ("auth-b-http-basic", 8102),
        ("auth-c-complex-form", 8103),
        ("auth-d-interactive-captcha", 8104),
        ("auth-e-delay-login", 8105),
        ("auth-f-ocr-captcha", 8106),
        ("auth-g-multi-step", 8107),
        ("auth-h-new-window", 8108),
        ("auth-i-iframe", 8109),
        ("auth-j-xsrf-token", 8110),
        ("auth-k-dynamic-fields", 8111),
        ("auth-l-security-question", 8112),
        ("auth-m-totp-mfa", 8113),

        ("auth-o-bearer-token", 8115),
    ]
    return [
        SiteConfig(
            name=name,
            workdir=ROOT / name,
            command=[flask_python, "app.py"],
            env={"PORT": str(port)},
            sitemap_path=ROOT / name / "sitemap.json",
        )
        for name, port in patterns
    ]


if __name__ == "__main__":
    venv_python = ROOT / ".venv" / "bin" / "python"
    flask_python = str(venv_python) if venv_python.exists() else sys.executable
    configs = [
        SiteConfig(
            name="site-a-static",
            workdir=ROOT / "site-a-static",
            command=[sys.executable, "-m", "http.server", "8001", "--directory", "html"],
            env={"PORT": "8001"},
            sitemap_path=ROOT / "site-a-static" / "sitemap.json",
        ),
        SiteConfig(
            name="site-b-login-flask",
            workdir=ROOT / "site-b-login-flask",
            command=[flask_python, "app.py"],
            env={"PORT": "8002"},
            sitemap_path=ROOT / "site-b-login-flask" / "sitemap.json",
        ),
        SiteConfig(
            name="site-c-registration-express",
            workdir=ROOT / "site-c-registration-express",
            command=["node", "server.js"],
            env={"PORT": "8003"},
            sitemap_path=ROOT / "site-c-registration-express" / "sitemap.json",
        ),
        SiteConfig(
            name="site-d-complex-auth-go",
            workdir=ROOT / "site-d-complex-auth-go",
            command=["go", "run", "main.go"],
            env={"PORT": "8004"},
            sitemap_path=ROOT / "site-d-complex-auth-go" / "sitemap.json",
            startup_timeout=30.0,
        ),
        SiteConfig(
            name="site-e-crawl-trap-ruby",
            workdir=ROOT / "site-e-crawl-trap-ruby",
            command=["ruby", "app.rb"],
            env={"PORT": "8005"},
            sitemap_path=ROOT / "site-e-crawl-trap-ruby" / "sitemap.json",
        ),
        SiteConfig(
            name="site-f-spa-deno",
            workdir=ROOT / "site-f-spa-deno",
            command=["deno", "run", "--allow-net", "--allow-read", "--allow-env", "server.ts"],
            env={"PORT": "8006"},
            sitemap_path=ROOT / "site-f-spa-deno" / "sitemap.json",
        ),
        *auth_pattern_configs(flask_python),
    ]
    raise SystemExit(main(configs))
