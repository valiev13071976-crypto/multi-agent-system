"""Bridge: Block 5.2 Data Acquisition -> Content Intelligence research evidence.

Block 5.3 activation wires the "Search -> Acquisition -> Research -> Content
generation -> Review -> Artifact" chain by letting ``content.research`` pull
evidence directly from public web pages through the existing, already-hardened
Block 5.2 platform (``scrape.extract`` / ``AcquisitionToolAdapter``) instead of
requiring the caller to pre-fetch and format evidence rows manually.

Acquired page content is untrusted DATA throughout this bridge: it is mapped
into a single ``extracted_claim`` string per record and handed to
``content_intel.research.normalize_evidence_rows`` (which already screens for
prompt-injection markers -- see ``_POISON_MARKERS``). It is never interpreted
as an instruction, never executed, and never changes policy/routing here.
"""

from __future__ import annotations

import uuid

MAX_CLAIM_CHARS = 600
MAX_ROWS_PER_PAGE = 20


def _claim_from_record(record: dict) -> str:
    parts: list[str] = []
    title = str(record.get("title") or "").strip()
    if title:
        parts.append(title)
    price = record.get("price")
    if price not in (None, ""):
        parts.append(f"price: {price}")
    text = str(record.get("text") or record.get("description") or record.get("body") or "").strip()
    if text:
        parts.append(text)
    claim = " | ".join(parts) if parts else str(record.get("url") or "").strip()
    return claim[:MAX_CLAIM_CHARS]


def evidence_rows_from_scrape_result(result: dict, *, url: str) -> list[dict]:
    """Map a ``scrape.extract`` tool payload into ``content.research`` rows.

    ``result`` is the ``data`` dict of a successful ``scrape.extract``
    :class:`~tools.models.ToolResult` (see
    ``acquisition/tools.py:AcquisitionToolAdapter``), which carries a bounded
    ``records_preview`` list. Records without any extractable text/title are
    skipped rather than emitting an empty claim.
    """

    records = list((result or {}).get("records_preview") or [])[:MAX_ROWS_PER_PAGE]
    rows: list[dict] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        claim = _claim_from_record(raw)
        if not claim:
            continue
        rows.append(
            {
                "extracted_claim": claim,
                "source_type": "web",
                "source_ref": str(raw.get("url") or url or "unknown"),
                "label": str(raw.get("title") or "web_page")[:120],
                "trust_level": "unverified_external",
                "relevance": 0.5,
            }
        )
    return rows


async def fetch_scrape_extract(gateway, *, tenant_id: str, url: str, workflow_id: str = "content_intel"):
    """Invoke the governed ``scrape.extract`` tool for a single URL.

    Mirrors ``acquisition/web_source.py``'s ``_invoke_scrape_fetch`` pattern:
    a fresh, narrowly-scoped :class:`CapabilitySet` is minted for this one
    call rather than reusing/broadening any caller-held capability set.
    """

    from autonomy.capabilities import CAP_SCRAPE, CapabilitySet
    from autonomy.models import utc_now as _utc_now
    from tools.models import ToolRequest

    caps = (CAP_SCRAPE,)
    request = ToolRequest(
        request_id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        task_id="content_research",
        tool_id="scrape.extract",
        operation="extract",
        arguments={"url": url},
        tenant_id=tenant_id,
        requested_capabilities=caps,
    )
    return await gateway.invoke(
        request,
        capabilities=CapabilitySet(subject_id="content_intel", capabilities=caps, issued_at=_utc_now()),
    )
