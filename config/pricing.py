import os
from decimal import Decimal, InvalidOperation

from agents.model_profile import PROVIDER_PROFILE_ENV
from finops.models import CURRENCY_USD, BudgetLimits, PriceQuote
from finops.service import InvalidBudgetPolicyError, parse_decimal, parse_unknown_cost_policy


def load_price_quotes() -> dict[tuple[str, str], PriceQuote]:
    quotes = {}
    for provider_id, prefix in PROVIDER_PROFILE_ENV.items():
        input_raw = os.getenv(f"{prefix}_INPUT_PRICE_PER_MILLION")
        output_raw = os.getenv(f"{prefix}_OUTPUT_PRICE_PER_MILLION")
        if not (input_raw and str(input_raw).strip() and output_raw and str(output_raw).strip()):
            continue
        try:
            input_price = Decimal(str(input_raw).strip())
            output_price = Decimal(str(output_raw).strip())
        except InvalidOperation as exc:
            raise InvalidBudgetPolicyError(
                f"Invalid price for {provider_id}: {input_raw!r}/{output_raw!r}"
            ) from exc
        model_id = os.getenv(f"{prefix}_MODEL") or ""
        currency = (os.getenv(f"{prefix}_PRICE_CURRENCY") or CURRENCY_USD).strip() or CURRENCY_USD
        quotes[(provider_id, model_id)] = PriceQuote(
            provider_id=provider_id,
            model_id=model_id,
            input_price_per_million=input_price,
            output_price_per_million=output_price,
            currency=currency,
            enabled=True,
        )
    return quotes


def load_budget_limits() -> BudgetLimits:
    return BudgetLimits(
        per_task=parse_decimal(os.getenv("FINOPS_PER_TASK_LIMIT")),
        per_day=parse_decimal(os.getenv("FINOPS_PER_DAY_LIMIT")),
        per_month=parse_decimal(os.getenv("FINOPS_PER_MONTH_LIMIT")),
        unknown_cost_policy=parse_unknown_cost_policy(
            os.getenv("FINOPS_UNKNOWN_COST_POLICY")
        ),
    )
