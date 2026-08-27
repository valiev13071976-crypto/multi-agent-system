"""Document bridge — reuse DocumentService parse path as acquisition records."""

from __future__ import annotations

from acquisition.models import RECORD_DOCUMENT, RawArtifact
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record


class DocumentBridgeParser(BaseParser):
    """Maps document artifacts (PDF/DOCX/XLSX refs) into acquisition records."""

    descriptor = AcquisitionParserDescriptor(
        parser_id="document.bridge",
        version="1.0.0",
        supported_content_types=(
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/x-document-ref",
        ),
        supported_record_types=(RECORD_DOCUMENT,),
        priority=90,
        source_types=("document",),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        if artifact.document_id:
            return True
        ct = (artifact.content_type or "").lower()
        return any(x in ct for x in ("pdf", "wordprocessingml", "spreadsheetml", "document-ref"))

    def parse(self, artifact: RawArtifact):
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
