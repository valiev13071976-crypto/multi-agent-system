"""Production defect closure: ``no_matching_section_found`` even though
``catalog.section.list`` answers HTTP 200 with the section that exists.

Every case below runs against ``tests/fixtures/
bitrix_catalog_section_list_production.json`` -- the VERBATIM response of a
read-only ``catalog.section.list`` call against the production Bitrix/Aspro
install (only the ``time`` block removed, webhook secret never stored).
That real response exposes two things the resolver did not handle:

1. It is PAGED. ``total`` is 62, one page carries 50 sections plus a
   ``next: 50`` cursor. The adapter read a single page, so 12 real
   sections (e.g. "Сантехника" 102, "Смесители" 105) were invisible and
   failed closed as if they did not exist.
2. Its section names are single shop terms ("Телевизоры"), while a
   supplier price list labels the same category as a COMPOUND string
   ("ТВ", "Smart TV", "Телевизоры LED", "Электроника / Телевизоры").
   Matching only compared whole labels to whole section names, so none of
   those could ever reach section 70.

No live mutation is possible from this file: the only Bitrix traffic is an
``httpx.MockTransport`` replaying the captured pages.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import httpx

from integrations.bitrix import schema
from integrations.bitrix.config import load_bitrix_config
from integrations.bitrix.live_adapter import LiveBitrixAdapter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bitrix_catalog_section_list_production.json"
WEBHOOK_URL = "https://panda.msk.ru/rest/1/fake-webhook-secret-for-tests/"
PRODUCTION_CATALOG_IBLOCK_ID = 14

# The real section this installation's TV products belong to, as returned
# by the captured response -- read from the fixture by NAME, never
# hardcoded as an id in production code.
TV_SECTION_NAME = "Телевизоры"


def _fixture() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _page_sections(index: int) -> list:
    return list(_fixture()["pages"][index]["response"]["result"]["sections"])


def _all_sections() -> list:
    return [s for page in _fixture()["pages"] for s in page["response"]["result"]["sections"]]


def _section_id_named(name: str) -> int:
    return next(s["id"] for s in _all_sections() if s["name"] == name)


class ProductionResponseShapeTests(unittest.TestCase):
    def test_captured_response_is_paged_and_incomplete_on_the_first_page(self):
        pages = _fixture()["pages"]
        first = pages[0]["response"]
        self.assertEqual(first["total"], 62)
        self.assertEqual(len(first["result"]["sections"]), 50)
        self.assertEqual(first["next"], 50)
        self.assertEqual(pages[1]["request"]["start"], 50)
        self.assertEqual(len(_all_sections()), 62)

    def test_first_page_alone_hides_real_sections(self):
        first_page_names = {s["name"] for s in _page_sections(0)}
        all_names = {s["name"] for s in _all_sections()}
        self.assertTrue(all_names - first_page_names)


class SupplierLabelResolvesToRealSectionTests(unittest.TestCase):
    """The exact production symptom: a TV product could not reach the
    "Телевизоры" section that the response plainly contains."""

    def setUp(self):
        self.sections = _page_sections(0)
        self.tv_section_id = _section_id_named(TV_SECTION_NAME)

    def test_compound_and_abbreviated_supplier_labels_resolve(self):
        for label in (
            "ТВ",
            "TV",
            "Smart TV",
            "Телевизоры LED",
            "ТВ и аудио",
            "Электроника / Телевизоры",
        ):
            with self.subTest(label=label):
                resolved = schema.resolve_section_id(category=label, sections=self.sections)
                self.assertEqual(resolved["section_id"], self.tv_section_id)
                self.assertEqual(resolved["name"], TV_SECTION_NAME)

    def test_more_specific_child_section_still_wins_when_named(self):
        resolved = schema.resolve_section_id(subcategory="Смарт-телевизоры", sections=self.sections)
        self.assertEqual(resolved["section_id"], _section_id_named("Смарт-телевизоры"))

    def test_duplicate_section_names_still_fail_closed(self):
        # "Аксессуары" genuinely exists twice in this catalog.
        duplicated = [s["name"] for s in _all_sections()].count("Аксессуары")
        self.assertGreater(duplicated, 1)
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category="Аксессуары", sections=self.sections)
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_qualified_hyphenated_supplier_labels_resolve(self):
        """A price list qualifies the category inline ("ЖК-телевизоры",
        "Смарт-ТВ", "LED-телевизоры") while the shop simply calls the
        section "Телевизоры" -- the hyphenated token was one opaque word,
        so none of these could reach it."""
        for label in ("Смарт-ТВ", "ЖК-телевизоры", "LED-телевизоры", "OLED-телевизоры", "ТВ-техника"):
            with self.subTest(label=label):
                resolved = schema.resolve_section_id(category=label, sections=self.sections)
                self.assertEqual(resolved["section_id"], self.tv_section_id)

    def test_hyphenated_section_names_keep_their_own_identity(self):
        for name in ("Смарт-телевизоры", "Смарт-часы"):
            with self.subTest(name=name):
                resolved = schema.resolve_section_id(subcategory=name, sections=self.sections)
                self.assertEqual(resolved["section_id"], _section_id_named(name))

    def test_failure_names_the_candidate_and_the_snapshot_it_was_matched_against(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category="Мониторы", sections=self.sections)
        message = str(ctx.exception)
        self.assertIn("Мониторы", message)
        self.assertIn(str(len(self.sections)), message)

    def test_empty_section_list_is_reported_as_its_own_failure(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category="ТВ", sections=[])
        self.assertEqual(ctx.exception.code, "section_list_empty")

    def test_unknown_category_still_fails_closed(self):
        for label in ("CE", "Продукты питания", "Widgets"):
            with self.subTest(label=label):
                with self.assertRaises(schema.SectionResolutionError) as ctx:
                    schema.resolve_section_id(category=label, sections=self.sections)
                self.assertEqual(ctx.exception.code, "no_matching_section_found")

    def test_unrelated_branches_of_the_same_label_stay_ambiguous(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category="Наушники и аксессуары", sections=self.sections)
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_section_beyond_the_first_page_only_resolves_once_all_pages_are_read(self):
        beyond = next(s for s in _page_sections(1) if s["name"] == "Смесители")
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category=beyond["name"], sections=_page_sections(0))
        self.assertEqual(ctx.exception.code, "no_matching_section_found")
        resolved = schema.resolve_section_id(category=beyond["name"], sections=_all_sections())
        self.assertEqual(resolved["section_id"], beyond["id"])


class UnresolvedWriteTextNamesTheCandidateTests(unittest.TestCase):
    """A production failure has to be diagnosable from the owner's own
    transcript: the reason code alone never said which value failed."""

    def test_unresolved_text_includes_the_resolution_detail(self):
        from business_assistant.controlled_bitrix_write import (
            STATUS_UNRESOLVED,
            format_bitrix_write_result_text,
        )

        try:
            schema.resolve_section_id(category="Мониторы", sections=_all_sections())
        except schema.SectionResolutionError as exc:
            result = {"status": STATUS_UNRESOLVED, "reason": exc.code, "detail": str(exc)}

        text = format_bitrix_write_result_text(result)
        self.assertIn("no_matching_section_found", text)
        self.assertIn("Мониторы", text)

    def test_unresolved_text_without_detail_is_unchanged(self):
        from business_assistant.controlled_bitrix_write import (
            STATUS_UNRESOLVED,
            format_bitrix_write_result_text,
        )

        text = format_bitrix_write_result_text({"status": STATUS_UNRESOLVED, "reason": "missing_title_or_sku"})
        self.assertEqual(text, "Не удалось подготовить запись в Bitrix: missing_title_or_sku.")


class LiveSectionReadFollowsPaginationTests(unittest.TestCase):
    """The adapter must hand the resolver the WHOLE section list."""

    def setUp(self):
        self._prior = os.environ.get("BITRIX_WEBHOOK_URL")
        os.environ["BITRIX_WEBHOOK_URL"] = WEBHOOK_URL

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("BITRIX_WEBHOOK_URL", None)
        else:
            os.environ["BITRIX_WEBHOOK_URL"] = self._prior

    def test_read_replays_every_captured_page(self):
        pages = _fixture()["pages"]
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            start = body.get("start")
            page = next(
                (p for p in pages if p["request"].get("start") == start),
                None,
            )
            self.assertIsNotNone(page, f"unexpected start offset: {start}")
            return httpx.Response(200, json=page["response"])

        adapter = LiveBitrixAdapter(
            config=load_bitrix_config(
                {"BITRIX_INTEGRATION_MODE": "LIVE", "BITRIX_CATALOG_ID": str(PRODUCTION_CATALOG_IBLOCK_ID)}
            )
        )
        adapter.client._http._client = httpx.Client(transport=httpx.MockTransport(handler))

        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "section_read"})

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["filter"], {"iblockId": PRODUCTION_CATALOG_IBLOCK_ID})
        self.assertNotIn("start", requests[0])
        self.assertEqual(requests[1]["start"], 50)
        self.assertEqual(len(out["items"]), 62)
        self.assertIn(TV_SECTION_NAME, {s["name"] for s in out["items"]})
        self.assertIn("Смесители", {s["name"] for s in out["items"]})

    def test_read_stops_when_the_response_has_no_next_cursor(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"result": {"sections": _page_sections(1)}, "total": 12})

        adapter = LiveBitrixAdapter(
            config=load_bitrix_config(
                {"BITRIX_INTEGRATION_MODE": "LIVE", "BITRIX_CATALOG_ID": str(PRODUCTION_CATALOG_IBLOCK_ID)}
            )
        )
        adapter.client._http._client = httpx.Client(transport=httpx.MockTransport(handler))

        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "section_read"})

        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(out["items"]), 12)


if __name__ == "__main__":
    unittest.main()
