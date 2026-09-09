"""LIVE Bitrix adapter — structurally capable, dormant without configuration."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Callable

logger = logging.getLogger(__name__)

from integrations.activation.adapters import FixtureAdapterState
from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.bitrix import schema
from integrations.bitrix.client import BitrixHttpClient
from integrations.bitrix.config import BitrixIntegrationConfig, load_bitrix_config
from integrations.bitrix.errors import BitrixValidationError
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter

# First controlled production Bitrix product write: the base product name,
# BRAND (property 100, verified PANDA_MANAGED on IBLOCK 14), the offer/SKU
# article (property 283, verified PANDA_MANAGED on IBLOCK 15), the retail
# selling price, and (Block 5.6 follow-up defect closure) the native
# purchasingPrice/purchasingCurrency product fields all have a verified
# real write destination on this installation -- see
# integrations/bitrix/schema.py's module docstring and PropertyBinding
# table (the source of truth this module reuses, rather than re-deriving/
# guessing its own property ids). EAN/GTIN and category still have no
# verified destination and remain unwritten.
_BRAND_PROPERTY = schema.catalog_property(code="BRAND")
_ARTICLE_PROPERTY = schema.offer_property(code="ARTICLE")

# Complete-product-card follow-up pass (module docstring in
# integrations/bitrix/schema.py, second Block 5.6 follow-up defect
# closure): section assignment, weight/dimensions, preview/detail content
# + pictures, and a small set of verified characteristics all now have a
# confirmed real destination too -- see ``_optional_product_fields`` below.
# EAN/GTIN, SEO, and gallery/additional-image writing still have none and
# remain unwritten, exactly as before.
_PHYSICAL_DIMENSION_KEYS = (schema.WEIGHT_FIELD, schema.LENGTH_FIELD, schema.WIDTH_FIELD, schema.HEIGHT_FIELD)


class LiveBitrixAdapter(BitrixFixtureAdapter):
    """Production Bitrix adapter — fail closed without LIVE webhook/OAuth config."""

    def __init__(
        self,
        *,
        config: BitrixIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        state: FixtureAdapterState | None = None,
    ):
        super().__init__(state=state)  # type: ignore[arg-type]
        self._config = config or load_bitrix_config()
        self._secret_resolver = secret_resolver
        self._client: BitrixHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    @property
    def client(self) -> BitrixHttpClient:
        if self._client is None:
            self._client = BitrixHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        return self._client

    def _assert_live_configured(self, credential_ref: str = "") -> None:
        if not self._config.is_live:
            raise IntegrationNotConfiguredError("bitrix_live_mode_required")
        url = ""
        if self._secret_resolver and credential_ref:
            url = str(self._secret_resolver(credential_ref) or "").strip()
        if not url:
            url = self._config._resolved_webhook_url()
        if not url:
            raise IntegrationNotConfiguredError("bitrix_webhook_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured(credential_ref)
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        if self.state.auth_ok is False:
            return {"ok": False, "category": "INTEGRATION_AUTH_FAILED"}
        return {
            "ok": True,
            "authentication_valid": True,
            "required_capabilities_available": True,
            "provider_identity": "live:bitrix",
            "destructive": False,
            "live": True,
            "mode": "LIVE",
            **self._config.safe_metadata(),
        }

    def health(self) -> dict:
        try:
            self._assert_live_configured()
        except IntegrationNotConfiguredError:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_NOT_CONFIGURED"}
        if self.state.rate_limited:
            return {"status": "DEGRADED", "error_category": "INTEGRATION_RATE_LIMITED"}
        if not self.state.auth_ok:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_AUTH_FAILED"}
        return {"status": "HEALTHY", "error_category": "", "live": True, "mode": "LIVE"}

    def _require_catalog_iblock_id(self) -> int:
        # ``filter.iblockId`` is a required parameter of catalog.product.list
        # / catalog.section.list; it identifies *which* products IBLOCK to
        # read and is installation-specific, so it is sourced from
        # ``BitrixIntegrationConfig.catalog_id`` (``BITRIX_CATALOG_ID``)
        # rather than assumed/hardcoded -- fail closed if unset instead of
        # guessing an IBLOCK ID.
        iblock_id = str(self._config.catalog_id or "").strip()
        if not iblock_id or not iblock_id.lstrip("-").isdigit():
            raise IntegrationNotConfiguredError("bitrix_catalog_iblock_id_not_configured")
        return int(iblock_id)

    def _require_offers_iblock_id(self) -> int:
        offers_id = str(self._config.offers_iblock_id or "").strip()
        if not offers_id or not offers_id.lstrip("-").isdigit():
            raise IntegrationNotConfiguredError("bitrix_offers_iblock_id_not_configured")
        return int(offers_id)

    def _require_retail_price_type_id(self) -> int:
        # ``catalogGroupId`` -- installation-specific price-type id (see
        # catalog.priceType.list); never guessed/hardcoded to e.g. "1"
        # (module docstring in integrations/bitrix/schema.py: regional/
        # price-type rows for this installation are known to be distinct,
        # non-uniform ids -- see catalog.price.list evidence in
        # tests/test_bitrix_production_schema_binding.py).
        type_id = str(self._config.retail_price_type_id or "").strip()
        if not type_id or not type_id.lstrip("-").isdigit():
            raise IntegrationNotConfiguredError("bitrix_retail_price_type_id_not_configured")
        return int(type_id)

    def _envelope(self, items: list) -> dict:
        return {"items": items, "mode": "LIVE", "live": True, "provider_metadata": self._config.safe_metadata()}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        self._raise_if_bad()
        # Engineering block: no destructive/mutating live calls in tests; read path only when configured.
        params = params or {}
        operation = str(params.get("operation") or "")

        if operation == "order_read":
            data = self.client.call(
                "sale.order.list", credential_ref=credential_ref, params={"filter": {}, "select": ["ID", "NAME"]}
            )
            return self._envelope(data.get("result") or [])

        if operation == "product_lookup":
            product_id = str(params.get("bitrix_id") or params.get("id") or params.get("product_id") or "").strip()
            if not product_id or not product_id.lstrip("-").isdigit():
                raise BitrixValidationError("product_id_required")
            data = self.client.call(
                "catalog.product.list",
                credential_ref=credential_ref,
                params={
                    "filter": {"iblockId": self._require_catalog_iblock_id(), "id": int(product_id)},
                    "select": schema.catalog_select_fields(),
                },
            )
            result = data.get("result")
            return self._envelope(result.get("products", []) if isinstance(result, dict) else (result or []))

        if operation in ("section_read", "category_read"):
            # Sections belong to a specific IBLOCK too (products by default;
            # callers reading the offers-IBLOCK's own sections may pass
            # ``iblock_id`` explicitly) -- never assumed.
            requested_iblock = params.get("iblock_id")
            iblock_id = int(requested_iblock) if requested_iblock else self._require_catalog_iblock_id()
            data = self.client.call(
                "catalog.section.list",
                credential_ref=credential_ref,
                params={"filter": {"iblockId": iblock_id}, "select": ["id", "name", "sort", "iblockSectionId"]},
            )
            result = data.get("result")
            return self._envelope(result.get("sections", []) if isinstance(result, dict) else (result or []))

        if operation == "offer_read":
            parent_id = str(
                params.get("parent_product_id") or params.get("bitrix_id") or params.get("product_id") or ""
            ).strip()
            if not parent_id or not parent_id.lstrip("-").isdigit():
                raise BitrixValidationError("parent_product_id_required")
            data = self.client.call(
                "catalog.product.offer.list",
                credential_ref=credential_ref,
                params={
                    "filter": {"iblockId": self._require_offers_iblock_id(), schema.CML2_LINK_REST_FIELD: int(parent_id)},
                    "select": schema.offer_select_fields(),
                },
            )
            result = data.get("result")
            return self._envelope(result.get("offers", []) if isinstance(result, dict) else (result or []))

        if operation == "price_read":
            product_id = str(params.get("bitrix_id") or params.get("product_id") or "").strip()
            if not product_id or not product_id.lstrip("-").isdigit():
                raise BitrixValidationError("product_id_required")
            data = self.client.call(
                "catalog.price.list",
                credential_ref=credential_ref,
                params={
                    "filter": {"productId": int(product_id)},
                    "select": ["id", "productId", "catalogGroupId", "price", "currency"],
                },
            )
            result = data.get("result")
            return self._envelope(result.get("prices", []) if isinstance(result, dict) else (result or []))

        # Default: bounded catalog listing (spec section 10's original READ
        # verification path -- already proven in production; behavior here
        # is unchanged).
        # Product/catalog reads use the self-hosted Bitrix REST "catalog"
        # service (``catalog.product.list``, webhook scope ``catalog``),
        # which operates on the installed products IBLOCK. This is NOT
        # ``crm.product.list`` -- that method belongs to the Bitrix24 CRM
        # product catalog, which does not exist on a self-hosted
        # "Управление сайтом" install (calling it there returns HTTP 404,
        # "method not found", matching a least-privilege webhook that only
        # grants ``catalog`` + ``iblock`` scopes -- never ``crm``).
        data = self.client.call(
            "catalog.product.list",
            credential_ref=credential_ref,
            params={"filter": {"iblockId": self._require_catalog_iblock_id()}, "select": ["id", "iblockId", "name"]},
        )
        result = data.get("result")
        return self._envelope(result.get("products", []) if isinstance(result, dict) else (result or []))

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        self._raise_if_bad()
        operation = str(payload.get("operation") or "").strip()
        if operation != "product_create":
            # Every other LIVE write operation (update/price/stock/media/
            # SEO/publish) remains the pre-existing, deliberate,
            # documented placeholder -- out of scope for the first
            # controlled single-product CREATE (see
            # docs/bitrix-aspro-premier-integration.md's "Deferred /
            # Unsupported" section). Only product_create is implemented
            # here, matching exactly what
            # business_assistant.controlled_bitrix_write.
            # execute_single_product_write ever calls.
            raise IntegrationNotConfiguredError("bitrix_live_write_blocked_engineering")
        return self._write_product_create_live(
            capability=capability, payload=payload, idempotency_key=idempotency_key, credential_ref=credential_ref
        )

    # --- Complete-product-card follow-up pass: optional verified fields ---
    # (module docstring in integrations/bitrix/schema.py, item A/C/D/E/F).
    # Every helper below is validated fail-closed BEFORE any HTTP call,
    # same discipline as the existing purchase-price validation; a field
    # the caller did not supply is simply omitted (never forced to 0/None
    # on the wire).

    def _section_field(self, product_in: dict) -> dict:
        """Native ``iblockSectionId`` assignment (item A). The id itself is
        ALREADY resolved -- never guessed here -- by
        ``business_assistant.controlled_bitrix_write.
        prepare_single_product_write`` against a live
        ``catalog.section.list`` snapshot, via ``schema.resolve_section_id``,
        before the user ever approves the write. This only sanity-checks
        that a supplied id looks like a real positive Bitrix id."""
        section_id = (product_in.get("classification") or {}).get("section_id")
        if section_id in (None, ""):
            return {}
        text = str(section_id).strip()
        if not text.lstrip("-").isdigit() or int(text) <= 0:
            raise BitrixValidationError("section_id_invalid")
        return {schema.SECTION_FIELD: int(text)}

    def _physical_fields(self, product_in: dict) -> dict:
        """Native weight/length/width/height pass-through (item D) -- no
        unit conversion, ever (see schema.py module docstring for why the
        unit itself could not be independently verified)."""
        physical = product_in.get("physical") or {}
        if not isinstance(physical, dict):
            raise BitrixValidationError("physical_fields_invalid")
        fields: dict = {}
        for key in _PHYSICAL_DIMENSION_KEYS:
            raw = physical.get(key)
            if raw in (None, ""):
                continue
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                raise BitrixValidationError(f"physical_{key}_invalid")
            if value <= 0:
                raise BitrixValidationError(f"physical_{key}_invalid")
            fields[key] = str(raw)
        return fields

    def _content_fields(self, product_in: dict) -> dict:
        """Native previewText/previewTextType/detailText/detailTextType
        pass-through (items E/F) -- Panda supplies already-prepared
        content; this never fabricates marketing copy."""
        content = product_in.get("content") or {}
        if not isinstance(content, dict):
            raise BitrixValidationError("content_fields_invalid")
        fields: dict = {}
        short = content.get("short_description")
        if short:
            fields[schema.PREVIEW_TEXT_FIELD] = str(short)
            fields[schema.PREVIEW_TEXT_TYPE_FIELD] = str(content.get("short_description_type") or "text")
        detailed = content.get("detailed_description")
        if detailed:
            fields[schema.DETAIL_TEXT_FIELD] = str(detailed)
            fields[schema.DETAIL_TEXT_TYPE_FIELD] = str(content.get("detailed_description_type") or "text")
        return fields

    def _media_fields(self, product_in: dict) -> dict:
        """Native previewPicture/detailPicture write mapping (items E/F):
        the CONFIRMED Bitrix write shape is ``{"fileData": [filename,
        base64_content]}`` -- never a bare URL. Panda must supply already-
        encoded ``base64`` content plus a ``filename``; this never
        fetches/encodes an image itself, and no LIVE upload is ever
        exercised by this repository's tests (mocked HTTP only)."""
        media = product_in.get("media") or {}
        if not isinstance(media, dict):
            raise BitrixValidationError("media_fields_invalid")
        fields: dict = {}
        for bitrix_field, media_key in (
            (schema.PREVIEW_PICTURE_FIELD, "preview_picture"),
            (schema.DETAIL_PICTURE_FIELD, "detail_picture"),
        ):
            entry = media.get(media_key)
            if not entry:
                continue
            if not isinstance(entry, dict) or not entry.get("filename") or not entry.get("base64"):
                raise BitrixValidationError(f"{media_key}_invalid")
            fields[bitrix_field] = {schema.PICTURE_FILE_DATA_KEY: [str(entry["filename"]), str(entry["base64"])]}
        return fields

    def _optional_product_fields(self, product_in: dict) -> tuple[dict, list[str]]:
        """Merge every verified-but-optional complete-card field (section,
        physical, content, media, characteristics) into one Bitrix
        ``fields`` dict. Returns ``(fields, characteristics_written_keys)``
        -- the latter is only for observability/read-back reporting, never
        used to change write behavior. Unrecognized characteristic keys
        are never written (``schema.map_characteristics_to_properties``
        already drops them) -- Panda still owns/preserves that source data,
        it is just never sent to a guessed property."""
        fields: dict = {}
        fields.update(self._section_field(product_in))
        fields.update(self._physical_fields(product_in))
        fields.update(self._content_fields(product_in))
        fields.update(self._media_fields(product_in))
        characteristic_fields, _unmapped = schema.map_characteristics_to_properties(
            product_in.get("characteristics")
        )
        fields.update(characteristic_fields)
        written_property_ids = {
            int(wire_key[len("property"):]) for wire_key in characteristic_fields if wire_key.startswith("property")
        }
        characteristics_written = []
        for key in dict(product_in.get("characteristics") or {}):
            binding = schema.characteristic_binding(key)
            if binding is not None and binding.property_id in written_property_ids:
                characteristics_written.append(key)
        return fields, characteristics_written

    # --- LIVE governed product create (first controlled production write) --

    def _write_product_create_live(
        self, *, capability: str, payload: dict, idempotency_key: str, credential_ref: str
    ) -> dict:
        """Real, minimal LIVE create for exactly the fields this
        installation's schema binding has verified a destination for:
        name, BRAND (property 100), article/SKU (offer property 283), the
        retail selling price, and (Block 5.6 follow-up defect closure) the
        native ``purchasingPrice``/``purchasingCurrency`` product fields.
        EAN/category are never written here -- ``controlled_bitrix_write``
        already never includes them in the canonical payload this reads.

        Idempotency/duplicate-protection design note: a FRESH
        ``LiveBitrixAdapter`` is constructed on every
        ``IntegrationActivationService.execute_via_gateway`` call (see
        ``IntegrationActivationService._adapter_for`` -- unlike the FIXTURE
        adapters, which are cached long-lived instances on the service),
        so no in-adapter-memory cache here would ever survive a retry
        across separate calls. Instead this asks BITRIX ITSELF, using the
        exact already-verified ``catalog.product.list`` /
        ``catalog.product.offer.list`` / ``catalog.price.list`` read
        surface, whether a product tagged with this idempotency key's
        deterministic ``xmlId`` (a native, already-used base field --
        never a guessed property) already exists before ever calling
        ``catalog.product.add`` -- a retry, from this process or a
        different one, can never create a second real product/offer/price
        row for the same idempotency key. Each step (product/offer/price)
        is checked-then-created independently, so a retry after a partial
        failure only performs the remaining, not-yet-completed step(s).
        """
        product_in = dict(payload.get("product") or {})
        name = str(product_in.get("title") or product_in.get("name") or "").strip()
        if not name:
            raise BitrixValidationError("name_required")
        active = bool(payload.get("active", False))
        brand = str((product_in.get("properties") or {}).get("brand") or "").strip()
        sku = str(product_in.get("sku") or product_in.get("article") or "").strip()
        price_field = product_in.get("price") or {}
        retail_amount = (
            str(price_field.get("selling_price") or "").strip() if isinstance(price_field, dict) else ""
        )
        currency = (price_field.get("currency") if isinstance(price_field, dict) else None) or "RUB"

        # Native purchasingPrice/purchasingCurrency (Block 5.6 follow-up
        # defect closure -- LIVE READ-ONLY discovery proved these are real,
        # first-class Bitrix catalog.product fields, distinct from the
        # retail selling price above). Deliberately read from a SEPARATE
        # top-level ``purchase_price`` key -- never from ``price_field`` --
        # so a purchase price can structurally never be substituted for the
        # retail selling price. Validated up front (fail closed on
        # malformed data) before any HTTP call is made, same as the
        # ``name_required`` check above.
        purchase_price_field = product_in.get("purchase_price") or {}
        purchase_price_amount = ""
        purchase_price_currency = "RUB"
        if isinstance(purchase_price_field, dict):
            raw_purchase_amount = str(purchase_price_field.get("amount") or "").strip()
            if raw_purchase_amount:
                try:
                    parsed = Decimal(raw_purchase_amount)
                except (InvalidOperation, ValueError, TypeError):
                    raise BitrixValidationError("purchase_price_invalid")
                if parsed <= 0:
                    raise BitrixValidationError("purchase_price_invalid")
                purchase_price_amount = raw_purchase_amount
                purchase_price_currency = str(purchase_price_field.get("currency") or "RUB").strip() or "RUB"
        elif purchase_price_field:
            # A non-empty, non-dict purchase_price payload is itself
            # malformed input -- fail closed rather than silently ignoring it.
            raise BitrixValidationError("purchase_price_invalid")

        # Complete-product-card follow-up pass: section/physical/content/
        # media/characteristics -- every one validated fail-closed here,
        # before any HTTP call, same as purchase_price above.
        optional_fields, characteristics_written = self._optional_product_fields(product_in)

        xml_id = self._idempotency_xml_id(idempotency_key)

        existing = self._find_product_by_xml_id(xml_id, credential_ref=credential_ref)
        if existing is not None:
            product_id = existing.get("id")
            resolved_active = existing.get("active")
            resolved_active = resolved_active in (True, "Y", "y", 1, "1") if resolved_active is not None else active
            resolved_name = existing.get("name") or name
            idempotent_replay = True
        else:
            # Nothing was created yet for this key -- this is the one step
            # allowed to raise straight through (there is nothing to
            # report as a partial success if this itself fails).
            product_id = self._live_create_product(
                name=name,
                active=active,
                brand=brand,
                xml_id=xml_id,
                credential_ref=credential_ref,
                purchase_price=purchase_price_amount,
                purchase_price_currency=purchase_price_currency,
                extra_fields=optional_fields,
            )
            resolved_active = active
            resolved_name = name
            idempotent_replay = False

        # Set on the very create call above (or, on an idempotent replay,
        # implied by the same deterministic xmlId already having been
        # created with this same request's fields) -- never re-sent via a
        # separate update call; mirrors ``article_written``'s existing
        # "confirmed present, regardless of which branch created it" style.
        purchase_price_written = bool(purchase_price_amount)
        # Same "set on the very create call above, regardless of which
        # branch created it" reasoning as purchase_price_written -- these
        # are all base-product fields, sent (or not) before the offer/
        # price steps below that can still fail.
        section_id_written = optional_fields.get(schema.SECTION_FIELD)
        physical_written = [k for k in _PHYSICAL_DIMENSION_KEYS if k in optional_fields]
        content_written = [
            k
            for k in (schema.PREVIEW_TEXT_FIELD, schema.DETAIL_TEXT_FIELD)
            if k in optional_fields
        ]
        media_written = [
            k for k in (schema.PREVIEW_PICTURE_FIELD, schema.DETAIL_PICTURE_FIELD) if k in optional_fields
        ]

        article_written = ""
        if sku:
            try:
                existing_offer = self._find_offer_by_parent(product_id, credential_ref=credential_ref)
                if existing_offer is None:
                    self._live_create_offer(
                        parent_id=product_id, name=resolved_name, active=resolved_active, sku=sku, credential_ref=credential_ref
                    )
            except Exception as exc:  # noqa: BLE001 -- normalize into PARTIAL_FAILURE, product already exists
                return self._partial_failure(
                    product_id=product_id,
                    name=resolved_name,
                    active=resolved_active,
                    article="",
                    failed_step="offer_create",
                    exc=exc,
                    idempotent=idempotent_replay,
                    purchase_price_written=purchase_price_written,
                    section_id_written=section_id_written,
                    characteristics_written=characteristics_written,
                )
            article_written = sku

        if retail_amount:
            try:
                price_type_id = self._require_retail_price_type_id()
                existing_price = self._find_price(product_id=product_id, catalog_group_id=price_type_id, credential_ref=credential_ref)
                if not existing_price:
                    self._live_create_price(
                        product_id=product_id,
                        price_type_id=price_type_id,
                        amount=retail_amount,
                        currency=currency,
                        credential_ref=credential_ref,
                    )
            except Exception as exc:  # noqa: BLE001 -- normalize into PARTIAL_FAILURE, product already exists
                return self._partial_failure(
                    product_id=product_id,
                    name=resolved_name,
                    active=resolved_active,
                    article=article_written,
                    failed_step="price_create",
                    exc=exc,
                    idempotent=idempotent_replay,
                    purchase_price_written=purchase_price_written,
                    section_id_written=section_id_written,
                    characteristics_written=characteristics_written,
                )

        return {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "product_create",
            "mode": "LIVE",
            "live": True,
            # The step calls above only prove Bitrix accepted (or already
            # held) each mutation -- they are not a substitute for the
            # separate, independent governed read-back
            # (BitrixProductBridge.read_product) the controlled write flow
            # always performs next. Never claimed as fully "VERIFIED" here.
            "verified": "PENDING_INDEPENDENT_READBACK",
            "idempotent": idempotent_replay,
            "product": {
                "external_product_id": str(product_id),
                "name": resolved_name,
                "active": bool(resolved_active),
                "article": article_written,
                "properties": {"brand": brand} if brand else {},
                "purchase_price_written": purchase_price_written,
                # Complete-product-card follow-up pass -- all base-product
                # fields, so (like purchase_price_written) always reflect
                # what was actually sent on the create call above,
                # regardless of whether a later offer/price step failed.
                "section_id_written": section_id_written,
                "physical_written": physical_written,
                "content_written": content_written,
                "media_written": media_written,
                "characteristics_written": characteristics_written,
                "mode": "LIVE",
                "live": True,
            },
            "mapping": {"bitrix_id": str(product_id)},
        }

    def _partial_failure(
        self,
        *,
        product_id,
        name: str,
        active: bool,
        article: str,
        failed_step: str,
        exc: Exception,
        idempotent: bool,
        purchase_price_written: bool = False,
        section_id_written=None,
        characteristics_written: list[str] | None = None,
    ) -> dict:
        return {
            "status": "PARTIAL_FAILURE",
            "write_id": str(uuid.uuid4()),
            "operation": "product_create",
            "mode": "LIVE",
            "live": True,
            "idempotent": idempotent,
            "product": {
                "external_product_id": str(product_id),
                "name": name,
                "active": bool(active),
                "article": article,
                # The base product (including purchasingPrice/Currency,
                # section, physical/content/characteristics, if any) is
                # always created BEFORE the offer/price steps that can fail
                # here -- so this reflects a real, already-sent value,
                # never a guess about a step that hasn't run yet.
                "purchase_price_written": purchase_price_written,
                "section_id_written": section_id_written,
                "characteristics_written": characteristics_written or [],
            },
            "mapping": {"bitrix_id": str(product_id)},
            "failed_step": failed_step,
            "error": getattr(exc, "code", type(exc).__name__),
        }

    @staticmethod
    def _idempotency_xml_id(idempotency_key: str) -> str:
        # Bounded to a conservative length; every real idempotency_key this
        # write path generates is far shorter than this.
        return f"panda-controlled-write:{idempotency_key}"[:255]

    def _find_product_by_xml_id(self, xml_id: str, *, credential_ref: str) -> dict | None:
        # Production defect closure: Bitrix's documented REST contract for
        # catalog.product.list requires BOTH "id" AND "iblockId" to be
        # present in ``select`` (not just usable in ``filter``) --
        # omitting either returns HTTP 400 / error 200040300010 ("Fields
        # id, iblockId are not specified in the selection fields"). This
        # select list previously omitted "iblockId", which is exactly the
        # real production 400 on this call (request_id
        # d5fda7ca-4915-4d75-bf61-f22ef4693f64). The proven, already-working
        # product_lookup read (this same method) always used
        # schema.catalog_select_fields(), whose base list already includes
        # both -- reused here instead of a second, narrower convention.
        select = ["id", "iblockId", "name", "active", "xmlId"]
        if _BRAND_PROPERTY is not None:
            select.append(_BRAND_PROPERTY.select_key)
        data = self.client.call(
            "catalog.product.list",
            credential_ref=credential_ref,
            params={"filter": {"iblockId": self._require_catalog_iblock_id(), "xmlId": xml_id}, "select": select},
        )
        result = data.get("result")
        items = result.get("products", []) if isinstance(result, dict) else (result or [])
        return items[0] if items else None

    def _find_offer_by_parent(self, parent_id, *, credential_ref: str) -> dict | None:
        # Same documented Bitrix requirement as catalog.product.list above
        # applies to catalog.product.offer.list -- "id" and "iblockId" are
        # both required in ``select``.
        data = self.client.call(
            "catalog.product.offer.list",
            credential_ref=credential_ref,
            params={
                "filter": {
                    "iblockId": self._require_offers_iblock_id(),
                    schema.CML2_LINK_REST_FIELD: self._as_bitrix_id(parent_id),
                },
                "select": ["id", "iblockId", schema.CML2_LINK_REST_FIELD],
            },
        )
        result = data.get("result")
        items = result.get("offers", []) if isinstance(result, dict) else (result or [])
        return items[0] if items else None

    def _find_price(self, *, product_id, catalog_group_id: int, credential_ref: str) -> bool:
        data = self.client.call(
            "catalog.price.list",
            credential_ref=credential_ref,
            params={
                "filter": {"productId": self._as_bitrix_id(product_id), "catalogGroupId": catalog_group_id},
                "select": ["id", "productId", "catalogGroupId"],
            },
        )
        result = data.get("result")
        items = result.get("prices", []) if isinstance(result, dict) else (result or [])
        return bool(items)

    def _live_create_product(
        self,
        *,
        name: str,
        active: bool,
        brand: str,
        xml_id: str,
        credential_ref: str,
        purchase_price: str = "",
        purchase_price_currency: str = "",
        extra_fields: dict | None = None,
    ):
        fields: dict = {
            "iblockId": self._require_catalog_iblock_id(),
            "name": name,
            "active": "Y" if active else "N",
            "xmlId": xml_id,
        }
        if brand:
            if _BRAND_PROPERTY is None:
                raise IntegrationNotConfiguredError("bitrix_brand_property_not_verified")
            fields[_BRAND_PROPERTY.select_key] = brand
        if purchase_price:
            # Native catalog.product fields (Block 5.6 follow-up defect
            # closure) -- structurally separate from retail selling price,
            # which is only ever written via ``catalog.price.add`` below.
            fields[schema.PURCHASING_PRICE_FIELD] = purchase_price
            fields[schema.PURCHASING_CURRENCY_FIELD] = purchase_price_currency or "RUB"
        if extra_fields:
            # Complete-product-card follow-up pass: section/physical/
            # content/media/characteristics -- already validated fail-
            # closed by ``_optional_product_fields`` before this was ever
            # called; merged last so it can never silently clobber the
            # base identity fields above (none of these keys overlap).
            fields.update(extra_fields)
        data = self.client.call(
            "catalog.product.add", credential_ref=credential_ref, params={"fields": fields}, idempotent=False
        )
        # Production defect closure (product_create_malformed_response,
        # HTTP 200 on both catalog.product.list and catalog.product.add):
        # Bitrix's documented REST contract for catalog.product.add nests
        # the created product under "element", NOT "product" -- unlike
        # catalog.product.offer.add ("offer") and catalog.price.add
        # ("price"), which already matched. This one call used the wrong
        # singular key, so a genuinely successful create was never
        # recognized: HTTP 200 was reached, but no concrete id could ever
        # be extracted from it, so this always failed closed rather than
        # ever falsely reporting WRITE_VERIFIED.
        product_id = self._extract_id(data, singular_key="element")
        if product_id is None:
            self._log_malformed_response("catalog.product.add", data, xml_id=xml_id)
            raise BitrixValidationError(
                "product_create_malformed_response",
                message=self._malformed_response_message("catalog.product.add", data),
            )
        return product_id

    def _live_create_offer(self, *, parent_id, name: str, active: bool, sku: str, credential_ref: str):
        if _ARTICLE_PROPERTY is None:
            raise IntegrationNotConfiguredError("bitrix_article_property_not_verified")
        fields: dict = {
            "iblockId": self._require_offers_iblock_id(),
            # The CML2_LINK (property 279) parent-product relationship is
            # exposed/accepted via the REST ``parentId`` field, never a raw
            # ``property279`` value -- symmetric with how
            # catalog.product.offer.list already exposes it (see
            # integrations.bitrix.schema.CML2_LINK_REST_FIELD).
            "parentId": self._as_bitrix_id(parent_id),
            "name": name,
            "active": "Y" if active else "N",
            _ARTICLE_PROPERTY.select_key: sku,
        }
        data = self.client.call(
            "catalog.product.offer.add", credential_ref=credential_ref, params={"fields": fields}, idempotent=False
        )
        offer_id = self._extract_id(data, singular_key="offer")
        if offer_id is None:
            self._log_malformed_response("catalog.product.offer.add", data)
            raise BitrixValidationError(
                "offer_create_malformed_response",
                message=self._malformed_response_message("catalog.product.offer.add", data),
            )
        return offer_id

    def _live_create_price(self, *, product_id, price_type_id: int, amount: str, currency: str, credential_ref: str) -> None:
        fields = {
            "productId": self._as_bitrix_id(product_id),
            "catalogGroupId": price_type_id,
            "price": amount,
            "currency": currency,
        }
        data = self.client.call(
            "catalog.price.add", credential_ref=credential_ref, params={"fields": fields}, idempotent=False
        )
        if self._extract_id(data, singular_key="price") is None:
            self._log_malformed_response("catalog.price.add", data)
            raise BitrixValidationError(
                "price_create_malformed_response",
                message=self._malformed_response_message("catalog.price.add", data),
            )

    @staticmethod
    def _as_bitrix_id(value):
        text = str(value)
        return int(text) if text.lstrip("-").isdigit() else value

    @staticmethod
    def _extract_id(data: dict, *, singular_key: str):
        """Bitrix's ``catalog.*`` REST family consistently nests a create/
        get response under the entity's singular name (mirrors this same
        family's own list responses, e.g. ``catalog.product.offer.list`` ->
        ``{"offers": [...]}}``, already relied on elsewhere in this module
        -- catalog.product.add/offer.add/price.add -> ``{"<entity>": {...,
        "id": ...}}``). Falls back to a flatter ``{"id": ...}`` shape
        defensively; returns None (never a guessed/fabricated id) if
        neither is present so the caller fails closed instead of
        proceeding with an unverified id.
        """
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        entity = result.get(singular_key)
        if isinstance(entity, dict) and entity.get("id") is not None:
            return entity["id"]
        if result.get("id") is not None:
            return result["id"]
        return None

    @staticmethod
    def _safe_response_shape(data: dict) -> dict:
        """Bounded, sanitized description of an unexpected Bitrix REST
        response -- top-level key NAMES only (never values, which could in
        principle echo request data), truncated defensively. Never
        includes the webhook URL, credentials, or authorization data --
        those never appear in a Bitrix REST response body in the first
        place, and this only ever looks at ``data``/``data["result"]``."""
        top_level_keys = sorted(str(k) for k in data.keys())[:20]
        result = data.get("result")
        result_keys = sorted(str(k) for k in result.keys())[:20] if isinstance(result, dict) else None
        shape: dict = {"top_level_keys": top_level_keys, "result_keys": result_keys}
        if isinstance(data.get("error"), (str, int)) or isinstance(data.get("error_description"), str):
            shape["error"] = str(data.get("error"))[:200]
            shape["error_description"] = str(data.get("error_description"))[:200]
        return shape

    @classmethod
    def _malformed_response_message(cls, method: str, data: dict) -> str:
        shape = cls._safe_response_shape(data)
        return f"bitrix REST method {method} returned HTTP 200 without a recognizable created-entity id; observed response shape: {shape}"

    @classmethod
    def _log_malformed_response(cls, method: str, data: dict, *, xml_id: str = "") -> None:
        """Production diagnostics closure: HTTP 200 alone is never treated
        as proof of success (see the callers above, which always require a
        concrete extracted id before proceeding) -- this additionally
        makes the exact unexpected response SHAPE observable in
        application logs (captured by Railway) the moment it happens,
        without waiting for another production incident report. ``xml_id``
        (when supplied) already embeds this write's idempotency key --
        see ``_idempotency_xml_id`` -- so it doubles as the correlation
        handle back to the originating request without threading a new
        parameter through the whole call chain."""
        shape = cls._safe_response_shape(data)
        logger.warning(
            "bitrix_malformed_create_response method=%s xml_id=%s shape=%s",
            method,
            xml_id or "(n/a)",
            shape,
        )
