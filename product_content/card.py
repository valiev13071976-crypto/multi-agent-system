"""Block 12 — deterministic ProductCard assembly. Reuses data_intel cleaning/mapping and Block 11 economics."""

from __future__ import annotations

import re
from decimal import Decimal

from content_intel.generator import DeterministicContentGenerator, GenerationContext
from content_intel.platform_models import PROVENANCE_CREATIVE
from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.contracts import (
    ROLE_ARTICLE,
    ROLE_BARCODE,
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_EAN,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_SKU,
)
from data_intel.economics import EconomicsInput, EconomicsPolicy, calculate_economics, calculate_minimum_price
from data_intel.mapping import map_header_role
from security.tenant import require_tenant_id

from product_content.category_policy import CategoryPolicy, resolve_category_policy
from product_content.contracts import (
    COMPLETE,
    INSUFFICIENT_INPUT,
    PARTIAL,
    PROV_AI,
    PROV_CATALOG,
    PROV_DERIVED,
    PROV_FILE,
    PROV_UNKNOWN,
    PROV_USER,
    ProductCard,
    ProvenancedValue,
    content_version,
    provenance_label,
)
from product_content.errors import CONTENT_IDENTITY_REQUIRED, ProductContentError

_SUPERLATIVES = re.compile(
    r"(?i)\b(best|#1|№1|number\s*one|official\s+dealer|certified\s+original|гарантия\s+100%)\b"
)
_ALL_CAPS = re.compile(r"[A-ZА-ЯЁ]")
_LOWER = re.compile(r"[a-zа-яё]")

_SPEC_KEYS = (
    "memory",
    "storage",
    "ram",
    "color",
    "size",
    "dimensions",
    "weight",
    "material",
    "materials",
    "processor",
    "camera",
    "battery",
    "ip_rating",
    "screen",
    "warranty",
    "country_of_origin",
    "manufacturer",
    "package_contents",
    "model",
)

_COLOR_MAP = {
    "чёрный": "black",
    "черный": "black",
    "белый": "white",
    "синий": "blue",
    "красный": "red",
    "серый": "gray",
}

_MEMORY_RE = re.compile(r"(?i)^\s*(\d+)\s*(gb|гб|tb|тб|mb|мб)\s*$")


def _first(row: dict, *keys: str):
    for k in keys:
        if k in row and clean_text(row[k]) is not None:
            return row[k]
    return None


def _text(value) -> str:
    return clean_text(value) or ""


def normalize_color(raw: str | None) -> str | None:
    t = clean_text(raw)
    if t is None:
        return None
    low = t.casefold()
    return _COLOR_MAP.get(low, low)


def normalize_memory(raw: str | None) -> str | None:
    t = clean_text(raw)
    if t is None:
        return None
    m = _MEMORY_RE.match(t)
    if not m:
        return t
    unit = m.group(2).upper().replace("ГБ", "GB").replace("ТБ", "TB").replace("МБ", "MB")
    if unit in {"ГБ"}:
        unit = "GB"
    return f"{m.group(1)} {unit}"


def normalize_weight(raw: str | None) -> str | None:
    t = clean_text(raw)
    if t is None:
        return None
    num = normalize_decimal_string(t.replace("kg", "").replace("кг", "").strip())
    if num and re.fullmatch(r"-?\d+(\.\d+)?", num):
        return f"{num} kg"
    return t


def _pv(raw, *, provenance: str, role: str, normalizer=None) -> ProvenancedValue:
    text = clean_text(raw)
    if text is None:
        return ProvenancedValue(raw=None, normalized=None, provenance=PROV_UNKNOWN, role=role)
    norm = normalizer(text) if normalizer else text
    return ProvenancedValue(raw=str(raw), normalized=norm, provenance=provenance, role=role)


def map_block10_row(row: dict) -> dict:
    """Reuse data_intel mapping aliases — do not invent a second mapper."""
    out = dict(row)
    for key, value in list(row.items()):
        role, _conf = map_header_role(str(key))
        if role == ROLE_SKU and "sku" not in out:
            out["sku"] = value
        elif role == ROLE_ARTICLE:
            if "article" not in out:
                out["article"] = value
            if "sku" not in out:
                out["sku"] = value
        elif role in {ROLE_BARCODE, ROLE_EAN} and "barcode" not in out:
            out["barcode"] = value
        elif role == ROLE_BRAND and "brand" not in out:
            out["brand"] = value
        elif role == ROLE_CATEGORY and "category" not in out:
            out["category"] = value
        elif role == ROLE_PRODUCT_NAME and "product_name" not in out:
            out["product_name"] = value
        elif role == ROLE_PURCHASE_PRICE and "purchase_price" not in out:
            out["purchase_price"] = value
        elif role == ROLE_SELLING_PRICE and "selling_price" not in out:
            out["selling_price"] = value
    return out


def canonical_title(row: dict, *, include_price: bool = False) -> str:
    name = _text(_first(row, "canonical_title", "product_name", "title", "товар"))
    brand = _text(_first(row, "brand", "бренд"))
    model = _text(_first(row, "model", "модель"))
    parts: list[str] = []
    if brand and brand.casefold() not in name.casefold():
        parts.append(brand)
    if model and model.casefold() not in name.casefold() and model.casefold() not in brand.casefold():
        parts.append(model)
    if name:
        parts.append(name)
    title = " ".join(parts).strip()
    title = _SUPERLATIVES.sub("", title)
    title = re.sub(r"\s+", " ", title).strip(" -|/")
    letters = _ALL_CAPS.findall(title) + _LOWER.findall(title)
    if title.isupper() and len(title) > 3:
        title = title.title()
    if not include_price:
        title = re.sub(r"(?i)\s*[$€₽]\s*\d[\d\s.,]*", "", title).strip()
        title = re.sub(r"(?i)\s*\d[\d\s.,]*\s*(руб|rub|usd)\b", "", title).strip()
    return title


def _grounded_bullets(specs: dict[str, ProvenancedValue], extra: list[str]) -> list[str]:
    bullets: list[str] = []
    for item in extra:
        t = clean_text(item)
        if t and not _SUPERLATIVES.search(t):
            bullets.append(t)
    for key, pv in specs.items():
        if pv.normalized and pv.provenance != PROV_UNKNOWN:
            bullets.append(f"{key}: {pv.normalized}")
    return bullets[:12]


def _grounded_descriptions(*, tenant_id: str, title: str, facts: dict[str, str], unknown: list[str]) -> tuple[str, str]:
    short_bits = [title] if title else []
    for key in ("brand", "model", "color", "memory"):
        if facts.get(key):
            short_bits.append(f"{key} {facts[key]}")
    short = ". ".join(short_bits)[:280]
    gen = DeterministicContentGenerator()
    ctx = GenerationContext(
        tenant_id=tenant_id,
        project_id="product-content",
        channel="site",
        objective="product_card",
        audience_segments=("shopper",),
        pillars=("facts",),
        evidence_refs=tuple(facts.keys()),
        product_facts=facts,
        forbidden_terms=("№1", "bestseller", "IP68") if "ip_rating" not in facts else ("№1", "bestseller"),
    )
    asset = gen.generate_copy(ctx, content_type="product_long_description")
    long_parts = [short] if short else []
    for k, v in facts.items():
        if k not in {"purchase_price", "selling_price", "source_price"}:
            long_parts.append(f"{k}: {v}")
    if asset.provenance_kind == PROVENANCE_CREATIVE and asset.body:
        # Keep generator output only as reformatting wrapper; facts stay explicit.
        long_parts.append("generated_wrapper: reformatted_known_facts_only")
    for u in unknown:
        long_parts.append(f"{u}: UNKNOWN")
    long_desc = ". ".join(p for p in long_parts if p)[:4000]
    return short or title, long_desc


def _economics_ref(row: dict) -> dict:
    purchase = normalize_decimal_string(_first(row, "purchase_price", "cost"))
    selling = normalize_decimal_string(_first(row, "selling_price", "price"))
    if purchase is None and selling is None:
        return {}
    try:
        inp = EconomicsInput(
            sku=_text(_first(row, "sku", "артикул")) or "",
            product_name=_text(_first(row, "product_name", "title")) or "",
            purchase_price=Decimal(purchase) if purchase else None,
            selling_price=Decimal(selling) if selling else None,
        )
        eco = calculate_economics(inp)
        floor = None
        if inp.purchase_price is not None:
            amount, _label, _notes = calculate_minimum_price(inp, policy=EconomicsPolicy())
            floor = str(amount) if amount is not None else None
        return {
            "completeness": eco.get("completeness"),
            "decision": eco.get("decision"),
            "status": eco.get("status"),
            "minimum_price": floor,
            "selling_price": selling,
            "engine": "data_intel.economics",
        }
    except Exception as exc:
        return {"engine": "data_intel.economics", "error": type(exc).__name__}


def assemble_product_card(
    row: dict,
    *,
    tenant_id: str,
    policy: CategoryPolicy | None = None,
    source_default: str = PROV_FILE,
) -> ProductCard:
    tenant = require_tenant_id(tenant_id)
    mapped = map_block10_row(row)
    src = provenance_label(mapped.get("provenance") or mapped.get("source") or source_default)
    sku = _text(_first(mapped, "sku", "артикул"))
    name = _text(_first(mapped, "product_name", "title", "товар"))
    product_id = _text(_first(mapped, "product_id")) or sku or name
    if not product_id:
        raise ProductContentError(CONTENT_IDENTITY_REQUIRED)
    category = _text(_first(mapped, "category")) or "generic"
    cat_policy = resolve_category_policy(category, override=policy)
    title = canonical_title(mapped)
    brand = _text(_first(mapped, "brand", "бренд"))
    model = _text(_first(mapped, "model", "модель"))

    specs: dict[str, ProvenancedValue] = {}
    unknown: list[str] = []
    for key in _SPEC_KEYS:
        raw = _first(mapped, key)
        if raw is None:
            if key in cat_policy.required or key in cat_policy.recommended:
                unknown.append(key)
            continue
        normalizer = None
        if key in {"color"}:
            normalizer = normalize_color
        elif key in {"memory", "ram", "storage"}:
            normalizer = normalize_memory
        elif key == "weight":
            normalizer = normalize_weight
        specs[key] = _pv(raw, provenance=src, role=key, normalizer=normalizer)

    # Explicit UNKNOWN marker from source must stay unknown — never fill.
    for key, value in mapped.items():
        if str(key).endswith("_unknown") or str(value).strip().upper() == "UNKNOWN":
            fact = key.replace("_unknown", "")
            if fact not in unknown:
                unknown.append(fact)
            if fact in specs:
                specs[fact] = ProvenancedValue(
                    raw=str(value), normalized=None, provenance=PROV_UNKNOWN, role=fact
                )

    facts = {k: v.normalized for k, v in specs.items() if v.normalized}
    if brand:
        facts["brand"] = brand
    if model:
        facts["model"] = model
    if sku:
        facts["sku"] = sku
    extra_features: list[str] = []
    raw_features = mapped.get("features") or mapped.get("feature_bullets") or []
    if isinstance(raw_features, str):
        extra_features = [p.strip() for p in re.split(r"[;\n•]", raw_features) if p.strip()]
    elif isinstance(raw_features, (list, tuple)):
        extra_features = [str(x) for x in raw_features]
    bullets = _grounded_bullets(specs, extra_features)
    short, long_desc = _grounded_descriptions(tenant_id=tenant, title=title, facts=facts, unknown=unknown)

    values = {
        "sku": sku,
        "product_name": name or title,
        "brand": brand,
        "model": model,
        "color": specs["color"].normalized if "color" in specs else "",
        "memory": specs["memory"].normalized if "memory" in specs else "",
        "size": specs["size"].normalized if "size" in specs else "",
        "weight": specs["weight"].normalized if "weight" in specs else "",
        "warranty": specs["warranty"].normalized if "warranty" in specs else "",
        "processor": specs["processor"].normalized if "processor" in specs else "",
        "camera": specs["camera"].normalized if "camera" in specs else "",
        "ip_rating": specs["ip_rating"].normalized if "ip_rating" in specs else "",
        "material": specs["material"].normalized if "material" in specs else specs["materials"].normalized if "materials" in specs else "",
    }
    missing_required = tuple(f for f in cat_policy.required if not str(values.get(f) or "").strip() and f not in specs)
    # sku/product_name/brand checked via values
    missing_required = tuple(
        f for f in cat_policy.required if not str(values.get(f) or (specs[f].normalized if f in specs else "") or "").strip()
    )
    missing_recommended = tuple(
        f
        for f in cat_policy.recommended
        if not str(values.get(f) or (specs[f].normalized if f in specs else "") or "").strip()
    )

    if missing_required:
        completeness = INSUFFICIENT_INPUT
        content_status = INSUFFICIENT_INPUT
    elif missing_recommended or unknown:
        completeness = PARTIAL
        content_status = PARTIAL
    else:
        completeness = COMPLETE
        content_status = COMPLETE

    purchase = normalize_decimal_string(_first(mapped, "purchase_price"))
    selling = normalize_decimal_string(_first(mapped, "selling_price", "price"))
    source_price = normalize_decimal_string(_first(mapped, "source_price"))
    eco = _economics_ref(mapped)

    field_prov = {
        "sku": src if sku else PROV_UNKNOWN,
        "product_name": src if name else PROV_UNKNOWN,
        "canonical_title": PROV_DERIVED,
        "brand": src if brand else PROV_UNKNOWN,
        "model": src if model else PROV_UNKNOWN,
        "short_description": PROV_AI,
        "long_description": PROV_AI,
        "economics_reference": "data_intel.economics" if eco else PROV_UNKNOWN,
    }
    for k, pv in specs.items():
        field_prov[k] = pv.provenance

    version = content_version(
        {
            "sku": sku,
            "name": name,
            "brand": brand,
            "model": model,
            "title": title,
            "specs": {k: v.normalized for k, v in specs.items()},
            "category": cat_policy.category,
        }
    )
    return ProductCard(
        tenant_id=tenant,
        product_id=product_id,
        sku=sku,
        article=_text(_first(mapped, "article")),
        barcode=_text(_first(mapped, "barcode", "ean", "gtin", "штрихкод")),
        brand=brand,
        model=model,
        category=cat_policy.category,
        subcategory=_text(_first(mapped, "subcategory")),
        product_name=name or title,
        canonical_title=title,
        short_description=short,
        long_description=long_desc,
        feature_bullets=tuple(bullets),
        attributes=tuple(specs.values()),
        specifications=specs,
        dimensions=_text(_first(mapped, "dimensions")),
        weight=values.get("weight") or "",
        materials=values.get("material") or "",
        color=values.get("color") or "",
        variant=_text(_first(mapped, "variant")),
        country_of_origin=_text(_first(mapped, "country_of_origin", "country")),
        manufacturer=_text(_first(mapped, "manufacturer")),
        warranty=values.get("warranty") or "",
        package_contents=_text(_first(mapped, "package_contents")),
        source_price=source_price,
        purchase_price=purchase,
        selling_price=selling,
        economics_reference=eco,
        completeness=completeness,
        missing_required=missing_required,
        missing_recommended=missing_recommended,
        unknown_facts=tuple(dict.fromkeys(unknown)),
        warnings=tuple(),
        field_provenance=field_prov,
        version=version,
        content_status=content_status,
    )
