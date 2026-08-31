# SEO & Digital Marketing Platform

## Architecture

One shared platform (`seo_marketing/`). `SeoSite` is the canonical SEO project identity. Content generation is delegated to Content Factory; crawling uses Data Acquisition; CMS writes use Tool Platform side effects.

```
SeoSite (SEOProject)
        ↓
 Evidence (crawl snapshot / GSC / analytics / SERP fixtures)
        ↓
 Keywords → SemanticCore (versioned) → Clusters (stable IDs) → Intent → Page mapping
        ↓
 Technical / On-page / Internal links / CWV / Competitors / Rank
        ↓
 Opportunities → Recommendations → SEOActionPlan
        ↓
 Content Factory brief  |  Governed CMS write (HITL/idempotency)
        ↓
 Change event → later observation → SEOLearningSignal (finite, no global mutation)
```

## Ownership

Owns keyword intelligence, technical SEO analysis, GSC/analytics normalization, recommendations, feedback.  
Does **not** replace crawler, Content Factory, CMS, ads platforms, or Tool Gateway.

## Semantic core

`build_semantic_core()` — versioned `SemanticCore`; deterministic clustering with stable `cl_<hash>` IDs. Large sets use batch jobs — no whole-corpus LLM.

## Content Factory handoff

`create_content_brief()` → `SEOContentBrief` → `content_factory_context` (`delegate_generation_to=content_intel`). Fact lock rejects invented commercial claims.

## Technical SEO

Snapshot analyzers: indexability, canonical, duplicates, orphans, H1, thin (page-type-aware), sitemap, robots.txt, structured data, internal-link recommendations (recommendation-only).

## CWV / Performance

Versioned `CWVBudget` LAB vs FIELD — mixing raises `SEO_CONFLICT`. Fake performance provider for closure.

## Rank / SERP

`RankObservation` / `SERPObservation` with OBSERVED vs NOT_AVAILABLE. History never overwritten; deltas via `rank_history_deltas`. Fake rank provider is fixture-only.

## Feedback / no-loop

`feedback_cycle()` returns one learning signal + bounded next recommendation; `global_mutation=False`. Optimization decide path never auto-mutates prompts/CMS.

## Jobs

Keyword/technical/bulk-meta jobs with checkpoint/partial; `cancel_job` → `cancelled` + `SEO_CANCELLED`.

## How to add a keyword source

Pass `source=` into `keyword_research` / `normalize_keywords`; preserve provenance trust levels.

## How to add a search/rank provider

Implement provider with `check()` returning position or unavailable; wire into `record_rank(use_provider=True)`.

## How to add Search Console / Analytics / Performance provider

Replace fake providers in `SearchConsoleService` / `AnalyticsService` / runtime — never call vendor SDKs from SEO business logic.

## How to add a technical SEO rule

Extend `analyze_technical_snapshot` with deterministic issue codes + severity.

## How to add Content Factory handoff

Extend `build_seo_content_brief` / `brief_to_content_factory_context`.

## How to add CMS write adapter

Register side-effect tool (see `seo_marketing/side_effect.py`); never write from analyzer path.

## How to add feedback metric

Pass baseline/post metrics into `feedback_cycle` / `optimization_measure`.

## Tests

- `tests/test_seo_marketing_block12_platform.py`
- `tests/test_seo_marketing_expansion_closure.py`

See `docs/seo-bypass-audit.md`.
