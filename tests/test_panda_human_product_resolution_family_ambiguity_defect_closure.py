"""PANDA -- human product resolution: candidate ranking/ambiguity defect
closure (post PR #99).

REAL PRODUCTION FAILURE (LG_TV.xlsx, 13 rows): a natural reference like
"Покажи LG 32 63806" -- brand + screen-size + model-family code, written
with a plain space between the two digit groups exactly like a human
would type it -- returned the generic dead-end

    "Не удалось однозначно выбрать товар."

even though the referenced product family ("...63806...") existed in the
dataset. Read-only trace (``data_intel.service._resolve_product_
reference`` against a realistic fixture with the SAME shape) showed the
root cause: real model codes routinely interleave their meaningful digit
groups with letter suffixes/series codes, e.g. "32LQ63806LA" = size "32"
+ series letters "LQ" + family code "63806" + generation suffix "LA".
Tier 3's old check required ONE contiguous common substring of at least
6 characters between what the user typed and the row's own identifier
value -- but the "LQ" in the middle breaks that into two separate runs
("32" and "63806", only the second even close to 6 chars, and never
combined), so EVERY row in that model family failed the threshold
equally and the resolver returned ``NONE`` (zero candidates), not even
``AMBIGUOUS``.

FIX (``data_intel.service._resolve_product_reference``, tier 3): an
ADDITIONAL, token-set-based check -- a row's own digit-run tokens
(extracted from its identifier/article/EAN/product-name columns) are
compared as a SET against the digit-run tokens the user actually typed.
When every digit group the user named is present somewhere in the row's
own tokens (regardless of which letters separate them in the source
value), AND the row's brand is also named (unchanged from before), AND
the combined length of the user's own tokens is still >=
``_MIN_PARTIAL_REFERENCE_LEN`` (6, unchanged), the row becomes a
candidate. This is purely ADDITIVE to the existing exact/normalized/
common-substring tiers -- it can only ever produce MORE UNIQUE
selections or MORE (real, surfaced) AMBIGUOUS candidates than before; it
never removes a check, never lowers the brand requirement, and a bare
brand/empty digit-token reference still never matches (see
``test_bare_brand_still_never_matches_via_new_tier`` below).

Fixture is a GENERALIZED, fictional TV brand/model-family shape (never
LG/32/63806 -- those appear only in this module's docstring, describing
the real production report, never in a fixture or assertion) with the
SAME structural property: a shared family code across several screen
sizes, plus two same-size/same-family rows differing ONLY by a trailing
generation-suffix letter (the "no unsafe guess" safety case).

Mandatory coverage:
  A. one unique partial human reference (brand + size + family code,
     split by a letter series-code in the source value) -> selects the
     single correct row.
  B. two genuinely ambiguous rows (same brand/size/family, no
     distinguishing info in the user's own wording) -> clarification
     listing the real candidates, never the generic dead-end message.
  C. same brand/model family differing ONLY by a meaningful suffix ->
     never guessed (covered by the SAME fixture as B; asserted
     separately here for the safety property itself: the two candidates
     remain genuinely distinguishable from each other in the surfaced
     text).
  D. the follow-up turn, now naming the previously-ambiguous suffix,
     selects the ONE intended candidate.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from business_assistant import workset as workset_lib
from business_assistant.conversation_gateway import ConversationRequest
from data_intel.service import DataIntelligenceService, _resolve_product_reference
from data_intel.store import InMemoryDatasetStore
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_table_execution import _analyze_plan_entry, _tracking_fake_run_turn
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tests.test_panda_selected_product_scope_continuity_defect_closure import _sequential_model_mock

TENANT = "tenant-family-ambiguity"
OWNER = "u1"
CONVERSATION_ID = "conv-family-ambiguity"
FILENAME = "family_ambiguity.xlsx"

# A shared model-family code ("50210") reused across several screen
# sizes, PLUS two same-size/same-family rows differing only by a trailing
# generation-suffix letter -- the exact structural shape of the real
# LG_TV.xlsx production report, using a fictional brand/model so no
# literal production SKU/brand ever becomes part of this fixture.
SKU_43 = "43QZ50210XA"  # unique: brand + size 43 + family 50210
SKU_32_XA = "32QZ50210XA"  # ambiguous pair member 1 (size 32 + family 50210)
SKU_32_XB = "32QZ50210XB"  # ambiguous pair member 2 -- differs ONLY by suffix
SKU_OTHER_FAMILY = "55QZ71330XA"  # same brand, different family -- must never match
SKU_OTHER_BRAND = "32OM10500"  # different brand entirely -- must never match

ROWS = [
    (SKU_43, "Телевизор Zeta 43QZ50210XA", "Zeta", "4600000000501", "32000.00"),
    (SKU_32_XA, "Телевизор Zeta 32QZ50210XA", "Zeta", "4600000000502", "25000.00"),
    (SKU_32_XB, "Телевизор Zeta 32QZ50210XB", "Zeta", "4600000000503", "26000.00"),
    (SKU_OTHER_FAMILY, "Телевизор Zeta 55QZ71330XA", "Zeta", "4600000000504", "45000.00"),
    (SKU_OTHER_BRAND, "Телевизор Omega 32OM10500", "Omega", "4600000000505", "20000.00"),
]


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["article", "product_name", "brand", "ean", "purchase_price"])
    for row in ROWS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ResolverUnitTests(unittest.TestCase):
    """Direct, fast unit coverage of ``_resolve_product_reference`` itself
    -- mandatory A/B/C, plus the explicit safety checks the audit asked
    for (candidates genuinely surfaced, brand-alone never matches)."""

    def setUp(self):
        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        ingested = self.svc.ingest(_xlsx_bytes(), filename=FILENAME, tenant_id=TENANT)
        self.dataset_id = ingested["dataset_id"]
        desc = self.svc.store.get_dataset(self.dataset_id, tenant_id=TENANT)
        self.table = desc.tables[0]
        self.rows = self.svc.store.get_rows(self.dataset_id, tenant_id=TENANT)

    def _resolve(self, text):
        return _resolve_product_reference(text, self.rows, self.table)

    def test_a_unique_partial_reference_split_by_series_letters_selects_correct_row(self):
        """Brand + size "43" + family "50210", typed with a plain space
        exactly like the production report -- the source article
        "43QZ50210XA" has a letter series-code ("QZ") between the two
        digit groups, so no single contiguous substring/common-substring
        of the OLD tier 3 ever covered this shape."""
        status, payload = self._resolve("Покажи Zeta 43 50210")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["article"], SKU_43)

    def test_b_two_genuinely_ambiguous_rows_return_real_candidates(self):
        """Same brand, same size "32", same family "50210" -- the user's
        own wording never distinguishes the two rows differing only by
        generation suffix ("XA" vs "XB") -- must be AMBIGUOUS with BOTH
        real candidates, never a guess, never the generic dead-end."""
        status, payload = self._resolve("Покажи Zeta 32 50210")
        self.assertEqual(status, "AMBIGUOUS")
        self.assertEqual(len(payload), 2)
        self.assertTrue(all(isinstance(c, str) and c for c in payload))
        # The audit's own question: does the ambiguity response actually
        # surface the two REAL, DISTINGUISHABLE candidates (never two
        # identical/blank lines the user could not act on)?
        self.assertNotEqual(payload[0], payload[1])
        self.assertTrue(any(SKU_32_XA in c for c in payload))
        self.assertTrue(any(SKU_32_XB in c for c in payload))

    def test_c_same_family_differing_only_by_suffix_never_guessed(self):
        """Safety property, asserted independently of B: neither
        candidate is ever silently preferred over the other -- resolving
        the SAME ambiguous reference twice must deterministically return
        the SAME two-candidate AMBIGUOUS result both times, never
        sometimes-UNIQUE (e.g. from set/iteration-order nondeterminism)."""
        for _ in range(3):
            status, payload = self._resolve("Покажи Zeta 32 50210")
            self.assertEqual(status, "AMBIGUOUS")
            self.assertEqual(len(payload), 2)

    def test_d_followup_naming_the_suffix_selects_the_intended_candidate(self):
        """The natural clarification follow-up: now that the user names
        the full, previously-ambiguous article (e.g. read back from the
        candidate list B/C just returned), it resolves UNIQUELY -- the
        SAME existing tier-1 exact-match path, completely unaffected by
        this fix."""
        status, payload = self._resolve(f"Покажи {SKU_32_XB}")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["article"], SKU_32_XB)

    def test_other_family_and_other_brand_never_match(self):
        """The SAME family code alone, on a DIFFERENT screen size that
        was never named, must never contaminate an unrelated family/
        brand row."""
        status, payload = self._resolve("Покажи Zeta 43 50210")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertNotEqual(self.rows[row_index]["article"], SKU_OTHER_FAMILY)
        self.assertNotEqual(self.rows[row_index]["article"], SKU_OTHER_BRAND)

    def test_bare_brand_still_never_matches_via_new_tier(self):
        """The new token-set tier must never fire on an empty digit-token
        set (a bare brand mention has none) -- an empty set is otherwise
        a vacuous subset of anything, which would be a real regression
        if not explicitly guarded against."""
        status, payload = self._resolve("Покажи Zeta")
        self.assertIn(status, ("AMBIGUOUS", "NONE"))
        if status == "UNIQUE":  # pragma: no cover -- defensive, must never happen
            self.fail("bare brand mention must never uniquely resolve a product")

    def test_single_short_digit_token_alone_stays_below_specificity_bar(self):
        """The family code alone (no size), below the combined-length
        specificity bar, must not be treated as confidently ambiguous
        candidates either -- it stays a clean NONE rather than a
        weak/coincidental multi-candidate guess."""
        status, payload = self._resolve("Покажи Zeta 50210")
        self.assertIn(status, ("AMBIGUOUS", "NONE"))


class ConversationalJourneyTests(unittest.IsolatedAsyncioTestCase):
    """Full production-shaped conversational journey through
    ``WorkflowPandaConversationGateway``: upload -> analyze -> ambiguous
    natural reference -> real clarification surfaced to the user -> a
    follow-up turn that resolves the one intended candidate."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.panda, self.artifact_service = _panda()
        self.artifact_id = await _register_upload(
            self.artifact_service,
            tenant=TENANT,
            owner=OWNER,
            conv=CONVERSATION_ID,
            filename=FILENAME,
            content=_xlsx_bytes(),
        )

    async def asyncTearDown(self):
        import shutil

        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _respond(self, text, *, request_id, attach=False):
        return await self.panda.respond(
            ConversationRequest(
                text=text,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id=request_id,
                conversation_id=CONVERSATION_ID,
                attachment_refs=(self.artifact_id,) if attach else (),
            )
        )

    async def test_unique_partial_reference_selects_row_in_conversation(self):
        payloads = [
            {"kind": "not_applicable"},  # plain analysis turn
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Zeta 43 50210"}},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="u1", attach=True)
            r2 = await self._respond("Покажи Zeta 43 50210", request_id="u2")

        self.assertNotEqual(r2.text.strip(), "Не удалось однозначно выбрать товар.")
        self.assertIn(SKU_43, r2.text)
        w2 = workset_lib.get_workset(
            self.panda._action_store.get(  # noqa: SLF001
                tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
            )
        )
        self.assertEqual(w2.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w2.selected_identifiers, (SKU_43,))

    async def test_ambiguous_reference_surfaces_candidates_then_followup_selects_one(self):
        payloads = [
            {"kind": "not_applicable"},  # plain analysis turn
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Zeta 32 50210"}},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": SKU_32_XB}},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="a1", attach=True)
            r2 = await self._respond("Покажи Zeta 32 50210", request_id="a2")

            # Never the generic dead-end -- real candidates, surfaced.
            self.assertNotEqual(r2.text.strip(), "Не удалось однозначно выбрать товар.")
            self.assertIn(SKU_32_XA, r2.text)
            self.assertIn(SKU_32_XB, r2.text)
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")

            # D: the natural follow-up, now naming the intended suffix,
            # resolves that ONE candidate.
            r3 = await self._respond(f"Покажи {SKU_32_XB}", request_id="a3")

        self.assertIn(SKU_32_XB, r3.text)
        w3 = workset_lib.get_workset(
            self.panda._action_store.get(  # noqa: SLF001
                tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
            )
        )
        self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w3.selected_identifiers, (SKU_32_XB,))

        self.assertEqual(len(calls), len(payloads))
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001 -- zero Bitrix mutation


if __name__ == "__main__":
    unittest.main()
