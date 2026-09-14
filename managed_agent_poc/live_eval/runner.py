"""Executes every scenario in ``scenarios.py`` against the REAL managed-
agent turn path (``ManagedAgentPOC.run_turn`` with no
``test_scripted_plan``) -- never ``ScriptedModel``. Each ``Scenario`` is
its own isolated conversation: fresh dataset/session/state SQLite files,
so scenarios never leak state into each other.

Raises ``ManagedAgentPocNoApiKeyError`` immediately, before any
subprocess/model call, if ``OPENAI_API_KEY`` is absent -- see
``managed_agent_poc/scripts/run_live_eval.py`` for the STOP behavior
this is meant to trigger.
"""

from __future__ import annotations

import os
import tempfile

from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.live_eval import fixture
from managed_agent_poc.live_eval.scenarios import Scenario, all_scenarios
from managed_agent_poc.live_eval.scoring import TurnResult, score_turn


def run_scenario(scenario: Scenario, *, work_dir: str, fixture_path: str) -> list[TurnResult]:
    scenario_dir = os.path.join(work_dir, scenario.scenario_id)
    os.makedirs(scenario_dir, exist_ok=True)
    poc = ManagedAgentPOC(
        dataset_store_path=os.path.join(scenario_dir, "dataset.sqlite3"),
        session_db_path=os.path.join(scenario_dir, "session.sqlite3"),
        state_store_path=os.path.join(scenario_dir, "state.sqlite3"),
    )

    dataset_id = ""
    prior_identifier = ""
    results: list[TurnResult] = []
    for idx, turn in enumerate(scenario.turns):
        result = poc.run_turn(
            text=turn.text,
            tenant_id="tenant-live-eval",
            conversation_id=scenario.scenario_id,
            dataset_id=dataset_id,
            artifact_bytes_path=fixture_path if turn.attach else "",
            artifact_filename=fixture.FILENAME if turn.attach else "",
        )
        dataset_id = result.dataset_id or dataset_id

        if result.status != "COMPLETED":
            results.append(
                TurnResult(
                    scenario_id=scenario.scenario_id,
                    group=scenario.group,
                    turn_index=idx,
                    text=turn.text,
                    expected=turn.expected or "(none/refusal-expected)",
                    selected_tool="(subprocess error)",
                    tool_arguments=None,
                    final_output="",
                    current_identifier=prior_identifier,
                    prior_identifier=prior_identifier,
                    pass_=False,
                    reason=f"subprocess/model call failed: {result.error}",
                )
            )
            break

        turn_result = score_turn(
            scenario_id=scenario.scenario_id,
            group=scenario.group,
            turn_index=idx,
            text=turn.text,
            expected=turn.expected,
            expected_identifier=turn.expected_identifier,
            prior_identifier=prior_identifier,
            result=result,
        )
        results.append(turn_result)
        prior_identifier = result.current_identifier or prior_identifier

    return results


def run_all(*, work_dir: str | None = None) -> list[TurnResult]:
    """Runs every scenario. Caller (``scripts/run_live_eval.py``) is
    responsible for checking ``ManagedAgentPOC.real_model_available()``
    BEFORE calling this -- this function does not re-check, so it must
    never be called from anywhere that skips that gate."""
    work_dir = work_dir or tempfile.mkdtemp(prefix="panda_managed_agent_live_eval_")
    fixture_path = os.path.join(work_dir, fixture.FILENAME)
    with open(fixture_path, "wb") as fh:
        fh.write(fixture.xlsx_bytes())

    all_results: list[TurnResult] = []
    for scenario in all_scenarios():
        all_results.extend(run_scenario(scenario, work_dir=work_dir, fixture_path=fixture_path))
    return all_results
