"""Feedback optimization loop — evidence-gated, version-safe."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from content_intel.errors import ContentInsufficientEvidence
from content_intel.platform_models import OPTIMIZATION_PROFILE_VERSION, OptimizationDecision


MIN_SAMPLE_OBSERVATIONS = 3


class OptimizationEngine:
    profile_version = OPTIMIZATION_PROFILE_VERSION
    min_observations = MIN_SAMPLE_OBSERVATIONS

    def decide(
        self,
        *,
        tenant_id: str,
        project_id: str,
        strategy_version_id: str,
        asset_version_ids: tuple[str, ...],
        observation_window: tuple[datetime, datetime],
        metrics: dict,
        idempotency_key: str = "",
    ) -> OptimizationDecision:
        key = idempotency_key or self._idempotency_key(
            tenant_id, strategy_version_id, asset_version_ids, observation_window, metrics
        )
        obs_count = int(metrics.get("observation_count") or 0)
        if obs_count < self.min_observations:
            raise ContentInsufficientEvidence("sparse_sample")

        ctr = metrics.get("ctr") or {}
        if isinstance(ctr, dict) and ctr.get("status") in {"missing", "zero_denominator"}:
            raise ContentInsufficientEvidence("insufficient_ctr_evidence")

        recommended = "iterate_hook_variant"
        if isinstance(ctr, dict) and ctr.get("status") == "ok" and float(ctr.get("ctr") or 0) < 0.01:
            recommended = "revise_cta_and_hook"

        return OptimizationDecision(
            decision_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            strategy_version_id=strategy_version_id,
            asset_version_ids=asset_version_ids,
            observation_window=observation_window,
            hypothesis="Performance below target for bound asset versions",
            recommended_action=recommended,
            confidence_label="low" if obs_count < 10 else "medium",
            limitations=("correlation_not_causation", "no_statistical_significance_claim"),
            idempotency_key=key,
        )

    @staticmethod
    def _idempotency_key(
        tenant_id: str,
        strategy_version_id: str,
        asset_version_ids: tuple[str, ...],
        window: tuple[datetime, datetime],
        metrics: dict,
    ) -> str:
        raw = "|".join(
            [
                tenant_id,
                strategy_version_id,
                ",".join(sorted(asset_version_ids)),
                window[0].isoformat(),
                window[1].isoformat(),
                str(sorted(metrics.items())),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
