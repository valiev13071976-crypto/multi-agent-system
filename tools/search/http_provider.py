from tools.models import SearchResult


class SearchUnavailableError(RuntimeError):
    pass


class UnconfiguredHttpSearchProvider:
    """
    Placeholder HTTP adapter.

    No paid vendor is hardcoded. Wire a real provider in a later patch
    via SEARCH_PROVIDER config + SecretStore.
    """

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchUnavailableError("search_provider_not_configured")
