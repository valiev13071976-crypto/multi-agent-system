from tools.models import SearchResult


class NullSearchProvider:
    """Read-only no-op adapter. Used when no search vendor is configured."""

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return []
