# P10 Evals & Versioning

Offline deterministic regression harness for prompts, roles, tools, policies,
validators/judge, workflow, ToolGateway, Autonomy/HITL/permit, and `/api/analyze`
compatibility.

## Run core evals

```bash
python -m evals.run --suite core --no-network
```

Network is **disabled by default**. Cases with `requires_network=true` are skipped
with reason `network_eval_disabled`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Release gate **PASS** |
| 1 | Release gate **FAIL** |
| 2 | **BLOCKED** / config error (unknown suite, bad baseline, infra) |

## Baseline

Optional JSON fixture under `evals/baselines/`. Compare with:

```bash
python -m evals.run --suite core --baseline evals/baselines/core_v1.json --no-network
```

Removed critical cases vs baseline → gate **FAIL** (`critical_eval_case_removed`).

## Version bump rule

Same `version` + different `content_hash` → **FAIL**
(`artifact_changed_without_version_bump`).

Bump the version when semantic content changes. Hash alone is not identity.

## Release gate (summary)

FAIL if: any critical case fails, pass rate below suite threshold, version/hash
mismatch, compatibility failure, security regression, deterministic error, or
critical case removed from suite vs baseline.

BLOCKED if the suite cannot run due to local infrastructure/config.

## Notes

- No live provider calls in the core suite.
- No real GitHub writes; fake adapters only.
- Reports/baselines must not contain secrets.
- Does not change the public `/api/analyze` contract.
