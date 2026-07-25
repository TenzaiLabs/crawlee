from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

EXPECTED_DIT_MODEL_SHA256 = "eba238b3093ff7aa4772ce17536bc313cb955428a6aa87dae41695a2dede6e59"
EXPECTED_KATANA_SHA256 = "49ab204962b91b4de9ee81b0f227716bae6f13ce71acadff60fe17e3ac1cb196"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_server_home(
    temp_dir: Path,
    configured_model: Path | None,
    configured_katana: Path | None,
) -> tuple[Path, Path, Path]:
    model_path = configured_model or Path.home() / ".dit" / "model.json"
    if not model_path.is_file():
        raise ValueError(
            "the pinned DIT classifier model is required; pass --dit-model or install "
            "it at $HOME/.dit/model.json"
        )
    actual_sha256 = sha256_file(model_path)
    if actual_sha256 != EXPECTED_DIT_MODEL_SHA256:
        raise ValueError(
            "DIT classifier model checksum mismatch: "
            f"expected {EXPECTED_DIT_MODEL_SHA256}, got {actual_sha256}"
        )

    browser_cache = Path(
        os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH",
            str(Path.home() / ".cache" / "ms-playwright"),
        )
    )
    if not browser_cache.is_dir():
        raise ValueError(
            "the Playwright browser cache is required; install Chromium or set "
            "PLAYWRIGHT_BROWSERS_PATH"
        )

    resolved_katana = configured_katana
    if resolved_katana is None:
        executable = shutil.which("katana")
        resolved_katana = Path(executable) if executable else None
    if resolved_katana is None or not resolved_katana.is_file():
        raise ValueError("the qualified Katana binary is required; pass --katana-binary")
    katana_sha256 = sha256_file(resolved_katana)
    if katana_sha256 != EXPECTED_KATANA_SHA256:
        raise ValueError(
            "Katana binary checksum mismatch: "
            f"expected {EXPECTED_KATANA_SHA256}, got {katana_sha256}; "
            "pass --katana-binary with the pinned project build"
        )

    home_dir = temp_dir / "home"
    model_dir = home_dir / ".dit"
    model_dir.mkdir(parents=True)
    (model_dir / "model.json").symlink_to(model_path.resolve())
    cache_dir = home_dir / ".cache"
    cache_dir.mkdir()
    (cache_dir / "ms-playwright").symlink_to(browser_cache.resolve(), target_is_directory=True)
    bin_dir = home_dir / "bin"
    bin_dir.mkdir()
    (bin_dir / "katana").symlink_to(resolved_katana.resolve())
    return home_dir, model_path.resolve(), resolved_katana.resolve()
