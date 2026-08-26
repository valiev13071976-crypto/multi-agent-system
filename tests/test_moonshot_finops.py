"""Moonshot FinOps / pricing / usage tests."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from agents.moonshot_fake import FakeMoonshotProvider
from config.pricing import load_price_quotes
from finops.models import CURRENCY_USD, UsageRecord, utc_now
from finops.service import FinOpsService


class MoonshotFinopsTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_price_not_zero(self):
        with patch.dict(
            os.environ,
            {
                "MOONSHOT_PRICE_STATUS": "unknown",
                "MOONSHOT_INPUT_PRICE_PER_MILLION": "1.0",
                "MOONSHOT_OUTPUT_PRICE_PER_MILLION": "2.0",
                "MOONSHOT_DEFAULT_MODEL": "kimi-k3",
            },
            clear=False,
        ):
            quotes = load_price_quotes()
            self.assertNotIn(("moonshot", "kimi-k3"), quotes)
        finops = FinOpsService(prices={}, limits=None)
        cost = finops.estimate("moonshot", "kimi-k3", 100, 50)
        self.assertIsNone(cost)
        self.assertNotEqual(cost, Decimal("0"))

    def test_verified_price_loads(self):
        with patch.dict(
            os.environ,
            {
                "MOONSHOT_PRICE_STATUS": "verified",
                "MOONSHOT_INPUT_PRICE_PER_MILLION": "3.0",
                "MOONSHOT_OUTPUT_PRICE_PER_MILLION": "15.0",
                "MOONSHOT_DEFAULT_MODEL": "kimi-k3",
            },
            clear=False,
        ):
            quotes = load_price_quotes()
            q = quotes.get(("moonshot", "kimi-k3"))
            self.assertIsNotNone(q)
            self.assertEqual(q.input_price_per_million, Decimal("3.0"))

    async def test_exact_usage_recorded(self):
        fake = FakeMoonshotProvider(model="kimi-k3", input_tokens=21, output_tokens=9)
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
