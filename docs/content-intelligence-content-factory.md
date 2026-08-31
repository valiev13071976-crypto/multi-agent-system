# Content Intelligence & Content Factory

## Architecture

One shared platform for research → strategy → content generation → publication planning → analytics → optimization. Reels/Shorts, product content, and SEO content are use cases over the same core — not separate engines.

```
Business / Brand / Product context
              ↓
       Research Request (Tool Gateway → content.research)
              ↓
     Evidence / ResearchResult
              ↓
 Content Intelligence Layer
      ↙       ↓        ↘
Competitors  Trends   Audience/Brand
      ↘       ↓        ↙
       Strategy → Ideas → Brief/Script
              ↓
       Content Generation (copy / script / media spec)
              ↓
    Quality validation / versioning
              ↓
    Content Calendar (PublicationPlan)
              ↓
    Governed publication (side-effect via Tool Platform)
              ↓
    Performance analytics → Feedback → Optimization
              ↓
    Next brief constraints (versioned, no auto-mutation)
```

Heavy workloads: `ContentJob` → existing batch/bulk queue → bounded partitions → checkpoint/resume.

## Platform handoffs

| Layer | Role |
|-------|------|
| Tool Platform | `content.*` tools via `ContentIntelToolAdapter` |
| Data Acquisition | Evidence rows from crawled/search content (untrusted) |
| Files/Documents/Knowledge | Optional enrichment (stub-safe) |
| Excel/Data Intelligence | Bulk product facts for product-card generation |
| Product Media | Image generation from `MediaBrief` (optional) |
| SEO Marketing (`seo_marketing/`) | Technical SEO platform (separate Block 12); content SEO generation here |

## Core contracts (`content_intel/platform_models.py`)

- `BrandProfile`, `AudienceProfile`, `ContentObjective`
- `ResearchEvidence`, `ResearchReport`
- `CompetitorProfile`, `TrendSignal`
- `ContentStrategy`, `ContentIdea`, `ContentHook`, `ContentScript`
- `ContentAssetVersion`, `MediaBrief`, `PublicationPlan`
- `PerformanceAnalytics`, `OptimizationDecision`, `ContentExperiment`

## Lifecycle statuses

`DRAFT` → `VALIDATED` → `APPROVED` → `SCHEDULED` → `PUBLISHED` (publication is governed write; plan-only in factory by default).

## LLM boundary

Deterministic generator used for closure tests. Production may route to Model Router, but:

- **Yes:** synthesis, strategy, ideation, writing, bounded evaluation
- **No:** mass raw-corpus processing, metric calculation, invented product facts, unbounded recursive refinement

Variant cap: `generate_ideas(count)` bounded to max 10.

## No-loop invariant

`optimize()` returns `OptimizationDecision` recommendations only — does not mutate assets, regenerate copy, or alter global prompts/models.

## Bulk / batch

- `LARGE_SYNC_ITEMS=10`, `LARGE_BATCH_ITEMS=50`
- `plan_content_job()` stamps `LANE_BULK` for heavy workloads
- `bulk_generate_product_content()` partitions catalog

## Security

- External content is **untrusted evidence** (`research.py` poison warnings)
- Product facts validated — no invented price (`validation.py`)
- Cross-tenant access fail-closed (`access.py`)
- Publication requires validated assets + timezone

## Reels / Shorts

Use `ContentScript` (hook, beats, on-screen text, CTA, duration estimate) + `MediaBrief` (aspect ratio, scene) + `channel="reels"|"shorts"|"social"`. Not a separate Reels core.

## How to add a channel

Add channel string to strategy `channel_roles` and pass through `GenerationContext.channel`. Channel adapters live in Tool Platform publication layer.

## How to add a content format

Extend `content_type` in `generate_copy()` and validation rules in `ContentValidator`.

## How to add a research source

Pass normalized evidence rows to `research()` / `content.research` tool. Use Data Acquisition for network fetch.

## How to add a quality check

Extend `ContentValidator` with deterministic rules; gate publication via `STATUS_VALIDATED`.

## How to add a publication adapter

Register governed write tool in Tool Platform (CMS/social adapter). Factory produces `PublicationPlan` only.

## How to add an analytics metric

Extend `PerformanceAnalytics` normalization in `analytics.py`; preserve provider + provenance.

## How to add an optimization rule

Extend `OptimizationEngine.decide()` with evidence-gated recommendations.

## Tests

- `tests/test_content_intel_block9_platform.py` — Block 9 closure
- `tests/test_content_intel_expansion_closure.py` — applied expansion closure
- `tests/test_seo_marketing_block12_platform.py` — adjacent SEO platform

See `docs/content-intel-bypass-audit.md`.
