"""Scrape pipeline — profile-pinned, ToolGateway fetch, static dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from acquisition.crawler import canonicalize_url
from acquisition.errors import AcquisitionDeniedError, SourceUnavailableError
from acquisition.manager import AcquisitionManager
from acquisition.models import (
    ACQ_BROWSER,
    ACQ_HTTP_GET,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_PARTIAL,
    AcquisitionJob,
    AcquisitionRequest,
    RawArtifact,
    SourceDescriptor,
)
from acquisition.scrape.pagination import PaginationController
from acquisition.scrape.profiles import (
    DISPATCH_BROWSER,
    DEFAULT_STATIC_PROFILE,
    ScrapingProfile,
)
from acquisition.source_policy import evaluate_url


@dataclass
class ScrapeResult:
    artifacts: tuple[RawArtifact, ...]
    pages: int
    records_hint: int
    status: str
    profile_id: str
    profile_version: str
    errors: tuple[str, ...] = ()
    terminated_reason: str = ""


class ScrapePipeline:
    def __init__(self, manager: AcquisitionManager, *, fetch_fn=None):
        self.manager = manager
        self._fetch_fn = fetch_fn

    async def _fetch(
        self,
        *,
        url: str,
        source: SourceDescriptor,
        tenant_id: str,
        workflow_id: str,
        use_browser: bool,
    ) -> RawArtifact:
        if self._fetch_fn is not None:
            return await self._fetch_fn(url)
        req = AcquisitionRequest(
            source_id=source.source_id,
            target=url,
            acquisition_type=ACQ_BROWSER if use_browser else ACQ_HTTP_GET,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        return await self.manager.acquire(req)

    async def run(
        self,
        *,
        source: SourceDescriptor,
        seed_url: str,
        tenant_id: str,
        profile: ScrapingProfile | None = None,
        job: AcquisitionJob | None = None,
        workflow_id: str = "",
        cancel_check=None,
    ) -> ScrapeResult:
        profile = profile or DEFAULT_STATIC_PROFILE
        # Pin version on job metadata contract
        if job is not None and job.scrape_profile_version and job.scrape_profile_version != profile.version:
            raise AcquisitionDeniedError("scrape_profile_version_mismatch")
        if profile.requires_browser:
            # Browser write is OOS; read scaffold may still be attempted via gateway.
            pass

        decision = evaluate_url(seed_url, source=source)
        if not decision.permitted:
            raise AcquisitionDeniedError(f"scrape_seed_denied:{decision.reason}")

        pag_cfg = dict(profile.pagination or {})
        pager = PaginationController(
            strategy=str(pag_cfg.get("strategy") or "next_link"),
            max_pages=int(pag_cfg.get("max_pages") or profile.max_pages),
            max_records=int(pag_cfg.get("max_records") or profile.max_records),
            page_param=str(pag_cfg.get("page_param") or "page"),
            cursor_param=str(pag_cfg.get("cursor_param") or "cursor"),
            cursor_field=str(pag_cfg.get("cursor_field") or "next_cursor"),
            next_selector=str(pag_cfg.get("next_selector") or ""),
        )
        state = pager.initial(canonicalize_url(seed_url))
        artifacts: list[RawArtifact] = []
        errors: list[str] = []
        records_hint = 0
        status = JOB_COMPLETED
        reason = ""

        while not state.done and state.next_url:
            if cancel_check is not None and cancel_check():
                status = JOB_CANCELLED
                reason = "cancelled"
                break
            if job is not None and job.cancel_requested:
                status = JOB_CANCELLED
                reason = "cancelled"
                break
            try:
                art = await self._fetch(
                    url=state.next_url,
                    source=source,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id or (job.workflow_id if job else ""),
                    use_browser=profile.dispatch == DISPATCH_BROWSER,
                )
            except SourceUnavailableError as exc:
                if profile.dispatch == DISPATCH_BROWSER:
                    errors.append(f"{state.next_url}:browser_unavailable")
                    status = JOB_PARTIAL
                    reason = "browser_unavailable"
                    break
                raise
            except Exception as exc:
                code = getattr(exc, "error_code", type(exc).__name__)
                errors.append(f"{state.next_url}:{code}")
                status = JOB_PARTIAL
                break

            ct = (art.content_type or "").split(";")[0].strip().lower()
            if ct and not any(ct.startswith(a) for a in profile.allowed_content_types):
                errors.append(f"{state.next_url}:content_type_filtered")
                status = JOB_PARTIAL
                break

            # Oversized guard
            if int(art.content_bytes_len or 0) > 5_000_000:
                errors.append(f"{state.next_url}:content_too_large")
                status = JOB_PARTIAL
                reason = "content_too_large"
                break

            artifacts.append(art)
            # crude record hint for HTML list pages / JSON arrays
            body = art.content_text or ""
            page_records = body.count("<li") + body.count("<tr")
            if art.content_type.startswith("application/json"):
                page_records = max(page_records, body.count("{"))
            records_hint += page_records
            state = pager.advance(state, body=body, record_count=page_records)
            reason = state.reason or reason

        return ScrapeResult(
            artifacts=tuple(artifacts),
            pages=len(artifacts),
            records_hint=records_hint,
            status=status,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            errors=tuple(errors),
            terminated_reason=reason,
        )
