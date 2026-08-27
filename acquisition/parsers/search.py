"""Search result normalization — evidence/discovery, not trusted facts."""

from __future__ import annotations

from acquisition.models import RECORD_SEARCH_HIT, RawArtifact, utc_now
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record, parse_json_payload


class SearchResultParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="search.results",
        version="1.0.0",
        supported_content_types=("application/json", "application/x-search-results"),
        supported_record_types=(RECORD_SEARCH_HIT,),
        priority=15,
        source_types=("search",),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        ct = (artifact.content_type or "").lower()
        if "search" in ct:
            return True
        meta = dict(artifact.metadata or {})
        return meta.get("record_hint") == "search" or meta.get("query") is not None

    def parse(self, artifact: RawArtifact):
        data = parse_json_payload(artifact.content_text or "{}")
        query = ""
        results = []
        if isinstance(data, dict):
            query = str(data.get("query") or dict(artifact.metadata or {}).get("query") or "")
            results = data.get("results") or data.get("items") or []
        elif isinstance(data, list):
            results = data
        out = []
        for idx, item in enumerate(results[:100]):
            if not isinstance(item, dict):
                continue
            fields = {
                "query": query,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("snippet") or item.get("description") or "",
                "source_domain": item.get("source_domain") or item.get("domain") or "",
                "rank": item.get("rank", idx + 1),
                "observed_at": utc_now().isoformat(),
            }
            out.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_SEARCH_HIT,
                    fields=fields,
                    confidence=0.4,  # discovery evidence, not fact
                    raw_field_refs={"rank": fields["rank"]},
                )
            )
        return tuple(out)
