# Block 9 — Content Intelligence Architecture Bypass Audit

## Canonical path

```
Agent / Workflow
  → ToolGateway (content.*)
  → ContentIntelToolAdapter
  → ContentIntelligenceService
  → governed research / generator / analytics / optimizer
  → SqliteContentStore + artifacts via fake/governed media adapter
```

## Findings

| Check | Status |
|-------|--------|
| Agent → HTTP/web direct | PASS |
| Agent → model SDK direct | PASS — DeterministicContentGenerator in service |
| Agent → media provider direct | PASS — generate_media uses fake or ToolGateway |
| ContentFactory → KnowledgeStore direct | PASS |
| ContentFactory → memory write direct | PASS |
| ContentFactory → direct publisher | PASS — publication plan only, no publish op |
| Payload tenant trust | PASS — ContentAccessPolicy |
| Heavy sync escape | PASS — assert_sync_content_allowed + planner |
| LLM arithmetic for analytics | PASS — Decimal deterministic analytics |
| Raw content in telemetry | PASS — ContentObservability strips body/content |

## Verdict

No production-reachable bypass paths identified for mandatory Block 9 contracts.
