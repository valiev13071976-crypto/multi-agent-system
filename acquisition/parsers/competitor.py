"""Competitor listing parser foundation — no repricing."""

from __future__ import annotations

import re

from acquisition.models import RECORD_COMPETITOR, RawArtifact, utc_now
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record, strip_html


_PRICE_RE = re.compile(
    r"(?:price|цена)[\"'\s:=]+([0-9]+(?:[.,][0-9]+)?)",
    re.IGNORECASE,
)
_AVAIL_RE = re.compile(r"(in[\s_-]?stock|out[\s_-]?of[\s_-]?stock|наличие|нет в наличии)", re.I)


class CompetitorHtmlParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="competitor.html",
        version="1.0.0",
        supported_content_types=("text/html", "application/json"),
        supported_record_types=(RECORD_COMPETITOR,),
        priority=30,
        source_types=("competitor", "website"),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        meta = dict(artifact.metadata or {})
        if meta.get("record_hint") == "competitor":
            return True
        if artifact.source_id.startswith("competitor"):
            return "html" in (artifact.content_type or "").lower()
        return False

    def parse(self, artifact: RawArtifact):
        text = artifact.content_text or ""
        plain = strip_html(text)
        price = None
        m = _PRICE_RE.search(text) or _PRICE_RE.search(plain)
        if m:
            try:
                price = float(m.group(1).replace(",", "."))
            except ValueError:
                price = None
        avail = None
        am = _AVAIL_RE.search(text) or _AVAIL_RE.search(plain)
        if am:
            avail = am.group(1).lower()
        fields = {
            "name": plain[:200],
            "listed_price": price,
            "price": price,
            "availability": avail,
            "seller": dict(artifact.metadata or {}).get("seller"),
            "url": artifact.url,
            "observed_at": utc_now().isoformat(),
        }
        return (
            make_record(
                artifact=artifact,
                parser_id=self.descriptor.parser_id,
                parser_version=self.descriptor.version,
                record_type=RECORD_COMPETITOR,
                fields={k: v for k, v in fields.items() if v is not None},
                confidence=0.55,
            ),
        )
