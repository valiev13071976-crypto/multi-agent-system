"""Canonical acquisition source categories — extensible, not business-specific."""

from __future__ import annotations

from acquisition.models import (
    ACQ_BROWSER,
    ACQ_CRAWL,
    ACQ_FEED,
    ACQ_HTTP_GET,
    ACQ_SEARCH,
    SOURCE_API,
    SOURCE_DOCUMENT,
    SOURCE_FEED,
    SOURCE_SEARCH,
    SOURCE_WEBSITE,
)

# Canonical platform source categories (§4)
SOURCE_CATEGORY_WEB_URL = "WEB_URL"
SOURCE_CATEGORY_WEB_SITE = "WEB_SITE"
SOURCE_CATEGORY_SEARCH_RESULT = "SEARCH_RESULT"
SOURCE_CATEGORY_BROWSER_PAGE = "BROWSER_PAGE"
SOURCE_CATEGORY_REST_API = "REST_API"
SOURCE_CATEGORY_JSON_API = "JSON_API"
SOURCE_CATEGORY_FILE = "FILE"
SOURCE_CATEGORY_FEED = "FEED"
SOURCE_CATEGORY_SITEMAP = "SITEMAP"
SOURCE_CATEGORY_RSS = "RSS"
SOURCE_CATEGORY_ATOM = "ATOM"

SOURCE_CATEGORIES = (
    SOURCE_CATEGORY_WEB_URL,
    SOURCE_CATEGORY_WEB_SITE,
    SOURCE_CATEGORY_SEARCH_RESULT,
    SOURCE_CATEGORY_BROWSER_PAGE,
    SOURCE_CATEGORY_REST_API,
    SOURCE_CATEGORY_JSON_API,
    SOURCE_CATEGORY_FILE,
    SOURCE_CATEGORY_FEED,
    SOURCE_CATEGORY_SITEMAP,
    SOURCE_CATEGORY_RSS,
    SOURCE_CATEGORY_ATOM,
)

# Map category → default registry source_type + acquisition_type hints
CATEGORY_DEFAULTS: dict[str, dict[str, str]] = {
    SOURCE_CATEGORY_WEB_URL: {"source_type": SOURCE_WEBSITE, "acquisition_type": ACQ_HTTP_GET},
    SOURCE_CATEGORY_WEB_SITE: {"source_type": SOURCE_WEBSITE, "acquisition_type": ACQ_CRAWL},
    SOURCE_CATEGORY_SEARCH_RESULT: {"source_type": SOURCE_SEARCH, "acquisition_type": ACQ_SEARCH},
    SOURCE_CATEGORY_BROWSER_PAGE: {"source_type": SOURCE_WEBSITE, "acquisition_type": ACQ_BROWSER},
    SOURCE_CATEGORY_REST_API: {"source_type": SOURCE_API, "acquisition_type": ACQ_HTTP_GET},
    SOURCE_CATEGORY_JSON_API: {"source_type": SOURCE_API, "acquisition_type": ACQ_HTTP_GET},
    SOURCE_CATEGORY_FILE: {"source_type": SOURCE_DOCUMENT, "acquisition_type": ACQ_HTTP_GET},
    SOURCE_CATEGORY_FEED: {"source_type": SOURCE_FEED, "acquisition_type": ACQ_FEED},
    SOURCE_CATEGORY_SITEMAP: {"source_type": SOURCE_WEBSITE, "acquisition_type": ACQ_CRAWL},
    SOURCE_CATEGORY_RSS: {"source_type": SOURCE_FEED, "acquisition_type": ACQ_FEED},
    SOURCE_CATEGORY_ATOM: {"source_type": SOURCE_FEED, "acquisition_type": ACQ_FEED},
}


def defaults_for_category(category: str) -> dict[str, str]:
    if category not in SOURCE_CATEGORIES:
        raise ValueError(f"invalid_source_category:{category}")
    return dict(CATEGORY_DEFAULTS[category])
