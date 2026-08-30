"""Scrape package — versioned profiles and pagination."""

from acquisition.scrape.pagination import PaginationController, PaginationState
from acquisition.scrape.pipeline import ScrapePipeline, ScrapeResult
from acquisition.scrape.profiles import (
    DEFAULT_STATIC_PROFILE,
    DISPATCH_BROWSER,
    DISPATCH_STATIC,
    ScrapingProfile,
)

__all__ = [
    "DEFAULT_STATIC_PROFILE",
    "DISPATCH_BROWSER",
    "DISPATCH_STATIC",
    "PaginationController",
    "PaginationState",
    "ScrapePipeline",
    "ScrapeResult",
    "ScrapingProfile",
]
