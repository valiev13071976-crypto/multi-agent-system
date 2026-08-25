from datetime import datetime, timezone

from tools.models import (
    TRUST_LOW,
    TRUST_MEDIUM,
    TRUST_UNKNOWN,
    SearchResult,
)
from tools.url_safety import is_safe_http_url, source_domain


class FakeSearchProvider:
    """Deterministic in-memory adapter for tests. No network."""

    def __init__(self, results_by_query: dict[str, list[SearchResult]] | None = None):
        self.results_by_query = results_by_query or {}
        self.queries: list[str] = []
        self.delay_seconds = 0.0
        self.error = None

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import asyncio

        self.queries.append(query)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        matched = []
        for key, rows in self.results_by_query.items():
            if key.casefold() in query.casefold() or query.casefold() in key.casefold():
                matched.extend(rows)
        return list(matched[:max_results])


def fake_result(
    url: str,
    *,
    title: str = "Result",
    snippet: str = "",
    trust_level: str = TRUST_MEDIUM,
    retrieved_at: datetime | None = None,
) -> SearchResult:
    domain = source_domain(url) if is_safe_http_url(url) else ""
    return SearchResult(
        title=title,
        url=url,
        snippet=snippet,
        source_domain=domain,
        published_at=None,
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        trust_level=trust_level if domain else TRUST_LOW if trust_level == TRUST_UNKNOWN else trust_level,
    )
