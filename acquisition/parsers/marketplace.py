"""Marketplace canonical record parser — provider schemas normalized."""

from __future__ import annotations

from acquisition.models import RECORD_MARKETPLACE, RawArtifact, utc_now
from acquisition.parsers import AcquisitionParserDescriptor
from acquisition.parsers._helpers import BaseParser, make_record, parse_json_payload


class MarketplaceJsonParser(BaseParser):
    descriptor = AcquisitionParserDescriptor(
        parser_id="marketplace.json",
        version="1.0.0",
        supported_content_types=("application/json",),
        supported_record_types=(RECORD_MARKETPLACE,),
        priority=28,
        source_types=("marketplace",),
    )

    def can_parse(self, artifact: RawArtifact) -> bool:
        meta = dict(artifact.metadata or {})
        if meta.get("record_hint") == "marketplace":
            return True
        if meta.get("marketplace") in {"ozon", "wildberries", "yandex_market"}:
            return True
        return False

    def parse(self, artifact: RawArtifact):
        data = parse_json_payload(artifact.content_text or "{}")
        items = data.get("products") if isinstance(data, dict) else None
        if items is None and isinstance(data, dict):
            items = data.get("items") or [data]
        if not isinstance(items, list):
            items = []
        provider = str(dict(artifact.metadata or {}).get("marketplace") or "generic")
        out = []
        for idx, item in enumerate(items[:5_000]):
            if not isinstance(item, dict):
                continue
            fields = {
                "provider": provider,
                "sku": item.get("sku") or item.get("offer_id") or item.get("product_id"),
                "ean": item.get("ean") or item.get("barcode"),
                "name": item.get("name") or item.get("title"),
                "price": item.get("price") or item.get("marketing_price"),
                "currency": item.get("currency") or "RUB",
                "stock": item.get("stock") or item.get("quantity"),
                "fees": item.get("fees") or item.get("commission"),
                "status": item.get("status"),
                "orders": item.get("orders"),
                "observed_at": utc_now().isoformat(),
            }
            out.append(
                make_record(
                    artifact=artifact,
                    parser_id=self.descriptor.parser_id,
                    parser_version=self.descriptor.version,
                    record_type=RECORD_MARKETPLACE,
                    fields={k: v for k, v in fields.items() if v is not None},
                    confidence=0.8,
                    raw_field_refs={"index": idx, "provider": provider},
                )
            )
        return tuple(out)
