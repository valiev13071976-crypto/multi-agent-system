"""Safe Markdown rendering for chat messages."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_CODE_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_UL = re.compile(r"^[\-\*]\s+(.+)$", re.MULTILINE)


def _safe_url(url: str) -> str | None:
    raw = url.strip().lower()
    if raw.startswith("javascript:") or raw.startswith("data:"):
        return None
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme not in {"http", "https", "mailto"}:
        return None
    return html.escape(url.strip(), quote=True)


def render_markdown_safe(text: str) -> str:
    """Render subset of Markdown to escaped HTML — no raw script execution."""
    if not text:
        return ""
    fences: list[str] = []

    def _fence_store(m):
        lang = html.escape(m.group(1) or "")
        body = html.escape(m.group(2))
        fences.append(f'<pre class="code-block" data-lang="{lang}"><code>{body}</code></pre>')
        return f"\x00FENCE{len(fences)-1}\x00"

    working = _CODE_FENCE.sub(_fence_store, text)

    def _link(m):
        label = html.escape(m.group(1))
        href = _safe_url(m.group(2))
        if href is None:
            return f"[{label}]"
        return f'<a href="{href}" rel="noopener noreferrer" target="_blank">{label}</a>'

    working = _LINK.sub(_link, working)
    out = html.escape(working, quote=False)
    for i, block in enumerate(fences):
        out = out.replace(html.escape(f"\x00FENCE{i}\x00"), block)
    out = _INLINE_CODE.sub(lambda m: f"<code>{html.escape(m.group(1))}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{html.escape(m.group(1))}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{html.escape(m.group(1))}</em>", out)
    out = _HEADING.sub(lambda m: f"<h{len(m.group(1))}>{html.escape(m.group(2))}</h{len(m.group(1))}>", out)
    out = _UL.sub(lambda m: f"<li>{html.escape(m.group(1))}</li>", out)
    out = out.replace("\n", "<br>\n")
    return out


def sanitize_filename_display(name: str, *, max_len: int = 200) -> str:
    cleaned = html.escape(str(name or "file")[:max_len])
    return cleaned.replace("/", "_").replace("\\", "_")
