"""Connects ``product_enrichment`` to the EXISTING, unchanged governed
Bitrix write path (``business_assistant.product_enrichment_bridge``).
Covers: identity-query building, running enrichment through a fake
``ToolGateway``-shaped double (no real network), merging enrichment output
into ``SingleProductWriteRequest`` (never overriding an explicit source
field), the combined preview message, and write-request
serialize/deserialize round-tripping used across conversational turns."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from business_assistant.controlled_bitrix_write import SingleProductWriteRequest
from business_assistant.product_enrichment_bridge import (
    build_enriched_write_request,
    build_identity_query_from_fields,
    deserialize_write_request,
    format_combined_preview_text,
    prepare_complete_card,
    run_enrichment,
    serialize_write_request,
)
from product_enrichment.models import EnrichmentResult

LG_FIELDS = {
    "title": "LG 55MRGB86B6A.ARUG",
    "sku": "55MRGB86B6A.ARUG",
    "ean": "8806096824788",
    "category": "CE",
    "brand": "LG",
    "purchase_price": "103198.3",
}


def _png(w: int = 400, h: int = 400) -> bytes:
    img = Image.new("RGB", (w, h), (5, 5, 5))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class _FakeToolGateway:
    """Duck-typed ``ToolGateway`` double: only implements ``search``/
    ``invoke``, exactly what ``ToolGatewayResearchAdapter`` calls -- no
    real network, deterministic fixed content."""

    def __init__(self, *, search_results, page_text_by_url):
        self._search_results = search_results
        self._page_text_by_url = page_text_by_url

    async def search(self, query, max_results=5):
        return self._search_results[:max_results]

    async def invoke(self, request):
        from types import SimpleNamespace

        url = str(request.arguments.get("url") or "")
        text = self._page_text_by_url.get(url, "")
        return SimpleNamespace(success=True, data={"body_text": text})


def _search_result(url, title):
    from datetime import datetime, timezone

    from tools.models import SearchResult

    return SearchResult(
        title=title,
        url=url,
        snippet="",
        source_domain="lg.com",
        published_at=None,
        retrieved_at=datetime.now(timezone.utc),
        trust_level="medium",
    )


class BuildIdentityQueryFromFieldsTests(unittest.TestCase):
    def test_builds_query_from_flat_field_dict(self):
        query = build_identity_query_from_fields(LG_FIELDS)
        self.assertEqual(query.brand, "LG")
        self.assertEqual(query.model, "55MRGB86B6A.ARUG")
        self.assertEqual(query.article, "55MRGB86B6A.ARUG")
        self.assertEqual(query.ean, "8806096824788")
        self.assertEqual(query.category, "CE")

    def test_model_field_preferred_over_sku_when_both_present(self):
        fields = dict(LG_FIELDS, model="EXACT-MODEL-CODE")
        query = build_identity_query_from_fields(fields)
        self.assertEqual(query.model, "EXACT-MODEL-CODE")


class RunEnrichmentTests(unittest.TestCase):
    def test_no_tool_gateway_skips_research_never_hallucinates(self):
        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS))
        self.assertFalse(result.research_available)
        self.assertEqual(result.characteristics, {})

    def test_with_fake_tool_gateway_produces_facts(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG"
        gateway = _FakeToolGateway(
            search_results=[_search_result(url, "LG 55MRGB86B6A.ARUG specs")],
            page_text_by_url={url: "Диагональ экрана: 139 см\nЦвет: черный"},
        )
        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS, tool_gateway=gateway))
        self.assertTrue(result.research_available)
        self.assertIn("screen_diagonal_cm", result.characteristics)


class BuildEnrichedWriteRequestTests(unittest.TestCase):
    def test_enrichment_fills_gaps_never_overrides_explicit_source_fields(self):
        fields = dict(LG_FIELDS, subcategory="", short_description="")
        result = _run(run_enrichment(tenant_id="t1", product_fields=fields))
        # Manually attach content as if research had produced it.
        from product_enrichment.content import generate_content
        from product_enrichment.identity import resolve_identity
        from product_enrichment.models import ProductIdentityQuery
        import dataclasses

        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        content = generate_content(identity, {})
        enrichment = dataclasses.replace(result, content=content)

        request = build_enriched_write_request(fields, tenant_id="t1", retail_price="120000", enrichment=enrichment)
        self.assertEqual(request.short_description, content.short_description)

    def test_explicit_short_description_from_source_wins_over_enrichment(self):
        fields = dict(LG_FIELDS, short_description="Оригинальное краткое описание из XLSX")
        result = _run(run_enrichment(tenant_id="t1", product_fields=fields))
        request = build_enriched_write_request(fields, tenant_id="t1", retail_price="120000", enrichment=result)
        self.assertEqual(request.short_description, "Оригинальное краткое описание из XLSX")

    def test_verified_characteristics_flow_into_write_request(self):
        from product_enrichment.models import (
            CONFIDENCE_VERIFIED,
            NormalizedCharacteristic,
        )
        import dataclasses

        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS))
        enriched = dataclasses.replace(
            result,
            characteristics={
                "color": NormalizedCharacteristic(
                    key="color", value="черный", confidence=CONFIDENCE_VERIFIED, bitrix_property_id=246, bitrix_writable=True
                ),
                "usb_count": NormalizedCharacteristic(
                    key="usb_count", value="3", confidence=CONFIDENCE_VERIFIED, bitrix_property_id=None, bitrix_writable=False
                ),
            },
        )
        request = build_enriched_write_request(LG_FIELDS, tenant_id="t1", retail_price="120000", enrichment=enriched)
        self.assertEqual(request.characteristics.get("color"), "черный")
        # Non-writable (no verified Bitrix property) characteristic never
        # flows into the write request's characteristics dict.
        self.assertNotIn("usb_count", request.characteristics)

    def test_media_assets_populate_preview_and_detail_picture_fields(self):
        from product_enrichment.media import MediaAcquisitionService
        from product_enrichment.media_fetch import FakeImageFetcher
        from product_enrichment.identity import resolve_identity
        from product_enrichment.models import MediaCandidateInput, ProductIdentityQuery
        import dataclasses

        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        fetcher = FakeImageFetcher({"https://lg.com/photo.png": _png()})
        service = MediaAcquisitionService(fetcher=fetcher)
        media_result = _run(
            service.acquire([MediaCandidateInput(url="https://lg.com/photo.png")], identity=identity)
        )
        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS))
        enriched = dataclasses.replace(result, media=media_result)

        request = build_enriched_write_request(LG_FIELDS, tenant_id="t1", retail_price="120000", enrichment=enriched)
        self.assertTrue(request.preview_picture.get("filename"))
        self.assertTrue(request.preview_picture.get("base64"))
        self.assertTrue(request.detail_picture.get("base64"))
        # The write request never carries a bare external URL as the
        # "final" image reference -- only already-encoded bytes.
        self.assertNotIn("https://", request.preview_picture.get("base64", ""))

    def test_existing_explicit_preview_picture_is_never_overridden_by_enrichment(self):
        fields = dict(LG_FIELDS)
        fields["preview_picture"] = {"filename": "explicit.jpg", "base64": "RVhQTA=="}
        from product_enrichment.media import MediaAcquisitionService
        from product_enrichment.media_fetch import FakeImageFetcher
        from product_enrichment.identity import resolve_identity
        from product_enrichment.models import MediaCandidateInput, ProductIdentityQuery
        import dataclasses

        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        fetcher = FakeImageFetcher({"https://lg.com/photo.png": _png()})
        service = MediaAcquisitionService(fetcher=fetcher)
        media_result = _run(
            service.acquire([MediaCandidateInput(url="https://lg.com/photo.png")], identity=identity)
        )
        result = _run(run_enrichment(tenant_id="t1", product_fields=fields))
        enriched = dataclasses.replace(result, media=media_result)
        request = build_enriched_write_request(fields, tenant_id="t1", retail_price="120000", enrichment=enriched)
        self.assertEqual(request.preview_picture, {"filename": "explicit.jpg", "base64": "RVhQTA=="})


class FormatCombinedPreviewTextTests(unittest.TestCase):
    def test_no_write_preview_still_shows_enrichment_and_hitl_note(self):
        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS))
        text = format_combined_preview_text(result, None)
        self.assertIn("READY TO WRITE", text)
        self.assertIn("НЕ подтверждение записи", text)

    def test_requires_approval_write_preview_appends_section_and_price(self):
        result = _run(run_enrichment(tenant_id="t1", product_fields=LG_FIELDS))
        write_preview = {
            "status": "REQUIRES_APPROVAL",
            "target_product": {"resolved_section_id": 42},
            "retail_price": {"amount": "120000", "currency": "RUB"},
            "will_write": ["name", "article/sku"],
            "will_not_write": [{"field": "ean", "reason": "x"}],
        }
        text = format_combined_preview_text(result, write_preview)
        self.assertIn("ID 42", text)
        self.assertIn("120000", text)
        self.assertIn("name", text)


class PrepareCompleteCardTests(unittest.TestCase):
    def test_prepare_complete_card_without_bitrix_bridge_returns_enrichment_only(self):
        outcome = _run(prepare_complete_card(tenant_id="t1", product_fields=LG_FIELDS, retail_price="120000"))
        self.assertIsInstance(outcome["enrichment"], EnrichmentResult)
        self.assertEqual(outcome["write_preview"], {})
        self.assertIn("READY TO WRITE", outcome["text"])
        self.assertIsInstance(outcome["write_request"], SingleProductWriteRequest)

    def test_prepare_complete_card_never_calls_bitrix_bridge_when_retail_price_missing(self):
        called = {"n": 0}

        class ExplodingBridge:
            def __getattr__(self, name):
                called["n"] += 1
                raise AssertionError("bitrix_bridge must never be touched without a retail price")

        outcome = _run(
            prepare_complete_card(tenant_id="t1", product_fields=LG_FIELDS, retail_price="", bitrix_bridge=ExplodingBridge())
        )
        self.assertEqual(called["n"], 0)
        self.assertEqual(outcome["write_preview"], {})


class SerializeDeserializeWriteRequestTests(unittest.TestCase):
    def test_round_trip_preserves_all_fields(self):
        request = SingleProductWriteRequest(
            tenant_id="t1",
            title="LG 55MRGB86B6A.ARUG",
            sku="55MRGB86B6A.ARUG",
            retail_price="120000",
            ean="8806096824788",
            brand="LG",
            characteristics={"color": "черный"},
            preview_picture={"filename": "p.jpg", "base64": "QUJD"},
            detail_picture={"filename": "d.jpg", "base64": "WFla"},
        )
        data = serialize_write_request(request)
        restored = deserialize_write_request(data)
        self.assertEqual(restored, request)

    def test_serialized_form_is_json_safe_plain_dict(self):
        import json

        request = SingleProductWriteRequest(
            tenant_id="t1", title="X", sku="Y", retail_price="1", characteristics={"a": "b"}
        )
        data = serialize_write_request(request)
        json.dumps(data)  # must not raise


if __name__ == "__main__":
    unittest.main()
