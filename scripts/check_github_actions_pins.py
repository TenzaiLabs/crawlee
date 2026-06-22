from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_DIR = Path(".github/workflows")
USES_RE = re.compile(r"^\s*uses:\s*(?P<value>\S+)")
SHA_REF_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def main() -> int:
    errors: list[str] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        for lineno, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_RE.match(line)
            if not match:
                continue
            value = match.group("value")
            if value.startswith("./"):
                continue
            if not SHA_REF_RE.match(value):
                errors.append(
                    f"{workflow}:{lineno}: pin GitHub action to a 40-character commit SHA: {value}"
                )

    if errors:
        print("\n".join(errors))
        return 1
    print("github actions pins ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
