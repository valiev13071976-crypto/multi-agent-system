"""Category-aware field policy — configuration, not a universal required-field list."""

from __future__ import annotations

from dataclasses import dataclass

FIELD_REQUIRED = "required"
FIELD_RECOMMENDED = "recommended"
FIELD_OPTIONAL = "optional"
FIELD_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CategoryPolicy:
    category: str
    required: tuple[str, ...]
    recommended: tuple[str, ...]
    optional: tuple[str, ...] = ()
    not_applicable: tuple[str, ...] = ()
    require_main_image: bool = False
    seo_title_max: int = 70
    seo_meta_max: int = 160


# Identity is always required; remaining fields vary by category.
DEFAULT_POLICIES: dict[str, CategoryPolicy] = {
    "smartphone": CategoryPolicy(
        category="smartphone",
        required=("sku", "product_name", "brand"),
        recommended=("model", "color", "memory"),
        optional=("weight", "dimensions", "warranty", "processor"),
        not_applicable=("clothing_size",),
        require_main_image=True,
    ),
    "headphones": CategoryPolicy(
        category="headphones",
        required=("sku", "product_name"),
        recommended=("brand", "color"),
        optional=("weight", "warranty"),
        not_applicable=("processor", "camera"),
        require_main_image=False,
    ),
    "watch": CategoryPolicy(
        category="watch",
        required=("sku", "product_name", "brand"),
        recommended=("model", "color"),
        optional=("weight", "warranty"),
        require_main_image=False,
    ),
    "tv": CategoryPolicy(
        category="tv",
        required=("sku", "product_name", "brand"),
        recommended=("model",),
        optional=("dimensions", "weight"),
        not_applicable=("clothing_size",),
        require_main_image=False,
    ),
    "clothing": CategoryPolicy(
        category="clothing",
        required=("sku", "product_name"),
        recommended=("brand", "color", "size"),
        optional=("material",),
        not_applicable=("processor", "ip_rating", "camera"),
        require_main_image=False,
    ),
    "generic": CategoryPolicy(
        category="generic",
        required=("sku", "product_name"),
        recommended=("brand",),
        optional=("model", "color", "weight"),
        require_main_image=False,
    ),
}


def resolve_category_policy(category: str | None, *, override: CategoryPolicy | None = None) -> CategoryPolicy:
    if override is not None:
        return override
    key = str(category or "generic").strip().lower() or "generic"
    return DEFAULT_POLICIES.get(key, DEFAULT_POLICIES["generic"])
