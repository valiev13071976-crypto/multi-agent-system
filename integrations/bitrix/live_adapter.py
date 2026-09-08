"""LIVE Bitrix adapter — structurally capable, dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.adapters import FixtureAdapterState
from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.bitrix import schema
from integrations.bitrix.client import BitrixHttpClient
from integrations.bitrix.config import BitrixIntegrationConfig, load_bitrix_config
from integrations.bitrix.errors import BitrixValidationError
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter


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
        # LIVE writes are structurally implemented but blocked during engineering closure.
        self._assert_live_configured(credential_ref)
        raise IntegrationNotConfiguredError("bitrix_live_write_blocked_engineering")
