"""Versioned scraping profiles — static vs browser dispatch (browser write OOS)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata

DISPATCH_STATIC = "static"
DISPATCH_BROWSER = "browser"
DISPATCH_MODES = (DISPATCH_STATIC, DISPATCH_BROWSER)


@dataclass(frozen=True)
class ScrapingProfile:
    profile_id: str
    version: str
    dispatch: str = DISPATCH_STATIC
    selectors: Mapping[str, object] = field(default_factory=dict)
    pagination: Mapping[str, object] = field(default_factory=dict)
    max_pages: int = 50
    max_records: int = 5000
    allowed_content_types: tuple[str, ...] = ("text/html", "application/json")
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.dispatch not in DISPATCH_MODES:
            raise ValueError(f"invalid_dispatch:{self.dispatch}")
        if not str(self.profile_id or "").strip():
            raise ValueError("profile_id_required")
        if not str(self.version or "").strip():
            raise ValueError("version_required")
        object.__setattr__(
            self,
            "selectors",
            MappingProxyType(sanitize_metadata(dict(self.selectors or {}))),
        )
        object.__setattr__(
            self,
            "pagination",
            MappingProxyType(sanitize_metadata(dict(self.pagination or {}))),
        )
        object.__setattr__(
            self,
            "allowed_content_types",
            tuple(self.allowed_content_types or ()),
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(sanitize_metadata(dict(self.metadata or {}))),
        )

    @property
    def requires_browser(self) -> bool:
        return self.dispatch == DISPATCH_BROWSER


DEFAULT_STATIC_PROFILE = ScrapingProfile(
    profile_id="static.generic",
    version="1.0.0",
    dispatch=DISPATCH_STATIC,
    pagination={"strategy": "next_link", "next_selector": "a[rel=next], a.next"},
)
