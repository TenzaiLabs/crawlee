from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

DOCS_ROOT = Path("docs")
HTML_FILES = (DOCS_ROOT / "index.html", DOCS_ROOT / "docs.html")


class LocalLinkParser(HTMLParser):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        value = attr_map.get("href") if tag in {"a", "link"} else attr_map.get("src")
        if not value or value.startswith(("http://", "https://", "#", "mailto:")):
            return
        if value.startswith("/"):
            return

        local_path = value.split("#", 1)[0]
        if not local_path:
            return
        target = (self.path.parent / local_path).resolve()
        if not target.exists():
            self.errors.append(f"{self.path}: missing local asset/link {value}")


def main() -> int:
    errors: list[str] = []
    for html_file in HTML_FILES:
        if not html_file.exists():
            errors.append(f"missing docs HTML file {html_file}")
            continue
        parser = LocalLinkParser(html_file)
        parser.feed(html_file.read_text(encoding="utf-8"))
        errors.extend(parser.errors)

    if errors:
        print("\n".join(errors))
        return 1
    print("docs links ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
