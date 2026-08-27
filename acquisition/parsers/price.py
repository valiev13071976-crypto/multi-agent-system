"""Price-list parser foundation — parsing only, no retail pricing."""

from __future__ import annotations

from acquisition.models import RECORD_PRICE, RawArtifact, utc_now
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record, map_price_fields, parse_csv_rows


_PRICE_HINTS = ("price", "цена", "sku", "ean", "артикул", "stock", "наличие")


class PriceListCsvParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="price.csv",
        version="1.0.0",
        supported_content_types=("text/csv", "application/csv", "text/plain"),
        supported_record_types=(RECORD_PRICE,),
        priority=20,
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "json" in ct or "xml" in ct or "html" in ct:
            return False
        text = (artifact.content_text or "").lower()
        if not text.strip():
            return False
        header = text.splitlines()[0] if text.splitlines() else ""
        hits = sum(1 for h in _PRICE_HINTS if h in header)
        return hits >= 2 and ("csv" in ct or "text/plain" in ct or ";" in header or header.count(",") >= 2)

    def parse(self, artifact: RawArtifact):
        rows = parse_csv_rows(artifact.content_text or "")
        records = []
        stamp = utc_now().isoformat()
        for idx, row in enumerate(rows[:5_000]):
            fields = map_price_fields(row)
            if not fields:
                continue
            fields["source_row"] = idx
            fields["timestamp"] = stamp
            records.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_PRICE,
                    fields=fields,
                    confidence=0.85,
                    raw_field_refs={"row": idx, "keys": list(row.keys())},
                )
            )
        return tuple(records)
