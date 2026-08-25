import asyncio
from datetime import datetime, timezone

from security.redaction import redact
from tools.models import (
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    MAX_SEARCH_RESULTS_PER_CLAIM,
    MAX_TOTAL_SEARCH_RESULTS,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    SearchResult,
    ToolUsageRecord,
)
from tools.search.http_provider import SearchUnavailableError
from tools.search.null_provider import NullSearchProvider
from tools.models import SearchResult
from tools.trust import trust_for_domain
from tools.url_safety import UnsafeUrlError, is_safe_http_url, source_domain, validate_http_url


class SearchTimeoutError(TimeoutError):
    pass


class ToolGateway:
    """
    Single external access point for read-only search.
    """

    def __init__(
        self,
        search_provider=None,
        *,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        max_results_per_call: int = MAX_SEARCH_RESULTS_PER_CLAIM,
        max_total_results: int = MAX_TOTAL_SEARCH_RESULTS,
        tool_trust_level: str = TOOL_TRUST_READ_ONLY_EXTERNAL,
        task_id: str = "",
    ):
        self._provider = search_provider or NullSearchProvider()
        self._timeout_seconds = float(timeout_seconds)
        self._max_results_per_call = int(max_results_per_call)
        self._max_total_results = int(max_total_results)
        self.tool_trust_level = tool_trust_level
        self.task_id = task_id
        self.last_usage: list[ToolUsageRecord] = []
        self._total_results = 0
        self.queries: list[str] = []

    def reset_budget(self) -> None:
        self._total_results = 0

    def remaining_results(self) -> int:
        return max(0, self._max_total_results - self._total_results)

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        if self.tool_trust_level != TOOL_TRUST_READ_ONLY_EXTERNAL:
            raise SearchUnavailableError("tool_trust_not_allowed")
        cleaned = redact(str(query or "")).strip()
        if not cleaned or cleaned == "[REDACTED]":
            return []
        remaining = self.remaining_results()
        if remaining <= 0:
            return []
        limit = min(int(max_results), self._max_results_per_call, remaining)
        if limit <= 0:
            return []
        self.queries.append(cleaned)
        started = datetime.now(timezone.utc)
        success = False
        try:
            rows = await asyncio.wait_for(
                self._provider.search(cleaned, max_results=limit),
                timeout=self._timeout_seconds,
            )
            safe = []
            for item in rows or []:
                if not isinstance(item, SearchResult):
                    continue
                if not is_safe_http_url(item.url):
                    continue
                try:
                    validate_http_url(item.url)
                except UnsafeUrlError:
                    continue
                domain = source_domain(item.url) or item.source_domain
                item = SearchResult(
                    title=item.title,
                    url=item.url,
                    snippet=item.snippet,
                    source_domain=domain,
                    published_at=item.published_at,
                    retrieved_at=item.retrieved_at,
                    trust_level=trust_for_domain(domain),
                )
                safe.append(item)
                if len(safe) >= limit:
                    break
            self._total_results += len(safe)
            success = True
            return safe
        except asyncio.TimeoutError as exc:
            raise SearchTimeoutError("external_evidence_timeout") from exc
        except SearchTimeoutError:
            raise
        except SearchUnavailableError:
            raise
        except Exception as exc:
            raise SearchUnavailableError(redact(str(exc))) from exc
        finally:
            elapsed = datetime.now(timezone.utc) - started
            self.last_usage.append(
                ToolUsageRecord(
                    tool_id="search",
                    task_id=self.task_id,
                    operation="search",
                    timestamp=started,
                    success=success,
                    latency_ms=int(elapsed.total_seconds() * 1000),
                    metadata={"query_len": len(cleaned)},
                )
            )
