# Image & Product Media Pipeline

## Architecture

One shared media-processing platform (`product_media/`). Channel-specific outputs (website, WB, Ozon, Yandex Market, social, banner, video) are **target profiles** over the same core — not separate engines.

```
Source upload / Content Factory MediaBrief / product facts
              ↓
       MediaIntake (validate MIME/magic/bounds)
              ↓
     Immutable working original (orientation-normalized)
     + source_content_hash of raw bytes
              ↓
     Versioned MediaRecipe (non-executable ops)
              ↓
 Existing Workflow / Batch lanes (media_large / media_bulk)
              ↓
 Media operations: cleanup · background/mask · crop · resize · pad · enhance · infographic
              ↓
 Canonical derivative (parent_version_id lineage)
              ↓
 TargetMediaProfile → website / WB / Ozon / Yandex / social / banner / video
              ↓
 Quality + Rights + Approval
              ↓
 Versioned MediaAssetVersion → artifact ref → Content / SEO / E-commerce / Marketplace
```

## Ownership

Media Pipeline owns processing contracts, recipes, transforms, variants, quality, jobs.  
It does **not** replace Files/Documents, Artifact Store, Tool Gateway, Workflow, Content Factory, or marketplace sync.

## Immutable originals

Ingest stores orientation-normalized canonical bytes as the working original. Raw upload identity is preserved as `source_content_hash`. All transforms create new `MediaAssetVersion` rows with `parent_version_id` — originals are never overwritten in place.

## Rights

`MediaRights` / `rights_status`: UNKNOWN, OWNED, LICENSED, USER_PROVIDED, GENERATED, THIRD_PARTY_RESTRICTED.  
Export paths requiring confirmed rights call `assert_export_rights()` — UNKNOWN/RESTRICTED fail closed.

## Recipes & operations

`product_media/recipes.py` — declarative `OPERATION_REGISTRY` + `MediaRecipe`. No eval/scripts.  
Idempotency: `recipe_identity(source_hash, recipe, target_profile)`.

## Background / mask

`FakeBackgroundRemovalProvider` adapter boundary (deterministic RGBA conversion — does not claim production matte quality).  
`replace_background` / `background_replace` for solid/transparent. Masks via `edit(mask_version_id=...)`.

## Target profiles

`product_media/profiles.py` — versioned `TargetMediaProfile` with `source_of_rules="configurable"`.  
WB / Ozon / Yandex Market share the same `render_for_profile` / `render_marketplace_set` core.

## Infographics

`render_infographic` — fact-locked text from governed `product_facts`. Invented commercial claims rejected (`MEDIA_FACT_UNSUPPORTED`).

## Product video

`VideoRecipe` + `FakeVideoRenderer` — orchestration contract; fake renderer sets `metadata_safe.fake=True` and does not claim real codec encoding.

## Content Factory handoff

`MediaBrief` → `ProductMediaService.generate_from_brief` / `recipe_from_media_brief` → `MediaAssetVersion.artifact_id` → Content `MediaAssetRef`.

## Batch / no-loop

`assert_sync_media_allowed`, `MAX_SYNC_VARIANTS=4`, `bounded_generate(max_attempts, max_quality_retries)`, bulk checkpoint/resume, `cancel_job`.

## How to add an image operation

Register in `OPERATION_REGISTRY`, implement transform helper, wire in `apply_recipe` / service method.

## How to add a target / marketplace profile

Call `register_target_profile(TargetMediaProfile(...))` or add to `_BUILTIN` in `profiles.py` with `source_of_rules="configurable"`.

## How to add a template

Construct `MediaTemplate` (data only — no scripts) and pass to infographic renderer.

## How to add a media provider

Implement provider interface used by service (`generate` / `remove_background` / `edit` / video `render`); wire via Tool Platform / runtime — never call SDKs from business logic.

## How to add a quality check

Extend `analyze_quality` deterministic issues; hard profile violations cannot be overridden by vision models.

## How to add a video renderer

Replace/inject `FakeVideoRenderer` with a governed adapter that returns bytes + mime + rights metadata.

## How to add a product media role

Add role constant and use in `ProductMediaSet` / `validate_set` items.

## Tests

- `tests/test_product_media_block10_platform.py`
- `tests/test_product_media_expansion_closure.py`

See `docs/product-media-bypass-audit.md`.
