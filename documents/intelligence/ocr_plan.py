"""OCR planning — decide whether OCR is needed from native text / page stats."""

from __future__ import annotations

from typing import Mapping, Sequence

from documents.platform_models import (
    OCR_FAILED,
    OCR_NOT_REQUIRED,
    OCR_PARTIAL,
    OCR_PERFORMED,
    OCR_REQUIRED,
    OCR_UNAVAILABLE,
    OCRPlanDecision,
)

# Minimum characters of native text to treat as sufficient (per page avg or total).
MIN_NATIVE_CHARS_TOTAL = 40
MIN_NATIVE_CHARS_PER_PAGE = 20


def plan_ocr(
    *,
    content: str | None = None,
    native_text: str | None = None,
    page_stats: Sequence[Mapping[str, object]] | None = None,
    page_count: int | None = None,
    provider: str = "",
    provider_available: bool | None = None,
    ocr_already_performed: bool = False,
    ocr_partial: bool = False,
    ocr_failed: bool = False,
) -> OCRPlanDecision:
    """Return OCRPlanDecision — never OCR by default when native text is OK."""
    text = (native_text if native_text is not None else content) or ""
    text = str(text).strip()
    pages = list(page_stats or ())
    count = int(page_count) if page_count is not None else (len(pages) if pages else 0)

    if ocr_failed:
        return OCRPlanDecision(
            status=OCR_FAILED,
            reason="ocr_failed",
            page_count=count,
            provider=provider,
        )
    if ocr_already_performed and ocr_partial:
        return OCRPlanDecision(
            status=OCR_PARTIAL,
            reason="partial_pages_ocrd",
            page_count=count,
            provider=provider,
        )
    if ocr_already_performed:
        return OCRPlanDecision(
            status=OCR_PERFORMED,
            reason="ocr_completed",
            page_count=count,
            provider=provider,
        )

    # Page-level emptiness signals
    empty_pages = 0
    native_pages = 0
    for p in pages:
        chars = int(p.get("char_count") or p.get("chars") or 0)
        if chars >= MIN_NATIVE_CHARS_PER_PAGE:
            native_pages += 1
        else:
            empty_pages += 1

    if pages:
        if empty_pages == 0 and native_pages > 0:
            return OCRPlanDecision(
                status=OCR_NOT_REQUIRED,
                reason="native_text_sufficient",
                page_count=count or len(pages),
                provider=provider,
            )
        if empty_pages > 0 and native_pages > 0:
            # Mixed — some pages need OCR
            if provider_available is False:
                return OCRPlanDecision(
                    status=OCR_UNAVAILABLE,
                    reason="mixed_pages_ocr_unavailable",
                    page_count=count or len(pages),
                    provider=provider,
                )
            return OCRPlanDecision(
                status=OCR_REQUIRED,
                reason="scanned_or_empty_pages",
                page_count=count or len(pages),
                provider=provider,
            )
        # All pages empty / insufficient
        if provider_available is False:
            return OCRPlanDecision(
                status=OCR_UNAVAILABLE,
                reason="scanned_document_ocr_unavailable",
                page_count=count or len(pages),
                provider=provider,
            )
        return OCRPlanDecision(
            status=OCR_REQUIRED,
            reason="scanned_or_empty_pages",
            page_count=count or len(pages),
            provider=provider,
        )

    # No page stats — use aggregate native text
    if len(text) >= MIN_NATIVE_CHARS_TOTAL:
        return OCRPlanDecision(
            status=OCR_NOT_REQUIRED,
            reason="native_text_sufficient",
            page_count=count,
            provider=provider,
        )

    if provider_available is False:
        return OCRPlanDecision(
            status=OCR_UNAVAILABLE,
            reason="insufficient_native_text_ocr_unavailable",
            page_count=count,
            provider=provider,
        )

    return OCRPlanDecision(
        status=OCR_REQUIRED,
        reason="insufficient_native_text",
        page_count=count,
        provider=provider,
    )
