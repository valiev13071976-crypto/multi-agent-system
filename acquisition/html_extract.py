"""Deterministic HTML parsing / structured extraction (Block 5.2).

Uses ``lxml`` (a maintained, minimal HTML parser) as the single source of
truth for DOM traversal -- never regex-as-primary-parser. All functions are
pure/deterministic: same HTML in -> same structured output out. Malformed
HTML never crashes the caller (lxml's HTML parser is lenient by design; any
residual failure degrades to an empty/partial result, never an exception
that could take down a worker).

Nothing here performs network I/O, LLM calls, or executes page script
content -- fetched HTML is treated purely as untrusted markup/text data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from lxml import etree, html as lhtml

MAX_HTML_BYTES = 5_000_000
MAX_TEXT_CHARS = 20_000
MAX_LINKS = 500
MAX_TABLE_ROWS = 2_000
MAX_CARDS = 500
MAX_HEADINGS = 100

_PRICE_RE = re.compile(r"\d+(?:[ \u00a0.,]\d{3})*(?:[.,]\d{1,2})?")
_PRICE_CLASS_RE = re.compile(r"price|цена|cost|amount", re.I)
_OLD_PRICE_CLASS_RE = re.compile(
    r"old[-_]?price|was[-_]?price|regular[-_]?price|crossed|стар(ая)?[-_ ]?цена", re.I
)
_TITLE_CLASS_RE = re.compile(r"title|name|heading|product[-_]?name|наименован|назван", re.I)
_CARD_HINT_RE = re.compile(
    r"product|item|card|listing|goods|товар|карточк", re.I
)
_SCRIPT_STYLE_TAGS = {"script", "style", "noscript", "template", "svg"}


def _safe_parse(html_text: str):
    """Parse HTML defensively; never raise on malformed markup."""
    text = str(html_text or "")[:MAX_HTML_BYTES]
    if not text.strip():
        return None
    try:
        parser = lhtml.HTMLParser(recover=True, remove_comments=True, encoding="utf-8")
        return lhtml.fromstring(text.encode("utf-8", errors="replace"), parser=parser)
    except Exception:
        try:
            return lhtml.fromstring(text)
        except Exception:
            return None


def parse_html(html_text: str):
    """Public entry point: parse HTML into an lxml tree or ``None``."""
    return _safe_parse(html_text)


def _text_of(el) -> str:
    if el is None:
        return ""
    try:
        return " ".join(el.itertext())
    except Exception:
        return ""


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_noise(tree) -> None:
    if tree is None:
        return
    for tag in _SCRIPT_STYLE_TAGS:
        for el in tree.iter(tag):
            el.getparent() is not None and el.getparent().remove(el)


@dataclass(frozen=True)
class PageMetadata:
    title: str = ""
    description: str = ""
    canonical_url: str = ""
    headings: tuple[str, ...] = ()
    links: tuple[dict, ...] = field(default_factory=tuple)
    images: tuple[dict, ...] = field(default_factory=tuple)


def extract_metadata(tree, *, base_url: str = "") -> PageMetadata:
    """Extract title/meta description/canonical/headings/links/images."""
    if tree is None:
        return PageMetadata()
    title = ""
    title_el = tree.find(".//title")
    if title_el is not None:
        title = _clean_text(_text_of(title_el))
    description = ""
    for meta in tree.findall(".//meta"):
        name = (meta.get("name") or meta.get("property") or "").strip().lower()
        if name in {"description", "og:description"} and not description:
            description = _clean_text(meta.get("content") or "")
    canonical = ""
    for link_el in tree.findall(".//link"):
        rel = (link_el.get("rel") or "").strip().lower()
        if rel == "canonical":
            href = link_el.get("href") or ""
            canonical = urljoin(base_url, href) if base_url else href
            break
    headings = []
    for level in range(1, 4):
        for h in tree.findall(f".//h{level}"):
            text = _clean_text(_text_of(h))
            if text:
                headings.append(text)
            if len(headings) >= MAX_HEADINGS:
                break
    links: list[dict] = []
    seen_links: set[str] = set()
    for a in tree.findall(".//a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        if absolute in seen_links:
            continue
        seen_links.add(absolute)
        links.append({"url": absolute, "text": _clean_text(_text_of(a))[:200]})
        if len(links) >= MAX_LINKS:
            break
    images: list[dict] = []
    for img in tree.findall(".//img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        absolute = urljoin(base_url, src) if base_url else src
        images.append({"url": absolute, "alt": _clean_text(img.get("alt") or "")[:200]})
        if len(images) >= MAX_LINKS:
            break
    return PageMetadata(
        title=title,
        description=description,
        canonical_url=canonical,
        headings=tuple(headings),
        links=tuple(links),
        images=tuple(images),
    )


def extract_main_text(tree, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Bounded, boilerplate-reduced main-content text extraction."""
    if tree is None:
        return ""
    work = lhtml.fromstring(etree.tostring(tree)) if tree is not None else None
    if work is None:
        return ""
    for tag in ("script", "style", "noscript", "nav", "footer", "header", "template", "svg"):
        for el in work.findall(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    text = _clean_text(_text_of(work))
    return text[:max_chars]


@dataclass(frozen=True)
class ExtractedTable:
    index: int
    headers: tuple[str, ...]
    rows: tuple[dict, ...]


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        key = h or "column"
        if key in seen:
            seen[key] += 1
            out.append(f"{key}_{seen[key]}")
        else:
            seen[key] = 0
            out.append(key)
    return out


def extract_tables(tree, *, base_url: str = "", max_tables: int = 10) -> tuple[ExtractedTable, ...]:
    """Deterministic HTML table extraction with conservative colspan handling."""
    if tree is None:
        return ()
    out: list[ExtractedTable] = []
    for idx, table_el in enumerate(tree.findall(".//table")[:max_tables]):
        rows_el = table_el.findall(".//tr")
        if not rows_el:
            continue
        header_cells = rows_el[0].findall("./th")
        body_start = 1
        if header_cells:
            headers = [_clean_text(_text_of(c)) or f"column_{i + 1}" for i, c in enumerate(header_cells)]
        else:
            first_row_cells = rows_el[0].findall("./td")
            if first_row_cells and all(
                c.tag == "th" for c in rows_el[0].iterchildren() if c.tag in ("td", "th")
            ):
                headers = [_clean_text(_text_of(c)) or f"column_{i + 1}" for i, c in enumerate(first_row_cells)]
            else:
                # No <th> row at all — deterministic fallback column names,
                # never invented from cell content.
                width = len(rows_el[0].findall("./td") or rows_el[0].findall("./th"))
                headers = [f"column_{i + 1}" for i in range(width)]
                body_start = 0
        headers = _dedupe_headers(headers)
        rows: list[dict] = []
        for row_el in rows_el[body_start:]:
            cells = row_el.findall("./td") or row_el.findall("./th")
            if not cells:
                continue
            values = [_clean_text(_text_of(c)) for c in cells]
            row = {}
            for i, hname in enumerate(headers):
                row[hname] = values[i] if i < len(values) else ""
            if any(v for v in row.values()):
                rows.append(row)
            if len(rows) >= MAX_TABLE_ROWS:
                break
        out.append(ExtractedTable(index=idx, headers=tuple(headers), rows=tuple(rows)))
    return tuple(out)


def _class_tokens(el) -> tuple[str, ...]:
    return tuple((el.get("class") or "").split())


def _repeated_groups(tree):
    """Group sibling elements by (tag, sorted classes) signature."""
    groups: dict[tuple, list] = {}
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        classes = _class_tokens(el)
        if not classes:
            continue
        sig = (el.tag, tuple(sorted(classes)))
        groups.setdefault(sig, []).append(el)
    return groups


def _looks_like_card_container(elements: list) -> bool:
    if len(elements) < 2:
        return False
    with_link = sum(1 for e in elements if e.findall(".//a"))
    return with_link >= max(2, int(len(elements) * 0.6))


def _price_in(el) -> str | None:
    # Always return just the matched numeric substring (never the surrounding
    # currency-symbol/text) so downstream deterministic normalization
    # (``acquisition.normalize.normalize_number``) can parse it losslessly —
    # returning the full label text here would make normalization treat the
    # price as EXTRACT_INVALID and silently drop it.
    for child in el.iter():
        classes = " ".join(_class_tokens(child))
        if _PRICE_CLASS_RE.search(classes) and not _OLD_PRICE_CLASS_RE.search(classes):
            text = _clean_text(_text_of(child))
            m = _PRICE_RE.search(text)
            if m:
                return m.group(0)
    text = _clean_text(_text_of(el))
    m = _PRICE_RE.search(text)
    return m.group(0) if m else None


def _title_in(el) -> str:
    for child in el.iter():
        classes = " ".join(_class_tokens(child))
        if _TITLE_CLASS_RE.search(classes):
            text = _clean_text(_text_of(child))
            if text:
                return text[:300]
    for level in range(1, 5):
        h = el.find(f".//h{level}")
        if h is not None:
            text = _clean_text(_text_of(h))
            if text:
                return text[:300]
    a = el.find(".//a")
    if a is not None:
        text = _clean_text(_text_of(a))
        if text:
            return text[:300]
    return _clean_text(_text_of(el))[:300]


def _url_in(el, *, base_url: str) -> str:
    a = el.find(".//a")
    if a is None:
        return ""
    href = (a.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return ""
    return urljoin(base_url, href) if base_url else href


def extract_cards(tree, *, base_url: str = "", max_cards: int = MAX_CARDS) -> tuple[dict, ...]:
    """Deterministic repeated-item ("card") extraction via structural heuristics.

    Groups sibling elements sharing an identical (tag, class-set) signature;
    the largest group that structurally looks like a list of items (>=2
    elements, most containing a link) is treated as the record container.
    Never fabricates records for pages without a repeated structure (returns
    an empty tuple).
    """
    if tree is None:
        return ()
    groups = _repeated_groups(tree)
    candidates = [
        (sig, elements)
        for sig, elements in groups.items()
        if len(elements) >= 2 and _looks_like_card_container(elements)
    ]
    if not candidates:
        return ()

    def _score(item) -> tuple:
        _, elements = item
        hinted = 1 if _CARD_HINT_RE.search(" ".join(elements[0].get("class") or "")) else 0
        return (hinted, len(elements))

    candidates.sort(key=_score, reverse=True)
    # Prefer the deepest matching container (most specific class signature)
    # among equally-sized top candidates to avoid picking an outer wrapper.
    best_sig, best_elements = candidates[0]
    best_count = len(best_elements)
    deepest = max(
        (c for c in candidates if len(c[1]) == best_count),
        key=lambda c: _element_depth(c[1][0]),
    )
    _, best_elements = deepest

    out: list[dict] = []
    seen_urls: set[str] = set()
    for el in best_elements[:max_cards]:
        title = _title_in(el)
        price_text = _price_in(el)
        url = _url_in(el, base_url=base_url)
        if not title and not price_text and not url:
            continue
        record: dict = {"title": title}
        if price_text:
            record["price"] = price_text
        if url:
            if url in seen_urls:
                # Keep the record (still a distinct DOM item) but do not
                # silently fabricate a fake unique URL.
                pass
            seen_urls.add(url)
            record["url"] = url
        out.append(record)
    return tuple(out)


def _element_depth(el) -> int:
    depth = 0
    node = el
    while node is not None and node.getparent() is not None:
        depth += 1
        node = node.getparent()
    return depth


@dataclass(frozen=True)
class PriceAmbiguity:
    ambiguous: bool
    values: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


def detect_price_ambiguity(tree) -> PriceAmbiguity:
    """Detect multiple, differently-classed, differently-valued prices on a
    single (non-list) page -- e.g. retail vs. old vs. wholesale price."""
    if tree is None:
        return PriceAmbiguity(ambiguous=False)
    found: dict[str, str] = {}
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        classes = " ".join(_class_tokens(el))
        if not classes or not _PRICE_CLASS_RE.search(classes):
            continue
        text = _clean_text(_text_of(el))
        m = _PRICE_RE.search(text)
        if not m:
            continue
        label = classes.strip().split()[0]
        found.setdefault(label, m.group(0))
    distinct_values = {v for v in found.values()}
    if len(found) >= 2 and len(distinct_values) >= 2:
        return PriceAmbiguity(
            ambiguous=True,
            values=tuple(found.values()),
            labels=tuple(found.keys()),
        )
    return PriceAmbiguity(ambiguous=False)


def resolve_pagination_next(tree, *, base_url: str) -> str:
    """Return the absolute URL of a deterministic 'next page' link, if any."""
    if tree is None:
        return ""
    for a in tree.findall(".//a"):
        rel = (a.get("rel") or "").strip().lower()
        classes = " ".join(_class_tokens(a)).lower()
        text = _clean_text(_text_of(a)).lower()
        if rel == "next" or "next" in classes or text in {"next", "далее", "вперед", "вперёд", ">"}:
            href = (a.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:")):
                return urljoin(base_url, href) if base_url else href
    return ""


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
