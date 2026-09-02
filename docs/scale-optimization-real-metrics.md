# Scale / Optimization Based on Real Metrics

Engineering measurement and recommendation plane for Panda Multi-Agent.

## Principle

```
MEASURE → ATTRIBUTE → LOCATE BOTTLENECK → DEFINE SLO/CAPACITY
→ OPTIMIZE ONLY PROVEN BOTTLENECK → VERIFY → PRESERVE CORRECTNESS
```

Optimization without evidence is forbidden. This block does **not** mutate production infrastructure.

## Architecture reuse

Consumes existing:

- `runtime.capacity_snapshot` / `runtime.alerts`
- `observability.runtime_metrics`
- `task_queue` lanes/pools/admission
- `providers.governor`
- `finops`
- `harness.load_harness` patterns
- Analytics management read-model style API

Does **not** create parallel queue, workflow engine, scheduler, ProviderGovernor, FinOps, ToolGateway, or auth.

## Metric contract

Versioned labels bounded to: environment, service, instance/worker, tenant_bucket, workload_class, queue, operation, provider, model, integration/tool, outcome, error_class, lane, pool.

Forbidden in labels: prompts, bodies, secrets, PII, arbitrary user text.

## Latency attribution

```
TOTAL = admission_wait + queue_wait + worker_wait + workflow_time
      + tool_time + provider_time + persistence_time + response_finalize
```

## Workload classes

INTERACTIVE / NORMAL / BATCH / BACKGROUND — measured separately.

## SLO / Capacity

SLO statuses: HEALTHY | WARNING | BREACHED | INSUFFICIENT_DATA  
Capacity: HEALTHY | NEAR_CAPACITY | SATURATED | OVERLOADED | INSUFFICIENT_DATA  

INSUFFICIENT_DATA is never treated as HEALTHY.

## Bottleneck + scale decisions

Deterministic categories and recommendations (`NO_ACTION`, `SCALE_OUT`, `SHED_LOAD`, …).  
Recommendations do **not** auto-change infrastructure.

Autoscaling signals include minimum window, sample count, cooldown, and hysteresis to prevent flapping.

## Benchmark harness

Deterministic fixture profiles A–L (interactive, mixed, provider 429, retry pressure, noisy tenant, Excel, crawler, persistence). No live traffic. Machine-readable results.

## Before / After

Comparable workloads only. Profile/class mismatch rejected.

## Flags

```
SCALE_OPTIMIZATION_ENGINEERING_READY=true
SCALE_OPTIMIZATION_LIVE_ACTIVE=false
SCALE_OPTIMIZATION_LIVE_VERIFIED=false
```

## Non-goals

Premature DB migration, uncontrolled cache, parallel runtime, production LIVE activation, infrastructure auto-mutation.
