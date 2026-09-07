"""General-web HTML parser — deterministic structured extraction (Block 5.2).

Applies to HTML acquired from ad-hoc, ``TRUST_GENERAL_WEB`` sources (see
``acquisition/web_source.py``'s ephemeral source registration) — i.e. ordinary
public pages the user pasted in chat, as opposed to pre-registered
supplier/competitor/marketplace feeds which already have dedicated parsers.

All extraction is deterministic (``acquisition.html_extract`` / lxml) — no
LLM calls, no regex-as-primary-parser, no fabricated records. A repeated-item
("card") layout yields one record per item (title/price/url); otherwise an
explicit table request yields one record per row; otherwise a single bounded
page-level record (metadata + main text) is produced.
"""

from __future__ import annotations

from acquisition.html_extract import (
    detect_price_ambiguity,
    extract_cards,
    extract_main_text,
    extract_metadata,
    extract_tables,
    parse_html,
)
from acquisition.models import RECORD_GENERIC, TRUST_GENERAL_WEB, RawArtifact
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record

MAX_RECORDS_PER_ARTIFACT = 500
EPHEMERAL_SOURCE_PREFIX = "web-adhoc-"


def _to_number(text: str) -> float | None:
    """Deterministic numeric-text -> float, mirroring
    ``acquisition.normalize.normalize_number`` so a card/table price is
    already a clean float at parse time (never a space-formatted string that
    would otherwise fail ``validate_record``'s numeric price check)."""

    cleaned = str(text or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


class WebGenericHtmlParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="web.generic_html",
        version="1.0.0",
        supported_content_types=("text/html", "application/xhtml+xml"),
        supported_record_types=(RECORD_GENERIC,),
        priority=32,
        source_types=("website",),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        looks_html = "html" in ct or (artifact.content_text or "").lstrip().lower().startswith(
            ("<!doctype html", "<html")
        )
        if not looks_html:
            return False
        meta = dict(artifact.metadata or {})
        if meta.get("source_trust") == TRUST_GENERAL_WEB:
            return True
        return str(artifact.source_id or "").startswith(EPHEMERAL_SOURCE_PREFIX)

    def parse(self, artifact: RawArtifact):
        html_text = artifact.content_text or ""
        tree = parse_html(html_text)
        base_url = artifact.url or ""
        plan = dict(dict(artifact.metadata or {}).get("extraction_plan") or {})
        mode = str(plan.get("mode") or "auto")

        if mode == "table":
            records = self._table_records(tree, artifact, base_url, plan)
            if records:
                return records
            return (self._page_record(tree, artifact, base_url),)

        if mode == "text":
            return (self._page_record(tree, artifact, base_url),)

        cards = extract_cards(tree, base_url=base_url)
        if cards:
            return self._card_records(cards, artifact)

        table_records = self._table_records(tree, artifact, base_url, plan)
        if table_records:
            return table_records

        return (self._page_record(tree, artifact, base_url),)

    def _card_records(self, cards, artifact):
        out = []
        for card in cards[:MAX_RECORDS_PER_ARTIFACT]:
            fields = {k: v for k, v in card.items() if v not in (None, "")}
            if not fields:
                continue
            if "price" in fields:
                num = _to_number(fields["price"])
                if num is not None:
                    fields["price"] = num
                else:
                    fields["price_text"] = fields.pop("price")
            out.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_GENERIC,
                    fields=fields,
                    confidence=0.6,
                )
            )
        return tuple(out)

    def _table_records(self, tree, artifact, base_url, plan):
        tables = extract_tables(tree, base_url=base_url)
        if not tables:
            return ()
        idx = int(plan.get("table_index") or 0)
        if idx < 0 or idx >= len(tables):
            idx = 0
        table = tables[idx]
        if not table.rows:
            return ()
        out = []
        for row in table.rows[:MAX_RECORDS_PER_ARTIFACT]:
            fields = dict(row)
            for key in list(fields.keys()):
                if str(key).lower() in {"price", "цена", "стоимость"} and fields[key]:
                    num = _to_number(fields[key])
                    if num is not None:
                        fields["price"] = num
            fields["table_index"] = table.index
            fields["url"] = base_url
            out.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_GENERIC,
                    fields={k: v for k, v in fields.items() if v not in (None, "")},
                    confidence=0.7,
                )
            )
        return tuple(out)

    def _page_record(self, tree, artifact, base_url):
        meta = extract_metadata(tree, base_url=base_url)
        text = extract_main_text(tree)
        ambiguity = detect_price_ambiguity(tree)
        fields = {
            "title": meta.title,
            "description": meta.description,
            "canonical_url": meta.canonical_url,
            "url": base_url,
            "text": text,
            "headings": list(meta.headings[:50]),
            "links": [item["url"] for item in meta.links[:100]],
            "price_ambiguous": ambiguity.ambiguous,
        }
        if ambiguity.ambiguous:
            fields["price_candidates"] = list(ambiguity.values)[:10]
        cleaned = {k: v for k, v in fields.items() if v not in (None, "", [], False)}
        return make_record(
            artifact=artifact,
            parser_id=self.descriptor.parser_id,
            parser_version=self.descriptor.version,
            record_type=RECORD_GENERIC,
            fields=cleaned,
            confidence=0.5,
        )
