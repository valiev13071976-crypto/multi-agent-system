"""Provider connectivity foundations — contracts only, no business workflows."""

from __future__ import annotations

from dataclasses import dataclass, field

from integrations.contracts import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_BEARER,
    AUTH_OAUTH2,
    AUTH_SERVICE_ACCOUNT,
)


@dataclass(frozen=True)
class ProviderConnectivityContract:
    provider_id: str
    display_name: str
    integration_type: str
    adapter_id: str
    supported_auth: tuple[str, ...]
    default_read_capabilities: tuple[str, ...]
    default_write_capabilities: tuple[str, ...]
    write_default_deny: bool = False
    notes: str = ""
    health_probe_path: str = "/health"


MOYSKLAD = ProviderConnectivityContract(
    provider_id="moysklad",
    display_name="МойСклад",
    integration_type="inventory_erp",
    adapter_id="moysklad",
    supported_auth=(AUTH_BEARER, AUTH_BASIC),
    default_read_capabilities=("moysklad.read",),
    default_write_capabilities=("moysklad.write",),
    health_probe_path="/api/remap/1.2/entity/assortment",
)

ONEC = ProviderConnectivityContract(
    provider_id="onec",
    display_name="1С",
    integration_type="erp",
    adapter_id="onec",
    supported_auth=(AUTH_BASIC, AUTH_OAUTH2, AUTH_SERVICE_ACCOUNT),
    default_read_capabilities=("onec.read",),
    default_write_capabilities=("onec.write",),
    notes="Transport-agnostic: HTTP REST/OData / custom API / file exchange later",
)

ERP_WMS = ProviderConnectivityContract(
    provider_id="erp_wms",
    display_name="Generic ERP/WMS",
    integration_type="erp_wms",
    adapter_id="erp_wms",
    supported_auth=(AUTH_API_KEY, AUTH_BEARER, AUTH_OAUTH2),
    default_read_capabilities=("erp.read", "wms.read"),
    default_write_capabilities=("erp.write", "wms.write"),
)

BITRIX = ProviderConnectivityContract(
    provider_id="bitrix",
    display_name="Bitrix24",
    integration_type="cms_crm",
    adapter_id="bitrix",
    supported_auth=(AUTH_API_KEY, AUTH_OAUTH2),
    default_read_capabilities=("bitrix.read",),
    default_write_capabilities=("bitrix.catalog.write", "bitrix.write"),
)

ASPRO = ProviderConnectivityContract(
    provider_id="aspro",
    display_name="Aspro",
    integration_type="cms",
    adapter_id="aspro",
    supported_auth=(AUTH_API_KEY, AUTH_OAUTH2),
    default_read_capabilities=("aspro.read", "bitrix.read"),
    default_write_capabilities=("aspro.write",),
    notes="Thin specialization over Bitrix",
)

EDO = ProviderConnectivityContract(
    provider_id="edo",
    display_name="ЭДО",
    integration_type="edo",
    adapter_id="edo",
    supported_auth=(AUTH_OAUTH2, AUTH_API_KEY, AUTH_SERVICE_ACCOUNT),
    default_read_capabilities=("edo.read",),
    default_write_capabilities=("edo.send",),
    notes="Diadoc / SBIS / EDO Lite connectivity foundation only",
)

FISCAL = ProviderConnectivityContract(
    provider_id="fiscal",
    display_name="KKT/OFD",
    integration_type="fiscal",
    adapter_id="fiscal",
    supported_auth=(AUTH_API_KEY, AUTH_SERVICE_ACCOUNT),
    default_read_capabilities=("fiscal.read", "ofd.read"),
    default_write_capabilities=("kkt.write",),
    notes="Connectivity only — no fiscal business decisions",
)

BANK = ProviderConnectivityContract(
    provider_id="bank",
    display_name="Bank",
    integration_type="bank",
    adapter_id="bank",
    supported_auth=(AUTH_OAUTH2, AUTH_API_KEY, AUTH_SERVICE_ACCOUNT),
    default_read_capabilities=("bank.read", "bank.statements.read", "bank.balance.read"),
    default_write_capabilities=(),  # deny write/transfer by default
    write_default_deny=True,
    notes="Read-only by default; never store full card data",
)

PAYMENT = ProviderConnectivityContract(
    provider_id="payment_gateway",
    display_name="Payment Gateway",
    integration_type="payment",
    adapter_id="payment",
    supported_auth=(AUTH_API_KEY, AUTH_OAUTH2),
    default_read_capabilities=("payment.status.read", "payment.transaction.read"),
    default_write_capabilities=("payment.refund",),  # declared but default deny at runtime
    write_default_deny=True,
    notes="Refund capability declared; payments.execute_refund remains disabled",
)

PROVIDER_CONTRACTS: dict[str, ProviderConnectivityContract] = {
    c.provider_id: c
    for c in (MOYSKLAD, ONEC, ERP_WMS, BITRIX, ASPRO, EDO, FISCAL, BANK, PAYMENT)
}


def get_provider_contract(provider_id: str) -> ProviderConnectivityContract | None:
    return PROVIDER_CONTRACTS.get(provider_id)


class FakeProviderAdapter:
    """Test/fake connectivity adapter implementing health + safe GET hook."""

    def __init__(self, contract: ProviderConnectivityContract, *, reachable: bool = True, auth_ok: bool = True):
        self.contract = contract
        self.adapter_id = contract.adapter_id
        self.reachable = reachable
        self.auth_ok = auth_ok

    def health(self) -> dict:
        if not self.reachable:
            return {"status": "unavailable", "provider": self.contract.provider_id}
        if not self.auth_ok:
            return {"status": "auth_failed", "provider": self.contract.provider_id}
        return {"status": "healthy", "provider": self.contract.provider_id, "latency_ms": 1.0}

    def supports_write(self, capability: str) -> bool:
        if self.contract.write_default_deny:
            return False
        return capability in self.contract.default_write_capabilities
