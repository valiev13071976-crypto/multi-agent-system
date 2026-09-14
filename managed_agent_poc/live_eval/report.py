"""Renders a compact evaluation table + accuracy summary from
``TurnResult`` objects. No chain-of-thought is ever captured or
rendered (the SDK's own ``final_output``/tool call trace only); no
secret is ever included."""

from __future__ import annotations

from managed_agent_poc.live_eval.scoring import AccuracyReport, TurnResult


def render_markdown(results: list[TurnResult], accuracy: AccuracyReport, *, real_model: str, real_calls: int) -> str:
    lines = []
    lines.append(f"# Managed Agent POC -- Live Semantic Evaluation\n")
    lines.append(f"Model: `{real_model}`  |  Real model calls: {real_calls}\n")
    lines.append("| scenario | turn | text | expected | selected_tool | current_id | pass | reason |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        text = r.text.replace("|", "\\|")
        reason = r.reason.replace("|", "\\|")
        mark = "PASS" if r.pass_ else "FAIL"
        lines.append(f"| {r.scenario_id} | {r.turn_index} | {text} | {r.expected} | {r.selected_tool} | {r.current_identifier} | {mark} | {reason} |")

    lines.append("")
    lines.append("## Accuracy")
    lines.append(f"- FIRST-TURN TOOL SELECTION ACCURACY: {accuracy.first_turn_accuracy:.1%} (n={accuracy.counts['first_turn']})")
    lines.append(f"- MULTI-TURN TOOL SELECTION ACCURACY: {accuracy.multi_turn_accuracy:.1%} (n={accuracy.counts['multi_turn']})")
    lines.append(f"- STATE CONTINUITY ACCURACY (mandatory scenario): {accuracy.state_continuity_accuracy:.1%} (n={accuracy.counts['state_continuity']})")
    lines.append(f"- UNSAFE/MISSING-TOOL REFUSAL ACCURACY: {accuracy.safety_refusal_accuracy:.1%} (n={accuracy.counts['safety']})")
    lines.append(f"- OVERALL SEMANTIC ACCURACY: {accuracy.overall_accuracy:.1%} (n={accuracy.counts['total']})")

    failures = [r for r in results if not r.pass_]
    lines.append("")
    lines.append(f"## Failures ({len(failures)})")
    for r in failures:
        lines.append(f"- [{r.scenario_id} turn {r.turn_index}] {r.text!r} -> {r.reason}")

    return "\n".join(lines)
