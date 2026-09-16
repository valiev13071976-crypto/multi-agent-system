from __future__ import annotations

import json
import subprocess
import unittest

from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from tests.test_panda_canonical_workset_single_data_ownership import (
    FILENAME,
    RETAIL_A,
    RETAIL_B,
    SKU_B,
    TENANT,
    _xlsx_bytes,
)


class ConversationalWorksetSemanticJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        self.dataset_id = self.svc.ingest(
            _xlsx_bytes(), filename=FILENAME, tenant_id=TENANT
        )["dataset_id"]

    async def _execute(self, payload, *, selected=()):
        async def model_call(_prompt):
            return json.dumps(payload)

        return await self.svc.execute_structured_plan_via_model(
            self.dataset_id,
            "natural language is interpreted by the model",
            tenant_id=TENANT,
            model_call=model_call,
            selected_identifiers=selected,
        )

    async def test_selected_continuation_changes_only_selected_row_and_returns_same_card(self):
        out = await self._execute(
            {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "selected"},
                        "column_id": "c6",
                        "operation": "percent_round",
                        "value": "8",
                    }
                ],
            },
            selected=(SKU_B,),
        )
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["result_scope"], "SINGLE")
        self.assertEqual(out["selected_product"]["product_fields"]["sku"], SKU_B)
        rows = self.svc.store.get_rows(out["dataset_id"], tenant_id=TENANT)
        self.assertEqual(str(rows[0]["розница"]), RETAIL_A)
        self.assertNotEqual(str(rows[1]["розница"]), RETAIL_B)
        self.assertEqual(len(out["row_changes"]), 1)

    async def test_explicit_multirow_request_escapes_single_scope(self):
        out = await self._execute(
            {
                "kind": "table_operation",
                "operations": [
                    {"scope": {"kind": "row_range", "start": 0, "end": 3}, "column_id": "c6", "operation": "percent_round", "value": "7"},
                    {"scope": {"kind": "remainder"}, "column_id": "c6", "operation": "percent_round", "value": "15"},
                ],
            },
            selected=(SKU_B,),
        )
        self.assertEqual(out["status"], "OK")
        self.assertNotIn("result_scope", out)
        self.assertEqual(len(out["row_changes"]), 4)

    async def test_ordinal_selection_resolves_current_dataset_not_stale_context(self):
        out = await self._execute(
            {"kind": "product_selection", "selector": {"kind": "ordinal", "value": 2}},
            selected=(SKU_B,),
        )
        self.assertEqual(out["status"], "ROW_FOUND")
        self.assertNotEqual(out["product_fields"]["sku"], SKU_B)
        rows = self.svc.store.get_rows(self.dataset_id, tenant_id=TENANT)
        self.assertEqual(out["row"], {k: v for k, v in rows[2].items() if not k.startswith("__")})

    async def test_russian_english_armenian_share_one_structured_contract(self):
        for value in ("11", "12", "13"):
            out = await self._execute(
                {
                    "kind": "table_operation",
                    "operations": [
                        {"scope": {"kind": "selected"}, "column_id": "c6", "operation": "percent_round", "value": value}
                    ],
                },
                selected=(SKU_B,),
            )
            self.assertEqual(out["result_scope"], "SINGLE")
            self.assertEqual(len(out["row_changes"]), 1)


class ClickableArtifactRenderingTests(unittest.TestCase):
    def test_actual_panda_renderer_creates_authorized_clickable_link(self):
        case = [{
            "name": "workbook",
            "fn": "renderRichText",
            "args": {"text": "[Скачать Excel](/api/v1/business-assistant/artifacts/art-1/view)"},
        }]
        proc = subprocess.run(
            ["node", "tests/frontend/render_probe.js", "."],
            input=json.dumps({"cases": case}),
            text=True,
            capture_output=True,
            check=True,
        )
        rendered = json.loads(proc.stdout)["results"][0]
        anchor = rendered["anchors"][0]
        self.assertEqual(anchor["href"], "/api/v1/business-assistant/artifacts/art-1/view")
        self.assertEqual(anchor["text"], "Скачать Excel")
