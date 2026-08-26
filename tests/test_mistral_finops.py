"""Mistral FinOps / pricing / usage tests."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from agents.mistral_fake import FakeMistralProvider
from config.pricing import load_price_quotes
from finops.models import CURRENCY_USD, UsageRecord, utc_now
from finops.service import FinOpsService


class MistralFinopsTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_price_not_zero(self):
        with patch.dict(
            os.environ,
            {
                "MISTRAL_PRICE_STATUS": "unknown",
                "MISTRAL_INPUT_PRICE_PER_MILLION": "1.0",
                "MISTRAL_OUTPUT_PRICE_PER_MILLION": "2.0",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
            },
            clear=False,
        ):
            quotes = load_price_quotes()
            self.assertNotIn(("mistral", "mistral-large-latest"), quotes)
        finops = FinOpsService(prices={}, limits=None)
        cost = finops.estimate("mistral", "mistral-large-latest", 100, 50)
        self.assertIsNone(cost)
        self.assertNotEqual(cost, Decimal("0"))

    def test_verified_price_loads(self):
        with patch.dict(
            os.environ,
            {
                "MISTRAL_PRICE_STATUS": "verified",
                "MISTRAL_INPUT_PRICE_PER_MILLION": "0.5",
                "MISTRAL_OUTPUT_PRICE_PER_MILLION": "1.5",
                "MISTRAL_DEFAULT_MODEL": "mistral-large-latest",
            },
            clear=False,
        ):
            quotes = load_price_quotes()
            q = quotes.get(("mistral", "mistral-large-latest"))
            self.assertIsNotNone(q)
            self.assertEqual(q.input_price_per_million, Decimal("0.5"))

    async def test_exact_usage_recorded(self):
        fake = FakeMistralProvider(
            model="mistral-large-latest", input_tokens=21, output_tokens=9
        )
        result = await fake.run("x")
        finops = FinOpsService(prices={}, limits=None)
        record = UsageRecord(
            task_id="t1",
            provider_id=result.provider_id,
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost=None,
            currency=CURRENCY_USD,
            timestamp=utc_now(),
        )
        finops.record_usage(record)
        stored = finops._store.records_for_task("t1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].input_tokens, 21)
        self.assertEqual(stored[0].output_tokens, 9)
        self.assertIsNone(stored[0].estimated_cost)


if __name__ == "__main__":
    unittest.main()
