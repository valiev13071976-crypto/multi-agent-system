"""Governed media operation registry and non-executable recipes."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Mapping

from product_media.errors import MEDIA_OPERATION_UNSUPPORTED, MEDIA_RECIPE_INVALID, MediaError
from product_media.platform_models import (
    RECIPE_PROFILE_VERSION,
    MediaOperation,
    MediaRecipe,
)

# Canonical operation registry — declarative metadata only (no eval/code).
OPERATION_REGISTRY: dict[str, dict] = {
    "orientation_normalize": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "cleanup": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "background_remove": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": False,
        "provider_required": True,
    },
    "background_replace": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "crop": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "resize": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "pad": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "enhance": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "sharpen": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "composite": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "text_overlay": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "infographic": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "export": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
    "strip_metadata": {
        "version": "1.0.0",
        "input_types": ("image",),
        "output_types": ("image",),
        "deterministic": True,
        "provider_required": False,
    },
}


def get_operation(name: str) -> dict:
    op = OPERATION_REGISTRY.get(name)
    if op is None:
        raise MediaError(MEDIA_OPERATION_UNSUPPORTED, name)
    return op


def build_recipe(
    *,
    tenant_id: str,
    operations: list[dict],
    target_profile_id: str = "",
    recipe_id: str | None = None,
    version: str = RECIPE_PROFILE_VERSION,
) -> MediaRecipe:
    ops: list[MediaOperation] = []
    for row in operations:
        name = str(row.get("name") or "")
        meta = get_operation(name)
        params = dict(row.get("parameters") or {})
        # Reject executable payloads
        for banned in ("eval", "exec", "code", "script", "__import__"):
            if banned in params:
                raise MediaError(MEDIA_RECIPE_INVALID, f"banned_param:{banned}")
        ops.append(
            MediaOperation(
                name=name,
                version=str(row.get("version") or meta["version"]),
                parameters=params,
                deterministic=bool(meta["deterministic"]),
                provider_required=bool(meta["provider_required"]),
            )
        )
    if not ops:
        raise MediaError(MEDIA_RECIPE_INVALID, "empty_operations")
    return MediaRecipe(
        recipe_id=recipe_id or str(uuid.uuid4()),
        version=version,
        tenant_id=tenant_id,
        operations=tuple(ops),
        target_profile_id=target_profile_id,
    )


def recipe_identity(
    *,
    source_hash: str,
    recipe: MediaRecipe,
    target_profile_version: str = "",
) -> str:
    payload = {
        "source_hash": source_hash,
        "recipe_id": recipe.recipe_id,
        "recipe_version": recipe.version,
        "target_profile_id": recipe.target_profile_id,
        "target_profile_version": target_profile_version,
        "operations": [
            {"name": o.name, "version": o.version, "parameters": dict(o.parameters)}
            for o in recipe.operations
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def recipe_from_brief(
    *,
    tenant_id: str,
    media_type: str = "image",
    aspect_ratio: str = "1:1",
    target_profile_id: str = "",
) -> MediaRecipe:
    """Translate Content Factory MediaBrief intent into executable MediaRecipe."""
    _ = media_type
    ops = [
        {"name": "cleanup", "parameters": {}},
        {"name": "resize", "parameters": {"aspect_ratio": aspect_ratio, "fit": "pad"}},
        {"name": "export", "parameters": {"format": "jpeg"}},
    ]
    return build_recipe(
        tenant_id=tenant_id,
        operations=ops,
        target_profile_id=target_profile_id,
    )
