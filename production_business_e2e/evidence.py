"""E2E evidence helpers."""

from __future__ import annotations

from typing import Any

from production_business_e2e.models import E2EEvidence


def assert_no_secrets(payload: Any, *, forbidden: tuple[str, ...] = ("SUPERSECRET", "sk-", "api_key=", "Bearer ")) -> None:
    text = str(payload)
    for token in forbidden:
        if token in text:
            raise AssertionError(f"secret_leak:{token}")


def assert_fixture_mode(evidence: E2EEvidence) -> None:
    if evidence.live_active:
        raise AssertionError("live_active_in_e2e")
    if not evidence.fixture_mode:
        raise AssertionError("fixture_mode_required")


def summarize(results: list[E2EEvidence]) -> dict[str, Any]:
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.status == "PASS"),
        "failed": sum(1 for r in results if r.status != "PASS"),
        "scenarios": [r.to_dict() for r in results],
    }
