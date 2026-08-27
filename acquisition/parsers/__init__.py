"""Parser Registry — content-type aware, no giant if/elif chains."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from acquisition.errors import ParserFailedError, ParserNotFoundError, UnsupportedContentError
from acquisition.models import ParsedRecord, RawArtifact, ValidationResult
from acquisition.validation import validate_record


@dataclass(frozen=True)
class AcquisitionParserDescriptor:
    parser_id: str
    version: str
    supported_content_types: tuple[str, ...]
    supported_record_types: tuple[str, ...]
    priority: int = 100
    enabled: bool = True
    source_types: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class AcquisitionParser(Protocol):
    descriptor: AcquisitionParserDescriptor

    def can_parse(self, artifact: RawArtifact) -> bool: ...

    def parse(self, artifact: RawArtifact) -> tuple[ParsedRecord, ...]: ...

    def validate_output(self, record: ParsedRecord) -> ValidationResult: ...


class ParserRegistry:
    def __init__(self):
        self._parsers: list[AcquisitionParser] = []
        self._frozen = False

    def register(self, parser: AcquisitionParser) -> None:
        if self._frozen:
            raise RuntimeError("parser_registry_frozen")
        self._parsers.append(parser)
        self._parsers.sort(key=lambda p: p.descriptor.priority)

    def freeze(self) -> None:
        self._frozen = True

    def list_descriptors(self) -> tuple[AcquisitionParserDescriptor, ...]:
        return tuple(p.descriptor for p in self._parsers if p.descriptor.enabled)

    def select(self, artifact: RawArtifact) -> AcquisitionParser:
        for parser in self._parsers:
            if not parser.descriptor.enabled:
                continue
            try:
                if parser.can_parse(artifact):
                    return parser
            except Exception:
                continue
        raise ParserNotFoundError()

    def parse(self, artifact: RawArtifact) -> tuple[ParsedRecord, ...]:
        if not artifact.content_type and not artifact.content_text:
            raise UnsupportedContentError()
        parser = self.select(artifact)
        try:
            records = parser.parse(artifact)
        except (ParserFailedError, UnsupportedContentError, ParserNotFoundError):
            raise
        except Exception as exc:
            raise ParserFailedError() from exc
        out = []
        for rec in records:
            vr = parser.validate_output(rec)
            if not vr.ok:
                from dataclasses import replace

                rec = replace(
                    rec,
                    validation_ok=False,
                    validation_errors=vr.errors,
                )
            out.append(rec)
        return tuple(out)


def build_default_parser_registry() -> ParserRegistry:
    from acquisition.parsers.competitor import CompetitorHtmlParser
    from acquisition.parsers.document_bridge import DocumentBridgeParser
    from acquisition.parsers.generic import (
        CsvTableParser,
        HtmlTextParser,
        JsonParser,
        XmlParser,
    )
    from acquisition.parsers.marketplace import MarketplaceJsonParser
    from acquisition.parsers.price import PriceListCsvParser
    from acquisition.parsers.search import SearchResultParser
    from acquisition.parsers.supplier import SupplierFeedParser

    reg = ParserRegistry()
    for parser in (
        PriceListCsvParser(),
        SupplierFeedParser(),
        CompetitorHtmlParser(),
        MarketplaceJsonParser(),
        SearchResultParser(),
        CsvTableParser(),
        JsonParser(),
        XmlParser(),
        HtmlTextParser(),
        DocumentBridgeParser(),
    ):
        reg.register(parser)
    reg.freeze()
    return reg
