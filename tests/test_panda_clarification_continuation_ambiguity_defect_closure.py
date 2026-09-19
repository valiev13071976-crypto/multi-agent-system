"""PANDA -- clarification-continuation defect closure (post PR #100).

REAL PRODUCTION FAILURE

Turn 1: a natural product reference finds multiple plausible candidates
and Panda returns a clarification list ("1. Candidate A / 2. Candidate
B"). Turn 2: the user replies naturally with a discriminator ("второй",
a unique suffix/token from Candidate B, a shortened distinguishing
fragment). Production behavior treated turn 2 as a BRAND-NEW product
reference against the ENTIRE Workset instead of resolving it against the
candidates just presented -- a short discriminator that uniquely
distinguished Candidate B inside that tiny 2-row set (e.g. "ARUG") failed
whole-dataset resolution (too short/no brand mentioned) and produced the
generic dead-end "Не удалось однозначно выбрать товар." even though the
user had just been shown exactly which two rows were in play.

READ-ONLY TRACE (before this fix):

- ``AMBIGUOUS`` candidates were produced by ``DataIntelligenceService.
  execute_structured_plan_via_model`` (the ``ModelProductSelection``
  handler, via ``_resolve_product_reference``) as a PLAIN LIST OF HUMAN-
  READABLE STRINGS ONLY -- no row index, no column, no canonical
  identifier value ever left that function call.
- ``business_assistant.conversation_gateway.
  _maybe_execute_canonical_table_operation``'s ``status == "AMBIGUOUS"``
  branch rendered those strings via ``format_tool_user_text`` and
  returned -- ``mark_executed(..., failed=True)`` was called, but
  NOTHING about the candidate set was ever written to
  ``task.parameters``/the Workset. ``business_assistant.workset.
  apply_tool_result`` explicitly documents ``AMBIGUOUS`` as one of the
  statuses where "nothing conclusive happened this turn" and scope/
  selection are left untouched.
- The NEXT turn therefore started with ZERO memory of the candidate set:
  its own text was handed to the SAME ``execute_structured_plan_via_
  model`` call, which re-interprets it from scratch against the WHOLE
  current dataset via ``_resolve_product_reference`` -- never against
  the 2 (or N) rows the user was just shown. A short, low-specificity
  discriminator that easily distinguishes 2 known rows is exactly the
  kind of reference that fails the (correctly strict) whole-dataset
  resolver's specificity bar (brand mention + minimum combined token
  length) -- hence the exact production symptom.

THE GAP, PRECISELY: an AMBIGUOUS result's candidate identity was never
persisted anywhere durable, so there was no "previous candidate set" for
the next turn to even consult -- clarification-continuation was not a
bug in the matching logic, it was a MISSING PERSISTENCE STEP.

FIX (additive, no new store/agent/router/dataset):

1. ``data_intel.service._resolve_product_reference_candidates`` -- a
   rich-candidate wrapper around the SAME tier 1/2/3 engine (now
   factored into ``_resolve_product_reference_full``) that keeps
   ``_resolve_product_reference``'s existing string-only contract 100%
   unchanged for every existing caller/test, while ALSO exposing each
   candidate's ``row_index``/``column``/``value``/``summary``.
2. ``execute_structured_plan_via_model``'s ``AMBIGUOUS`` response now
   ALSO carries ``candidate_rows`` (that rich list) alongside the
   existing plain-string ``candidates``/``message_safe``.
3. ``business_assistant.conversation_gateway.
   _maybe_execute_canonical_table_operation`` persists that
   ``candidate_rows`` list on the EXISTING ``ActiveTask.parameters``
   (key ``pending_product_ambiguity``, alongside the SAME ``workset``
   key ``business_assistant.workset`` already uses) -- no new store.
4. On the VERY NEXT turn, that method resolves the turn's text AGAINST
   this persisted candidate set FIRST, via ``data_intel.service.
   resolve_ambiguity_clarification`` (ordinal word/number, unique
   suffix/token, shortened fragment, or exact article/SKU/EAN -- all
   generic, data-driven, and NEVER re-searching the whole dataset for
   this check). Exactly one match -> re-expressed as a direct reference
   to that candidate's own canonical identifier and run through the
   SAME deterministic ``ROW_FOUND`` path every other explicit selection
   already uses (Workset -> SINGLE, ambiguity state cleared). 2+ matches
   -> the REDUCED candidate list is persisted and shown again. 0 matches
   -> a SEPARATE, single whole-Workset attempt with the turn's own
   original text decides between "the user started a genuinely
   different product request" (a real ``ROW_FOUND``/``OK`` -- pending
   ambiguity is abandoned) and "still nothing conclusive" (the ORIGINAL
   candidate set is kept alive and re-asked, never lost, never
   re-searched a second way).

Fixture is a GENERALIZED, fictional brand/model shape (brand "Nova",
unrelated fictional model codes) -- no real production brand/SKU/model
ever appears here; concrete identifiers below are test data only.

Mandatory coverage (A-G, all against a live ``WorkflowPandaConversation
Gateway`` -- never a synthetic call directly into the persistence
helper):
  A. ambiguous reference -> 2 candidates -> "второй" -> Candidate B.
  B. ambiguous reference -> unique suffix/token -> correct candidate.
  C. ambiguous reference (3 candidates) -> clarification still matches
     2 of them -> REDUCED candidate list, never a guess.
  D. clarification matches none of the candidates AND nothing else in
     the dataset either -> no guess, original candidate set retained.
  E. after successful clarification: a plain field-query follow-up about
     the SAME just-selected product answers correctly.
  F. after successful clarification: a Bitrix write-plan preview for the
     SAME just-selected product renders correctly, zero write.
  G. a new turn that clearly, uniquely names a DIFFERENT product (never
     part of the pending candidate set) is not hijacked by the stale
     pending ambiguity -- it selects the NEW product instead.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from business_assistant import workset as workset_lib
from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_table_execution import _analyze_plan_entry, _tracking_fake_run_turn
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tests.test_panda_selected_product_scope_continuity_defect_closure import _sequential_model_mock

TENANT = "tenant-clarification-continuation"
OWNER = "u1"
CONVERSATION_ID = "conv-clarification-continuation"
FILENAME = "clarification_continuation.xlsx"

# Group 1 (used by A/B/D/E/F/G): shared family digits "71340" + size "50",
# the two rows below distinguishable ONLY by a trailing, unrelated
# fictional gemstone-name suffix -- the SAME structural shape ("a human
# names only part of a longer code") as the real production defect this
# closes, never the real production brand/model itself.
SKU_UNIQUE = "80QX71340RUBY"  # different size (80) -- unambiguous on its own
SKU_A = "50QX71340RUBY"  # ambiguous pair member 1
SKU_B = "50QX71340JADE"  # ambiguous pair member 2 -- unique suffix "JADE"
SKU_OTHER_FAMILY = "45QX99900RUBY"  # same brand, different size/family -- must never match
SKU_OTHER_BRAND = "50VX71340RUBY"  # different brand entirely -- must never match

# Group 2 (used by C only): 3-way ambiguity, 2 of the 3 share a
# distinguishing name fragment ("Edition Plus") the 3rd does not.
SKU_P = "60QX83500RUBY"
SKU_Q = "60QX83500JADE"
SKU_R = "60QX83500PEARL"

# A wholly separate, uniquely-identifiable product (used by G only) --
# never part of any ambiguous group above.
SKU_NEW = "90ZX44700EMBER"

ROWS = [
    (SKU_UNIQUE, "Телевизор Nova 80QX71340RUBY", "Nova", "4600000001001", "40000.00"),
    (SKU_A, "Телевизор Nova 50QX71340RUBY", "Nova", "4600000001002", "25000.00"),
    (SKU_B, "Телевизор Nova 50QX71340JADE", "Nova", "4600000001003", "26000.00"),
    (SKU_OTHER_FAMILY, "Телевизор Nova 45QX99900RUBY", "Nova", "4600000001004", "27000.00"),
    (SKU_OTHER_BRAND, "Телевизор Vex 50VX71340RUBY", "Vex", "4600000001005", "28000.00"),
    (SKU_P, "Телевизор Nova 60QX83500RUBY (Edition Plus)", "Nova", "4600000001006", "31000.00"),
    (SKU_Q, "Телевизор Nova 60QX83500JADE (Edition Plus)", "Nova", "4600000001007", "32000.00"),
    (SKU_R, "Телевизор Nova 60QX83500PEARL (Edition Base)", "Nova", "4600000001008", "29000.00"),
    (SKU_NEW, "Телевизор Nova 90ZX44700EMBER", "Nova", "4600000001009", "50000.00"),
]

# Column order: article, product_name, brand, ean, purchase_price.
PURCHASE_PRICE_COLUMN_ID = "c4"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["article", "product_name", "brand", "ean", "purchase_price"])
    for row in ROWS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ClarificationContinuationJourneyTests(unittest.IsolatedAsyncioTestCase):
    """Full production-shaped conversational journeys through
    ``WorkflowPandaConversationGateway`` -- upload -> analyze -> ambiguous
    natural reference -> real clarification surfaced to the user ->
    natural follow-up resolved AGAINST that exact candidate set."""

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

    def _workset(self):
        return workset_lib.get_workset(
            self.panda._action_store.get(  # noqa: SLF001
                tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
            )
        )

    def _pending_ambiguity(self):
        task = self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
        )
        return task.parameters.get("pending_product_ambiguity") if task is not None else None

    async def _upload_and_trigger_group1_ambiguity(self, model_patches):
        """Turn 1 (attach+analyze) then turn 2 (the ambiguous reference
        that surfaces Candidate A/Candidate B). Returns the AMBIGUOUS
        ``ConversationResult`` for turn 2."""
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])
        with model_patches[0], model_patches[1], mock.patch.object(
            ManagedAgentPOC, "run_turn", new=fake_run_turn
        ):
            await self._respond("Проанализируй этот прайс.", request_id="t1", attach=True)
            r2 = await self._respond("Покажи Nova 50 71340", request_id="t2")
        return r2

    async def test_a_ordinal_second_selects_candidate_b(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # Turn 3's own natural-language ordinal reply resolves
            # AGAINST the persisted candidates via a pure, deterministic
            # data match (row_canonical_identifier) -- never a model
            # call at all, so this queue only ever needs the 2 payloads
            # above.
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="a1", attach=True)
            r2 = await self._respond("Покажи Nova 50 71340", request_id="a2")
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
            self.assertIn(SKU_A, r2.text)
            self.assertIn(SKU_B, r2.text)
            pending = self._pending_ambiguity()
            self.assertIsNotNone(pending)
            self.assertEqual(len(pending.get("candidates") or []), 2)

            # Turn 3: a bare ordinal ("second") resolves AGAINST the
            # candidate set the previous turn just showed -- never a
            # fresh whole-dataset search.
            r3 = await self._respond("Покажи второй", request_id="a3")

        self.assertIn(SKU_B, r3.text)
        self.assertNotIn(SKU_A, r3.text)
        self.assertEqual(r3.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
        w3 = self._workset()
        self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w3.selected_identifiers, (SKU_B,))
        self.assertIsNone(self._pending_ambiguity())

    async def test_b_unique_suffix_token_selects_candidate(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # Turn 3's own unique-token reply resolves AGAINST the
            # persisted candidates via a pure, deterministic data match,
            # then re-expresses as that row's own canonical SKU -- never
            # a model call at all, so this queue only ever needs the 2
            # payloads above.
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="b1", attach=True)
            r2 = await self._respond("Покажи Nova 50 71340", request_id="b2")
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")

            # Turn 3: a SHORT, unique suffix/token from Candidate B's own
            # identifier -- never resolved against the whole dataset
            # (which is exactly why the real production report failed:
            # too short/no brand mentioned for the whole-dataset bar).
            r3 = await self._respond("JADE", request_id="b3")

        self.assertNotEqual(r3.text.strip(), "Не удалось однозначно выбрать товар.")
        self.assertIn(SKU_B, r3.text)
        self.assertNotIn(SKU_A, r3.text)
        w3 = self._workset()
        self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w3.selected_identifiers, (SKU_B,))
        self.assertIsNone(self._pending_ambiguity())

    async def test_c_still_ambiguous_reduced_candidate_list(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 60 83500"}},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="c1", attach=True)
            r2 = await self._respond("Покажи Nova 60 83500", request_id="c2")
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
            self.assertIn(SKU_P, r2.text)
            self.assertIn(SKU_Q, r2.text)
            self.assertIn(SKU_R, r2.text)
            pending_before = self._pending_ambiguity()
            self.assertEqual(len(pending_before.get("candidates") or []), 3)

            # Turn 3: names a fragment shared by 2 of the 3 candidates
            # ("Edition Plus" -- P and Q, not R) -- still ambiguous, but
            # the REDUCED list is shown, never a guess between P/Q.
            r3 = await self._respond("Edition Plus", request_id="c3")

        self.assertEqual(r3.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
        self.assertIn(SKU_P, r3.text)
        self.assertIn(SKU_Q, r3.text)
        self.assertNotIn(SKU_R, r3.text)
        pending_after = self._pending_ambiguity()
        self.assertIsNotNone(pending_after)
        self.assertEqual(len(pending_after.get("candidates") or []), 2)
        w3 = self._workset()
        self.assertNotEqual(w3.scope, workset_lib.SCOPE_SINGLE)

    async def test_d_no_match_retains_original_candidate_set(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # The follow-up turn's OWN whole-Workset attempt: genuinely
            # nothing conclusive (not a product reference at all).
            {"kind": "not_applicable"},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="d1", attach=True)
            r2 = await self._respond("Покажи Nova 50 71340", request_id="d2")
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
            pending_before = self._pending_ambiguity()

            # Turn 3: matches neither candidate, and nothing else in the
            # dataset either -- no guess, and the ORIGINAL candidate set
            # (A and B) must survive completely unchanged.
            r3 = await self._respond("не разбираю, повтори", request_id="d3")

        self.assertEqual(r3.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
        self.assertIn(SKU_A, r3.text)
        self.assertIn(SKU_B, r3.text)
        pending_after = self._pending_ambiguity()
        self.assertEqual(pending_after, pending_before)
        w3 = self._workset()
        self.assertNotEqual(w3.scope, workset_lib.SCOPE_SINGLE)

    async def test_e_pronoun_followup_works_after_selection(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # Turn 3 (clarification "JADE") resolves deterministically
            # against the persisted candidates -- never a model call.
            {"kind": "field_query", "column_id": PURCHASE_PRICE_COLUMN_ID, "field_label": "закупочная цена"},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="e1", attach=True)
            await self._respond("Покажи Nova 50 71340", request_id="e2")
            r3 = await self._respond("JADE", request_id="e3")
            self.assertIn(SKU_B, r3.text)

            # Turn 4: an ordinary pronoun-style follow-up about the
            # product just selected via clarification -- must answer
            # from THAT SAME row, never a stale/previous one.
            r4 = await self._respond("а сколько он стоит по закупке?", request_id="e4")

        self.assertEqual(r4.metadata.get("action_decision"), "FIELD_QUERY")
        self.assertIn("26000", r4.text)
        w4 = self._workset()
        self.assertEqual(w4.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w4.selected_identifiers, (SKU_B,))

    async def test_f_bitrix_preview_after_selection_zero_write(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # Turn 3 (clarification "JADE") resolves deterministically
            # against the persisted candidates -- never a model call.
            {"kind": "write_plan_query"},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="f1", attach=True)
            await self._respond("Покажи Nova 50 71340", request_id="f2")
            r3 = await self._respond("JADE", request_id="f3")
            self.assertIn(SKU_B, r3.text)

            # Turn 4: ask for the Bitrix write-plan preview of the SAME
            # just-selected product, in the SAME conversation.
            r4 = await self._respond("покажи что уйдёт в Bitrix", request_id="f4")

        self.assertIn(SKU_B, r4.text)
        self.assertIn("ничего в bitrix не записано", r4.text.lower())
        # Zero write: no real Bitrix bridge configured for this journey.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001

    async def test_g_new_unrelated_request_not_hijacked_by_pending_ambiguity(self):
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 50 71340"}},
            # Turn 3's OWN whole-Workset attempt: a clean, unique
            # reference to a WHOLLY DIFFERENT product, never part of the
            # pending A/B candidate set.
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "Nova 90 44700"}},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, _ = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="g1", attach=True)
            r2 = await self._respond("Покажи Nova 50 71340", request_id="g2")
            self.assertEqual(r2.metadata.get("action_decision"), "AMBIGUOUS_PRODUCT_REFERENCE")
            self.assertIsNotNone(self._pending_ambiguity())

            # Turn 3: a genuinely different, uniquely-identified product
            # -- must select IT, never be blocked/hijacked by the stale
            # A/B pending ambiguity from turn 2.
            r3 = await self._respond("Покажи Nova 90 44700", request_id="g3")

        self.assertEqual(r3.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
        self.assertIn(SKU_NEW, r3.text)
        w3 = self._workset()
        self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w3.selected_identifiers, (SKU_NEW,))
        self.assertIsNone(self._pending_ambiguity())


if __name__ == "__main__":
    unittest.main()
