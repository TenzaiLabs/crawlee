from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """
You are an authentication agent. Your goal is to log into a website.

You will be given the current page state.
Use the available tools to interact with the page and complete the login process.

Safety rules:
- Do NOT click logout, sign-out, delete, remove, unsubscribe, or deactivation links.
- Do NOT submit registration or sign-up forms.

Call done() when you believe authentication is complete.
""".strip()


def build_user_prompt(
    *,
    page_state: str,
    target_url: str,
    credentials: dict[str, Any],
    instructions: str | None,
    success_indicator: str | None,
) -> str:
    lines: list[str] = [
        "Current page state:",
        page_state,
        "",
        f"Target URL: {target_url}",
    ]
    if credentials:
        lines.append("Credentials:")
        for key, value in credentials.items():
            lines.append(f"- {key}: {value}")
    if instructions:
        lines.append("")
        lines.append("Instructions:")
        lines.append(instructions)
    if success_indicator:
        lines.append("")
        lines.append(f"Success indicator (CSS selector or text): {success_indicator}")
    return "\n".join(lines).strip()


async def extract_page_state(page: Any, *, text_limit: int = 4000) -> str:
    """Return a simplified text representation of the current page."""

    js = r"""
    (textLimit) => {
      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style) return false;
        if (
          style.display === 'none' ||
          style.visibility === 'hidden' ||
          style.opacity === '0'
        ) return false;
        if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };

      const attr = (el, name) => (el && el.getAttribute) ? el.getAttribute(name) : null;
      const safeText = (s) => (s || '').toString().replace(/\s+/g, ' ').trim();

      const inputs = Array.from(document.querySelectorAll('input, textarea'))
        .filter(isVisible)
        .slice(0, 80)
        .map(el => ({
          tag: el.tagName.toLowerCase(),
          type: (el.getAttribute('type') || '').toString(),
          name: attr(el, 'name') || '',
          id: attr(el, 'id') || '',
          placeholder: attr(el, 'placeholder') || '',
          value: (el.value || '').toString().slice(0, 80),
        }));

      const selects = Array.from(document.querySelectorAll('select'))
        .filter(isVisible)
        .slice(0, 40)
        .map(el => ({
          name: attr(el, 'name') || '',
          id: attr(el, 'id') || '',
        }));

      const buttons = Array.from(
        document.querySelectorAll('button, input[type=submit], input[type=button]')
      )
        .filter(isVisible)
        .slice(0, 60)
        .map(el => ({
          tag: el.tagName.toLowerCase(),
          type: (el.getAttribute('type') || '').toString(),
          id: attr(el, 'id') || '',
          text: safeText(el.innerText || el.value || ''),
        }));

      const links = Array.from(document.querySelectorAll('a[href]'))
        .filter(isVisible)
        .slice(0, 80)
        .map(el => ({
          text: safeText(el.innerText || ''),
          href: (el.getAttribute('href') || '').toString(),
        }));

      const rawText = safeText(document.body ? document.body.innerText : '').slice(0, textLimit);

      return {
        url: location.href,
        title: document.title,
        inputs,
        selects,
        buttons,
        links,
        text: rawText,
      };
    }
    """

    payload = await page.evaluate(js, text_limit)
    if not isinstance(payload, dict):
        return "(unable to read page state)"

    def _lines_for_items(label: str, items: Any, formatter) -> list[str]:
        if not isinstance(items, list) or not items:
            return []
        lines = [f"{label}:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            rendered = formatter(item)
            if rendered:
                lines.append(f"- {rendered}")
        return lines

    url = str(payload.get("url") or "")
    title = str(payload.get("title") or "")
    lines: list[str] = [f"URL: {url}", f"Title: {title}"]

    lines.extend(
        _lines_for_items(
            "Inputs",
            payload.get("inputs"),
            lambda i: (
                f"{i.get('tag', 'input')} type={i.get('type', '')} "
                f"name={i.get('name', '')} id={i.get('id', '')} "
                f"placeholder={i.get('placeholder', '')}"
            ),
        )
    )
    lines.extend(
        _lines_for_items(
            "Selects",
            payload.get("selects"),
            lambda i: f"name={i.get('name', '')} id={i.get('id', '')}",
        )
    )
    lines.extend(
        _lines_for_items(
            "Buttons",
            payload.get("buttons"),
            lambda i: (
                f"{i.get('tag', 'button')} type={i.get('type', '')} "
                f"id={i.get('id', '')} text={i.get('text', '')}"
            ),
        )
    )
    lines.extend(
        _lines_for_items(
            "Links",
            payload.get("links"),
            lambda i: f"text={i.get('text', '')} href={i.get('href', '')}",
        )
    )

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        lines.append("VisibleText:")
        lines.append(text)

    return "\n".join(lines).strip()
