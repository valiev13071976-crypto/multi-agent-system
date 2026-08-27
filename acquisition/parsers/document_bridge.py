"""Document bridge — reuse DocumentService / Document Intelligence as acquisition records."""

from __future__ import annotations

from acquisition.models import RECORD_DOCUMENT, RECORD_SUPPLIER_ITEM, RawArtifact
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record


class DocumentBridgeParser(BaseParser):
    """Maps document artifacts (PDF/DOCX/XLSX refs) into acquisition records.

    When artifact metadata includes structured price_list fields (from Document
    Intelligence), emit supplier_item records without duplicating ingest.
    """

    descriptor = AcquisitionParserDescriptor(
        parser_id="document.bridge",
        version="1.1.0",
        supported_content_types=(
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/x-document-ref",
            "text/csv",
            "application/json",
        ),
        supported_record_types=(RECORD_DOCUMENT, RECORD_SUPPLIER_ITEM),
        priority=90,
        source_types=("document",),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        if artifact.document_id:
            return True
        ct = (artifact.content_type or "").lower()
        meta = dict(artifact.metadata or {})
        if meta.get("record_hint") in {"supplier_item", "price_list"}:
            return True
        return any(
            x in ct
            for x in (
                "pdf",
                "wordprocessingml",
                "spreadsheetml",
                "document-ref",
                "csv",
                "json",
            )
        )

    def parse(self, artifact: RawArtifact):
        meta = dict(artifact.metadata or {})
        hint = meta.get("record_hint") or ""
        records = []

        # Structured price list CSV from Document Intelligence bridge
        if hint in {"supplier_item", "price_list"} and (artifact.content_text or "").strip():
            lines = [ln for ln in artifact.content_text.splitlines() if ln.strip()]
            header = [h.strip().lower() for h in lines[0].split(",")] if lines else []
            body = lines[1:] if len(lines) > 1 else []
            for line in body[:500]:
                cols = [c.strip() for c in line.split(",")]
                row = {header[i]: cols[i] if i < len(cols) else "" for i in range(len(header))}
                if not any(row.values()):
                    continue
                records.append(
                    make_record(
                        artifact=artifact,
                        parser_id=self.descriptor.parser_id,
                        parser_version=self.descriptor.version,
                        record_type=RECORD_SUPPLIER_ITEM,
                        fields={
                            "sku": row.get("sku") or row.get("article") or "",
                            "ean": row.get("ean") or "",
                            "name": row.get("name") or row.get("title") or "",
                            "price": row.get("price") or "",
                            "currency": row.get("currency") or "",
                            "stock": row.get("stock") or "",
                            "document_id": artifact.document_id,
                        },
                        confidence=0.8,
                    )
                )
            if records:
                return tuple(records)

        fields = {
            "document_id": artifact.document_id,
            "content_type": artifact.content_type,
            "content_ref": artifact.content_ref,
            "checksum": artifact.checksum,
            "url": artifact.url,
            "preview": (artifact.content_text or "")[:2000],
        }
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_DOCUMENT,
                fields=fields,
                confidence=0.75,
            ),
        )
