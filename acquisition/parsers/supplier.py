"""Supplier feed parser — canonical supplier item records."""

from __future__ import annotations

from acquisition.models import RECORD_SUPPLIER_ITEM, RawArtifact, utc_now
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record, map_price_fields, parse_csv_rows, parse_json_payload


class SupplierFeedParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="supplier.feed",
        version="1.0.0",
        supported_content_types=(
            "text/csv",
            "application/csv",
            "application/json",
            "text/plain",
        ),
        supported_record_types=(RECORD_SUPPLIER_ITEM,),
        priority=25,
        source_types=("supplier", "feed"),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "json" in ct or "xml" in ct or "html" in ct:
            # JSON handled by supplier JSON branch only when hinted
            meta = dict(artifact.metadata or {})
            if meta.get("record_hint") == "supplier_item" and "json" in ct:
                return True
            return False
        meta = dict(artifact.metadata or {})
        if meta.get("record_hint") == "supplier_item":
            return True
        text = (artifact.content_text or "").lower()
        header = text.splitlines()[0] if text.splitlines() else ""
        return "supplier" in header or "lead_time" in header or "moq" in header

    def parse(self, artifact: RawArtifact):
        text = artifact.content_text or ""
        ct = (artifact.content_type or "").lower()
        rows: list[dict] = []
        if "json" in ct or text.lstrip().startswith(("{", "[")):
            data = parse_json_payload(text)
            if isinstance(data, dict) and "items" in data:
                data = data["items"]
            if isinstance(data, list):
                rows = [{str(k).lower(): v for k, v in dict(item).items()} for item in data if isinstance(item, dict)]
        else:
            rows = parse_csv_rows(text)

        stamp = utc_now().isoformat()
        out = []
        for idx, row in enumerate(rows[:5_000]):
            fields = map_price_fields({str(k): str(v) for k, v in row.items()})
            # supplier-specific extras
            for key in ("lead_time", "terms", "supplier_id", "warehouse"):
                if key in row and row[key] not in ("", None):
                    fields[key] = row[key]
            fields.setdefault("supplier_id", artifact.source_id)
            fields["source_timestamp"] = stamp
            if not fields.get("sku") and not fields.get("name"):
                continue
            out.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_SUPPLIER_ITEM,
                    fields=fields,
                    confidence=0.85,
                    raw_field_refs={"row": idx},
                )
            )
        return tuple(out)
