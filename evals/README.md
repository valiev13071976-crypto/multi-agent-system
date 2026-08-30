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

## Offline promotion governance (PATCH-MR-06)

Full promotion lifecycle is represented **offline** in `evals/promotion.py`:

```text
EVALUATED → CANDIDATE → SHADOW_VALIDATED → CANARY_VALIDATED
         → RELEASE_APPROVED / PRODUCTION_ELIGIBLE
```

- Candidate Policy is a versioned artifact (base + proposed `routing_policy_version`,
  eval suite/run/manifest refs, model/provider profile versions).
- Shadow / Canary are **evidence contracts** (offline). No live traffic mirroring
  and no real canary deploy in this module.
- ReleaseGate PASS is required **after** Shadow + Canary acceptance.
- `PRODUCTION_ELIGIBLE != PRODUCTION_ACTIVE`.
- Production activation remains a **manual/external** step (config/env/code deploy).
  There is no `--apply-production` and ReleaseGate never writes live ModelRouter
  configuration.

Missing latency/cost metrics must be recorded as `unavailable` — never fabricated.

## Notes

- No live provider calls in the core suite.
- No real GitHub writes; fake adapters only.
- Reports/baselines must not contain secrets.
- Does not change the public `/api/analyze` contract.
- Eval / ReleaseGate / Candidate artifacts must not auto-mutate production routing.
