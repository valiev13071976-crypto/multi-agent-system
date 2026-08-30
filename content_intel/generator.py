"""Deterministic structured content generator — no vendor SDK."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from content_intel.platform_models import (
    GENERATION_PROFILE_VERSION,
    ContentHook,
    ContentIdea,
    ContentScript,
    ContentStrategy,
    PROVENANCE_CREATIVE,
    PROVENANCE_MODEL,
    STATUS_DRAFT,
    ContentAssetVersion,
)


@dataclass(frozen=True)
class GenerationContext:
    tenant_id: str
    project_id: str
    channel: str
    objective: str
    audience_segments: tuple[str, ...]
    pillars: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    brand_tone: str = "professional"
    product_facts: dict[str, str] | None = None
    forbidden_terms: tuple[str, ...] = ()


class DeterministicContentGenerator:
    """Schema-valid structured output for tests and offline closure."""

    profile_version = GENERATION_PROFILE_VERSION

    def generate_strategy(self, ctx: GenerationContext) -> ContentStrategy:
        sid = str(uuid.uuid4())
        pillars = ctx.pillars or ("education", "trust", "conversion")
        return ContentStrategy(
            strategy_id=sid,
            version_id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            version_num=1,
            pillars=pillars,
            channel_roles={ctx.channel: "primary"},
            messaging_principles=(f"Tone: {ctx.brand_tone}", f"Objective: {ctx.objective}"),
            evidence_refs=ctx.evidence_refs,
            provenance_kind=PROVENANCE_MODEL,
        )

    def generate_ideas(self, ctx: GenerationContext, *, count: int = 3) -> list[ContentIdea]:
        ideas: list[ContentIdea] = []
        for i in range(min(count, 10)):
            concept = f"{ctx.objective} angle {i + 1} for {ctx.channel}"
            ideas.append(
                ContentIdea(
                    idea_id=str(uuid.uuid4()),
                    version_id=str(uuid.uuid4()),
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    channel=ctx.channel,
                    concept=concept,
                    angle=f"segment:{ctx.audience_segments[0] if ctx.audience_segments else 'general'}",
                    evidence_refs=ctx.evidence_refs,
                    version_num=1,
                )
            )
        return ideas

    def generate_hook(self, idea: ContentIdea) -> ContentHook:
        return ContentHook(
            hook_id=str(uuid.uuid4()),
            tenant_id=idea.tenant_id,
            idea_id=idea.idea_id,
            text=f"Hook: {idea.concept[:80]}",
            channel=idea.channel,
        )

    def generate_script(self, idea: ContentIdea, hook: ContentHook) -> ContentScript:
        beats = (hook.text, f"Explain {idea.angle}", "Show value", "Call to action")
        words = sum(len(b.split()) for b in beats)
        duration = max(15, int(words / 2.5))  # ~150 wpm estimate
        return ContentScript(
            script_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            tenant_id=idea.tenant_id,
            idea_id=idea.idea_id,
            hook=hook.text,
            beats=beats,
            on_screen_text=(idea.concept[:40],),
            cta="Learn more",
            estimated_duration_sec=duration,
        )

    def generate_copy(
        self,
        ctx: GenerationContext,
        *,
        content_type: str,
        strategy_version_id: str | None = None,
        idea_id: str | None = None,
    ) -> ContentAssetVersion:
        facts = dict(ctx.product_facts or {})
        body_parts = [f"{content_type} for {ctx.channel}: {ctx.objective}"]
        missing: list[str] = []
        used: dict[str, str] = {}
        for key in ("price", "stock", "warranty", "sku"):
            if key in facts:
                used[key] = facts[key]
                body_parts.append(f"{key}: {facts[key]}")
            elif content_type.startswith("product"):
                missing.append(key)
        body = " | ".join(body_parts)
        for term in ctx.forbidden_terms:
            if term.lower() in body.lower():
                body = body.replace(term, "[REDACTED]")
        return ContentAssetVersion(
            asset_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            content_type=content_type,
            channel=ctx.channel,
            body=body,
            status=STATUS_DRAFT,
            version_num=1,
            strategy_version_id=strategy_version_id,
            idea_id=idea_id,
            product_facts_used=used,
            missing_facts=tuple(missing),
            provenance_kind=PROVENANCE_CREATIVE,
        )

    @staticmethod
    def dedupe_key(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
