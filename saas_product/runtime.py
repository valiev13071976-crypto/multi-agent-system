"""Build SaaS product runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

from saas_product.access import ProductAuthorizationPolicy
from saas_product.billing import BillingService
from saas_product.entitlements import EntitlementService
from saas_product.metering import MeteringService
from saas_product.privacy import PrivacyService
from saas_product.service import SaaSProductService
from saas_product.sqlite_store import SqliteSaaSProductStore


@dataclass
class SaaSProductRuntime:
    service: SaaSProductService
    store: SqliteSaaSProductStore

    def close(self) -> None:
        self.store.close()


def build_saas_product_runtime(*, finops=None, env: dict | None = None, production_bundle=None) -> SaaSProductRuntime:
    source = env if env is not None else os.environ
    db_path = source.get("SAAS_PRODUCT_DB_PATH") or source.get("SIDE_EFFECT_DB_PATH") or "data/saas_product.sqlite"
    if db_path.endswith(".sqlite") and "saas" not in db_path:
        db_path = db_path.replace(".sqlite", "_saas.sqlite")
    store = SqliteSaaSProductStore(db_path)
    access = ProductAuthorizationPolicy(store.get_active_membership)
    entitlements = EntitlementService()
    billing_provider = production_bundle.billing_provider if production_bundle is not None else None
    if billing_provider is None:
        from integrations.production.adapters.billing import build_billing_provider

        billing_provider = build_billing_provider(source)
    billing = BillingService(store=store, entitlements=entitlements, provider=billing_provider)
    metering = MeteringService(store=store, finops=finops)
    privacy = PrivacyService(store=store, export_root=source.get("SAAS_EXPORT_ROOT") or "data/privacy_exports")
    email_provider = production_bundle.email_provider if production_bundle is not None else None
    service = SaaSProductService(
        store=store,
        access=access,
        billing=billing,
        entitlements=entitlements,
        metering=metering,
        privacy=privacy,
        finops=finops,
        email_provider=email_provider,
    )
    return SaaSProductRuntime(service=service, store=store)
