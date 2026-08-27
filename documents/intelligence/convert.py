"""Safe document conversion contracts."""

from __future__ import annotations

from documents.errors import CONVERSION_FAILED, CONVERSION_UNAVAILABLE, DocumentError
from documents.intelligence.generate import generate_docx, generate_pdf, generate_txt
from documents.intelligence.contracts import GeneratedDocument


def convert_document(
    *,
    tenant_id: str,
    source_media_type: str,
    target_format: str,
    text: str = "",
    title: str = "converted",
    backend: str = "builtin",
) -> GeneratedDocument:
    """Convert using safe built-in generators only — no LibreOffice shell."""
    target = str(target_format or "").lower().strip()
    src = str(source_media_type or "").lower()
    paragraphs = [p for p in text.split("\n\n") if p.strip()] or [text]

    if backend not in {"builtin", "internal", ""}:
        raise DocumentError(CONVERSION_UNAVAILABLE)

    try:
        if target in {"txt", "text", "md"}:
            return generate_txt(tenant_id=tenant_id, title=title, paragraphs=paragraphs)
        if target in {"docx"}:
            if "pdf" in src and not text:
                raise DocumentError(CONVERSION_UNAVAILABLE)
            return generate_docx(tenant_id=tenant_id, title=title, paragraphs=paragraphs)
        if target in {"pdf"}:
            return generate_pdf(tenant_id=tenant_id, title=title, paragraphs=paragraphs)
        if target in {"csv"} and ("csv" in src or "sheet" in src or text):
            data = text.encode("utf-8")
            from documents.intelligence.contracts import GeneratedDocument, utc_now
            from documents.models import content_hash_bytes
            import uuid
            from security.tenant import normalize_tenant_id

            return GeneratedDocument(
                document_id=str(uuid.uuid4()),
                tenant_id=normalize_tenant_id(tenant_id),
                media_type="text/csv",
                filename=f"{title}.csv",
                content=data,
                template_id="csv_passthrough",
                template_version="1.0.0",
                checksum=content_hash_bytes(data),
                provenance={"generated_at": utc_now().isoformat(), "format": "csv"},
            )
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(CONVERSION_FAILED) from exc
    raise DocumentError(CONVERSION_UNAVAILABLE)
