from datetime import datetime, timezone
from decimal import Decimal
import unittest

from finops.forecast import forecast_from_usage
from finops.models import UsageRecord


class BudgetForecastTests(unittest.TestCase):
    def test_moving_average_foundation(self):
        day = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
        records = tuple(
            UsageRecord(
                task_id=f"t{i}",
                provider_id="openai",
                model_id="m",
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                estimated_cost=Decimal("2"),
                currency="USD",
                timestamp=day,
            )
            for i in range(5)
        )
        result = forecast_from_usage(records, remaining_budget=Decimal("10"), window_limit=Decimal("100"))
        self.assertEqual(result.estimated_remaining_calls, 5)
        self.assertEqual(result.sample_size, 5)
        self.assertIsNotNone(result.metadata_safe.get("avg_cost"))


if __name__ == "__main__":
    unittest.main()
