"""Pagination strategies for scrape pipelines — bounded, terminate on repeated cursor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

STRATEGY_NEXT_LINK = "next_link"
STRATEGY_PAGE = "page"
STRATEGY_CURSOR = "cursor"


@dataclass(frozen=True)
class PaginationState:
    page: int = 1
    cursor: str = ""
    next_url: str = ""
    records_seen: int = 0
    pages_seen: int = 0
    done: bool = False
    reason: str = ""


def extract_next_link(html: str, *, selector_hint: str = "") -> str:
    """Best-effort next-link extraction without a full DOM engine."""
    text = html or ""
    patterns = [
        r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']next["\']',
        r'<a[^>]+class=["\'][^"\']*next[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    _ = selector_hint
    return ""


def page_url(base: str, page: int, *, param: str = "page") -> str:
    parsed = urlparse(base)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != param]
    pairs.append((param, str(page)))
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(pairs), "")
    )


def cursor_url(base: str, cursor: str, *, param: str = "cursor") -> str:
    parsed = urlparse(base)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != param]
    if cursor:
        pairs.append((param, cursor))
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(pairs), "")
    )


def extract_cursor(payload: dict | str, *, field: str = "next_cursor") -> str:
    if isinstance(payload, dict):
        return str(payload.get(field) or payload.get("cursor") or "")
    text = str(payload or "")
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


class PaginationController:
    def __init__(
        self,
        *,
        strategy: str = STRATEGY_NEXT_LINK,
        max_pages: int = 50,
        max_records: int = 5000,
        page_param: str = "page",
        cursor_param: str = "cursor",
        cursor_field: str = "next_cursor",
        next_selector: str = "",
    ):
        self.strategy = strategy
        self.max_pages = max(1, int(max_pages))
        self.max_records = max(1, int(max_records))
        self.page_param = page_param
        self.cursor_param = cursor_param
        self.cursor_field = cursor_field
        self.next_selector = next_selector
        self._seen_cursors: set[str] = set()

    def initial(self, seed_url: str) -> PaginationState:
        return PaginationState(page=1, next_url=seed_url, pages_seen=0)

    def advance(
        self,
        state: PaginationState,
        *,
        body: str = "",
        record_count: int = 0,
        payload: dict | None = None,
    ) -> PaginationState:
        pages = state.pages_seen + 1
        records = state.records_seen + max(0, int(record_count))
        if pages >= self.max_pages:
            return PaginationState(
                page=state.page,
                cursor=state.cursor,
                next_url="",
                records_seen=records,
                pages_seen=pages,
                done=True,
                reason="max_pages",
            )
        if records >= self.max_records:
            return PaginationState(
                page=state.page,
                cursor=state.cursor,
                next_url="",
                records_seen=records,
                pages_seen=pages,
                done=True,
                reason="max_records",
            )

        if self.strategy == STRATEGY_PAGE:
            nxt_page = state.page + 1
            return PaginationState(
                page=nxt_page,
                next_url=page_url(state.next_url or "", nxt_page, param=self.page_param),
                records_seen=records,
                pages_seen=pages,
            )

        if self.strategy == STRATEGY_CURSOR:
            cursor = extract_cursor(payload if payload is not None else body, field=self.cursor_field)
            if not cursor:
                return PaginationState(
                    page=state.page,
                    cursor="",
                    next_url="",
                    records_seen=records,
                    pages_seen=pages,
                    done=True,
                    reason="cursor_exhausted",
                )
            if cursor in self._seen_cursors:
                return PaginationState(
                    page=state.page,
                    cursor=cursor,
                    next_url="",
                    records_seen=records,
                    pages_seen=pages,
                    done=True,
                    reason="repeated_cursor",
                )
            self._seen_cursors.add(cursor)
            base = state.next_url or ""
            return PaginationState(
                page=state.page + 1,
                cursor=cursor,
                next_url=cursor_url(base, cursor, param=self.cursor_param),
                records_seen=records,
                pages_seen=pages,
            )

        # next_link default
        href = extract_next_link(body, selector_hint=self.next_selector)
        if not href:
            return PaginationState(
                page=state.page,
                next_url="",
                records_seen=records,
                pages_seen=pages,
                done=True,
                reason="no_next_link",
            )
        from urllib.parse import urljoin

        absolute = urljoin(state.next_url or "", href)
        if absolute == state.next_url:
            return PaginationState(
                page=state.page,
                next_url="",
                records_seen=records,
                pages_seen=pages,
                done=True,
                reason="repeated_cursor",
            )
        return PaginationState(
            page=state.page + 1,
            next_url=absolute,
            records_seen=records,
            pages_seen=pages,
        )
