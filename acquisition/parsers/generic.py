"""Generic content parsers — HTML/JSON/XML/CSV."""

from __future__ import annotations

from acquisition.models import RECORD_DOCUMENT, RECORD_GENERIC, RawArtifact
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import (
    BaseParser,
    make_record,
    parse_csv_rows,
    parse_json_payload,
    parse_xml_root,
    strip_html,
)


class HtmlTextParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="generic.html",
        version="1.0.0",
        supported_content_types=("text/html", "application/xhtml+xml"),
        supported_record_types=(RECORD_GENERIC,),
        priority=80,
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        return "html" in ct or (artifact.content_text or "").lstrip().lower().startswith("<!doctype html")

    def parse(self, artifact: RawArtifact):
        text = strip_html(artifact.content_text or "")
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_GENERIC,
                fields={"text": text[:50_000], "url": artifact.url, "title": ""},
                confidence=0.5,
            ),
        )


class JsonParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="generic.json",
        version="1.0.0",
        supported_content_types=("application/json", "text/json"),
        supported_record_types=(RECORD_GENERIC,),
        priority=70,
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "json" in ct:
            return True
        text = (artifact.content_text or "").lstrip()
        return text.startswith("{") or text.startswith("[")

    def parse(self, artifact: RawArtifact):
        data = parse_json_payload(artifact.content_text or "")
        fields = {"json": data if isinstance(data, (dict, list)) else {"value": data}}
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_GENERIC,
                fields=fields,
                confidence=0.7,
            ),
        )


class XmlParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="generic.xml",
        version="1.0.0",
        supported_content_types=("application/xml", "text/xml"),
        supported_record_types=(RECORD_GENERIC,),
        priority=75,
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "xml" in ct and "html" not in ct:
            return True
        text = (artifact.content_text or "").lstrip()
        return text.startswith("<?xml") or (text.startswith("<") and "html" not in text[:40].lower())

    def parse(self, artifact: RawArtifact):
        root = parse_xml_root(artifact.content_text or "")
        fields = {
            "root_tag": root.tag,
            "child_count": len(list(root)),
            "text_preview": (root.text or "")[:2000],
        }
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_GENERIC,
                fields=fields,
                confidence=0.6,
            ),
        )


class CsvTableParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="generic.csv",
        version="1.0.0",
        supported_content_types=("text/csv", "application/csv", "text/plain"),
        supported_record_types=(RECORD_GENERIC, RECORD_DOCUMENT),
        priority=60,
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "csv" in ct:
            return True
        if "json" in ct or "html" in ct or "xml" in ct:
            return False
        text = artifact.content_text or ""
        return "," in text.splitlines()[0] if text.strip() else False

    def parse(self, artifact: RawArtifact):
        rows = parse_csv_rows(artifact.content_text or "")
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_DOCUMENT,
                fields={"row_count": len(rows), "columns": list(rows[0].keys()) if rows else []},
                confidence=0.7,
            ),
        )
