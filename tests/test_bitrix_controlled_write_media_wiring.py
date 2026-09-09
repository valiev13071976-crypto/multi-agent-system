"""Product enrichment pipeline follow-up: wires ``SingleProductWriteRequest.
preview_picture``/``detail_picture`` (already-downloaded/validated/base64
bytes -- see ``product_enrichment.media``) through
``business_assistant.controlled_bitrix_write``'s canonical payload into the
real ``catalog.product.add`` call.

``tests/test_bitrix_live_product_create_write.py``'s ``ContentAndMediaFieldsTests``
already covers the LOWER layer (``LiveBitrixAdapter._media_fields`` field
construction/validation once a ``product_in["media"]`` dict already
exists). This file covers the layer ABOVE that: nothing in
``controlled_bitrix_write.py`` populated ``canonical["media"]`` at all
before this pass -- ``SingleProductWriteRequest`` had no media fields, so
the confirmed write shape from #48 was unreachable from the controlled
write path. These tests prove the gap is closed end to end, through
``execute_single_product_write``, on the mocked LIVE transport already
used throughout the Bitrix regression suite -- zero real network calls,
zero real Bitrix mutations.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from business_assistant.controlled_bitrix_write import (
    STATUS_UNRESOLVED,
    STATUS_WRITE_VERIFIED,
    SingleProductWriteRequest,
    execute_single_product_write,
    format_bitrix_write_result_text,
    prepare_single_product_write,
)
from integrations.bitrix import schema
from integrations.production.http import BoundedHttpClient

from tests.test_bitrix_live_product_create_write import (
    TARGET_BRAND,
    TARGET_PURCHASE_PRICE,
    TARGET_RETAIL_PRICE,
    TARGET_SKU,
    TARGET_TENANT,
    TARGET_TITLE,
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
)

PREVIEW_PICTURE = {"filename": "preview.jpg", "base64": "QUJD"}
DETAIL_PICTURE = {"filename": "detail.jpg", "base64": "WFla"}


def _request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=TARGET_TENANT,
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=TARGET_RETAIL_PRICE,
        brand=TARGET_BRAND,
        purchase_price=TARGET_PURCHASE_PRICE,
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


class MediaFieldsReachRealCreateCallTests(unittest.TestCase):
    def test_preview_and_detail_picture_reach_catalog_product_add(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(preview_picture=PREVIEW_PICTURE, detail_picture=DETAIL_PICTURE),
                approved=True,
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(
            product_body["fields"][schema.PREVIEW_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: ["preview.jpg", "QUJD"]},
        )
        self.assertEqual(
            product_body["fields"][schema.DETAIL_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: ["detail.jpg", "WFla"]},
        )
        self.assertIn(schema.PREVIEW_PICTURE_FIELD, result["media_written"])
        self.assertIn(schema.DETAIL_PICTURE_FIELD, result["media_written"])

    def test_no_picture_supplied_never_sends_media_fields(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertNotIn(schema.PREVIEW_PICTURE_FIELD, product_body["fields"])
        self.assertNotIn(schema.DETAIL_PICTURE_FIELD, product_body["fields"])
        self.assertEqual(result["media_written"], [])

    def test_malformed_picture_entry_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            preview = prepare_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(preview_picture={"filename": "only-a-name.jpg"})
            )
        self.assertEqual(preview["status"], STATUS_UNRESOLVED)
        self.assertEqual(preview["reason"], "invalid_preview_picture")
        self.assertEqual(transport.calls, [])

    def test_result_text_mentions_uploaded_media_never_a_hotlink(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(preview_picture=PREVIEW_PICTURE), approved=True
            )
        text = format_bitrix_write_result_text(result)
        self.assertIn("Изображения загружены", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()
