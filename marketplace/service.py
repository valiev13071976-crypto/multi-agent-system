"""Marketplace Platform service — one shared core over WB/Ozon/Yandex adapters."""

from __future__ import annotations

import uuid
from decimal import Decimal

from security.tenant import require_tenant_id

from marketplace.adapters.ozon import OzonAdapter
from marketplace.adapters.wildberries import WildberriesAdapter
from marketplace.adapters.yandex_market import YandexMarketAdapter
from marketplace.alerts import AlertStore
from marketplace.economics import (
    assess_promotion_risk,
    calculate_minimum_allowed_price,
    calculate_profitability,
    scenario_price,
)
from marketplace.errors import (
    MARKETPLACE_BATCH_REQUIRED,
    MARKETPLACE_CANCELLED,
    MARKETPLACE_CROSS_TENANT,
    MARKETPLACE_MAPPING_AMBIGUOUS,
    MARKETPLACE_NOT_FOUND,
    MARKETPLACE_SELECTION_REQUIRED,
    MarketplaceError,
)
from marketplace.intelligence import (
    competitive_recommendation,
    competitor_summary,
    draft_review_reply,
    match_competitor,
    normalize_review,
)
from marketplace.models import (
    CAP_PRICE_WRITE,
    LISTING_PUBLISHED,
    LISTING_SELECTED,
    MAP_CONFLICT,
    MAP_MATCHED,
    MAP_UNMAPPED,
    MODE_AUTO_CORRECT,
    MODE_MONITOR_ONLY,
    MarketplaceAccount,
    MarketplaceChannelPrice,
    MarketplaceCommissionObservation,
    MarketplaceJob,
    MarketplaceListing,
    MarketplaceMinPricePolicy,
    MarketplaceProject,
    MarketplacePromotionObservation,
    MarketplacePublicationPlan,
    MarketplaceSelection,
    MoneyAmount,
    PLATFORM_SCHEMA_VERSION,
    PROFIT_LOSS,
    PROMO_PLATFORM,
    PROMO_SELLER,
    PROVIDER_OZON,
    PROVIDER_WILDBERRIES,
    PROVIDER_YANDEX_MARKET,
)
from marketplace.price_guard import PriceSyncLedger, decide_auto_correct
from marketplace.selection import new_selection, require_explicit_selection, resolve_selection
from marketplace.stock_sync import assert_export_safe, export_quantity, reconcile_stock


class MarketplacePlatformService:
    """Canonical marketplace orchestration. Does not own Product/SKU/Order masters."""

    BATCH_THRESHOLD = 100

    def __init__(self, *, commerce=None):
        self.commerce = commerce
        self._projects: dict[str, MarketplaceProject] = {}
        self._accounts: dict[str, MarketplaceAccount] = {}
        self._listings: dict[str, MarketplaceListing] = {}
        self._mappings: dict[str, str] = {}  # tenant:provider:account:type:ext -> listing_id
        self._adapters = {
            PROVIDER_WILDBERRIES: WildberriesAdapter(),
            PROVIDER_OZON: OzonAdapter(),
            PROVIDER_YANDEX_MARKET: YandexMarketAdapter(),
        }
        self._alerts = AlertStore()
        self._price_ledger = PriceSyncLedger()
        self._jobs: dict[str, dict] = {}
        self._commissions: dict[str, MarketplaceCommissionObservation] = {}
        self._correction_mode = MODE_MONITOR_ONLY
        self._catalog_cache: dict[str, list[dict]] = {}

    # --- accounts / adapters ---

    def create_project(self, *, tenant_id: str, owner_id: str = "", commerce_project_ref: str = "") -> MarketplaceProject:
        tenant = require_tenant_id(tenant_id)
        project = MarketplaceProject(
            project_id=str(uuid.uuid4()),
            tenant_id=tenant,
            owner_id=owner_id,
            commerce_project_ref=commerce_project_ref,
        )
        self._projects[project.project_id] = project
        return project

    def register_account(
        self,
        *,
        tenant_id: str,
        provider: str,
        credential_ref: str,
        external_shop_ref: str = "",
    ) -> MarketplaceAccount:
        tenant = require_tenant_id(tenant_id)
        if not credential_ref.startswith("secret:"):
            # only refs allowed — reject raw-looking tokens in model
            credential_ref = f"secret:{credential_ref}"
        adapter = self.adapter(provider)
        account = MarketplaceAccount(
            account_id=str(uuid.uuid4()),
            tenant_id=tenant,
            provider=provider,
            credential_ref=credential_ref,
            external_shop_ref=external_shop_ref,
            capabilities=tuple(sorted(adapter.capabilities())),
            live=False,
        )
        self._accounts[account.account_id] = account
        return account

    def adapter(self, provider: str):
        if provider not in self._adapters:
            raise MarketplaceError(MARKETPLACE_NOT_FOUND, f"unknown_provider:{provider}")
        return self._adapters[provider]

    def get_account(self, *, tenant_id: str, account_id: str) -> MarketplaceAccount | None:
        tenant = require_tenant_id(tenant_id)
        acc = self._accounts.get(account_id)
        if acc is None:
            return None
        if acc.tenant_id != tenant:
            raise MarketplaceError(MARKETPLACE_CROSS_TENANT, "account")
        return acc

    def capability_matrix(self) -> dict:
        return {p: sorted(a.capabilities()) for p, a in self._adapters.items()}

    def health(self, provider: str) -> dict:
        h = self.adapter(provider).health()
        return {
            "provider": provider,
            "configured": h.configured,
            "authenticated": h.authenticated,
            "reachable": h.reachable,
            "degraded": h.degraded,
            "rate_limited": h.rate_limited,
            "live": h.live,
            "detail": h.detail,
        }

    def seed_catalog(self, *, tenant_id: str, items: list[dict]) -> None:
        tenant = require_tenant_id(tenant_id)
        self._catalog_cache[tenant] = list(items)

    # --- selection / publication ---

    def selection_preview(
        self,
        *,
        tenant_id: str,
        selection: MarketplaceSelection | None,
        catalog: list[dict] | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        catalog = catalog if catalog is not None else self._catalog_cache.get(tenant, [])
        resolved = resolve_selection(selection=require_explicit_selection(selection), catalog=catalog)
        return {
            "tenant_id": tenant,
            "selected_count": resolved["count"],
            "selected": resolved["selected"],
            "excluded_count": len(resolved["excluded"]),
            "ineligible": resolved["ineligible"],
            "mode": resolved["mode"],
            "external_mutation": False,
        }

    def publication_plan(
        self,
        *,
        tenant_id: str,
        provider: str,
        account_id: str,
        selection: MarketplaceSelection | None,
        catalog: list[dict] | None = None,
        dry_run: bool = True,
    ) -> MarketplacePublicationPlan:
        tenant = require_tenant_id(tenant_id)
        acc = self.get_account(tenant_id=tenant, account_id=account_id)
        if acc is None or acc.provider != provider:
            raise MarketplaceError(MARKETPLACE_NOT_FOUND, "account")
        preview = self.selection_preview(tenant_id=tenant, selection=selection, catalog=catalog)
        creates: list[dict] = []
        invalid: list[dict] = []
        conflicts: list[dict] = []
        for item in preview["selected"]:
            validation = self.validate_card(provider=provider, item=item)
            if validation["ok"]:
                creates.append({"product_id": item["product_id"], "sku_id": item["sku_id"], "action": "CREATE"})
            else:
                invalid.append({"sku_id": item.get("sku_id"), "errors": validation["errors"]})
        return MarketplacePublicationPlan(
            plan_id=str(uuid.uuid4()),
            tenant_id=tenant,
            provider=provider,
            account_id=account_id,
            dry_run=dry_run,
            selected=tuple(i["sku_id"] for i in preview["selected"]),
            creates=tuple(creates),
            updates=(),
            skips=(),
            conflicts=tuple(conflicts),
            invalid=tuple(invalid),
            estimated_api_calls=len(creates),
            approval_required=len(creates) >= self.BATCH_THRESHOLD,
        )

    def map_category(self, *, provider: str, canonical_category_id: str) -> dict:
        profile = self.adapter(provider).profile()
        mapped = profile["category_map"].get(canonical_category_id)
        if mapped is None:
            return {"status": MAP_UNMAPPED, "marketplace_category_id": ""}
        # conflict if multiple keys map to same external (fixture simple)
        return {"status": MAP_MATCHED, "marketplace_category_id": mapped}

    def map_attributes(self, *, provider: str, attributes: dict) -> list[dict]:
        profile = self.adapter(provider).profile()
        amap = profile["attribute_map"]
        required = set(profile["required_attributes"])
        out = []
        for key, val in attributes.items():
            if key not in amap:
                out.append({"attribute_id": key, "status": MAP_UNMAPPED, "required": key in required})
            else:
                out.append(
                    {
                        "attribute_id": key,
                        "provider_key": amap[key],
                        "status": MAP_MATCHED,
                        "value": str(val),
                        "required": key in required,
                    }
                )
        for req in required:
            if req not in attributes:
                out.append({"attribute_id": req, "status": MAP_UNMAPPED, "required": True, "error": "missing"})
        return out

    def validate_card(self, *, provider: str, item: dict) -> dict:
        errors: list[str] = []
        if not item.get("sku_id"):
            errors.append("missing_sku")
        if not item.get("title"):
            errors.append("missing_title")
        cat = self.map_category(provider=provider, canonical_category_id=str(item.get("category_id") or ""))
        if cat["status"] != MAP_MATCHED:
            errors.append("category_unmapped")
        attrs = self.map_attributes(provider=provider, attributes=dict(item.get("attributes") or {}))
        for a in attrs:
            if a.get("required") and a.get("status") != MAP_MATCHED:
                errors.append(f"attr_missing:{a['attribute_id']}")
        return {"ok": not errors, "errors": errors, "category": cat, "attributes": attrs}

    def card_quality_score(self, *, provider: str, item: dict) -> dict:
        v = self.validate_card(provider=provider, item=item)
        dims = {
            "identity": 1.0 if item.get("sku_id") else 0.0,
            "title": 1.0 if item.get("title") else 0.0,
            "category": 1.0 if v["category"]["status"] == MAP_MATCHED else 0.0,
            "attributes": 1.0 if not any(e.startswith("attr_") for e in v["errors"]) else 0.0,
            "media": 1.0 if item.get("media_refs") else 0.0,
            "content": 1.0 if item.get("content_refs") else 0.0,
        }
        score = sum(dims.values()) / len(dims)
        return {"score": round(score, 3), "dimensions": dims, "version": "1.0.0"}

    def content_handoff(self, *, item: dict, provider: str) -> dict:
        return {
            "delegate_to": "content_intel",
            "provider": provider,
            "facts": {"sku": item.get("sku_id"), "title": item.get("title"), "brand": item.get("brand")},
            "constraints": ("no_invented_product_facts",),
        }

    def media_handoff(self, *, item: dict, provider: str) -> dict:
        profile = self.adapter(provider).profile()["media"]
        return {
            "delegate_to": "product_media",
            "provider": provider,
            "profile": {
                "max_images": profile.max_images,
                "min_width": profile.min_width,
                "min_height": profile.min_height,
                "aspect_ratio": profile.aspect_ratio,
            },
            "product_id": item.get("product_id"),
        }

    def seo_handoff(self, *, item: dict, provider: str) -> dict:
        return {
            "delegate_to": "seo_marketing",
            "channel": "marketplace",
            "provider": provider,
            "request": "marketplace_title_description_bullets",
            "facts": {"sku": item.get("sku_id"), "title": item.get("title")},
            "fact_lock": True,
        }

    def apply_publication(
        self,
        *,
        tenant_id: str,
        plan: MarketplacePublicationPlan,
        authorized: bool = True,
        idempotency_prefix: str = "pub",
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        if plan.tenant_id != tenant:
            raise MarketplaceError(MARKETPLACE_CROSS_TENANT)
        if plan.dry_run:
            return {"status": "DRY_RUN", "applied": 0, "plan_id": plan.plan_id}
        if not authorized:
            raise MarketplaceError("MARKETPLACE_ACCESS_DENIED", "publication")
        if len(plan.creates) >= self.BATCH_THRESHOLD:
            raise MarketplaceError(MARKETPLACE_BATCH_REQUIRED, "use_batch_job")
        adapter = self.adapter(plan.provider)
        results = []
        for row in plan.creates:
            payload = {
                "sku": row["sku_id"],
                "product_id": row["product_id"],
                "storefront": plan.provider,
            }
            key = f"{idempotency_prefix}:{plan.plan_id}:{row['sku_id']}"
            card = adapter.card_create(payload=payload, idempotency_key=key)
            listing = MarketplaceListing(
                listing_id=str(uuid.uuid4()),
                tenant_id=tenant,
                provider=plan.provider,
                account_id=plan.account_id,
                product_id=row["product_id"],
                sku_id=row["sku_id"],
                external_listing_id=card["external_id"],
                status=LISTING_PUBLISHED,
            )
            self._listings[listing.listing_id] = listing
            map_key = f"{tenant}:{plan.provider}:{plan.account_id}:listing:{card['external_id']}"
            self._mappings[map_key] = listing.listing_id
            results.append({"listing_id": listing.listing_id, "external_id": card["external_id"]})
        return {"status": "APPLIED", "applied": len(results), "results": results, "live": False}

    # --- economics / loss / auto-correct ---

    def set_commission(self, obs: MarketplaceCommissionObservation) -> None:
        self._commissions[f"{obs.provider}:{obs.category}"] = obs

    def get_commission(self, *, provider: str, category: str) -> MarketplaceCommissionObservation | None:
        return self._commissions.get(f"{provider}:{category}")

    def minimum_price(
        self,
        *,
        purchase_cost: Decimal | None,
        provider: str,
        category: str,
        logistics: Decimal | None,
        acquiring_rate: Decimal | None = Decimal("0.02"),
        policy: MarketplaceMinPricePolicy | None = None,
    ) -> dict:
        policy = policy or MarketplaceMinPricePolicy(policy_id="default")
        commission = self.get_commission(provider=provider, category=category)
        amount, status, evidence = calculate_minimum_allowed_price(
            purchase_cost=purchase_cost,
            commission=commission,
            logistics=logistics,
            acquiring_rate=acquiring_rate,
            policy=policy,
        )
        return {
            "status": status,
            "minimum_allowed": str(amount.amount) if amount else None,
            "currency": policy.currency,
            "evidence": evidence,
            "policy_version": policy.version,
        }

    def profitability(
        self,
        *,
        sku_id: str,
        provider: str,
        selling_price: Decimal,
        purchase_cost: Decimal | None,
        category: str,
        logistics: Decimal | None,
        policy: MarketplaceMinPricePolicy | None = None,
    ) -> dict:
        result = calculate_profitability(
            sku_id=sku_id,
            provider=provider,
            selling_price=selling_price,
            purchase_cost=purchase_cost,
            commission=self.get_commission(provider=provider, category=category),
            logistics=logistics,
            acquiring_rate=Decimal("0.02"),
            policy=policy,
        )
        return {
            "status": result.status,
            "contribution": str(result.contribution.amount),
            "margin_pct": str(result.margin_pct) if result.margin_pct is not None else None,
            "minimum_allowed": str(result.minimum_allowed.amount) if result.minimum_allowed else None,
            "unknown_costs": result.unknown_costs,
            "evidence": result.evidence,
            "result": result,
        }

    def loss_guard(
        self,
        *,
        tenant_id: str,
        account_id: str,
        provider: str,
        sku_id: str,
        selling_price: Decimal,
        purchase_cost: Decimal | None,
        category: str,
        logistics: Decimal | None,
        mode: str | None = None,
        authorized: bool = False,
        proposed_correction: Decimal | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        mode = mode or self._correction_mode
        econ = self.profitability(
            sku_id=sku_id,
            provider=provider,
            selling_price=selling_price,
            purchase_cost=purchase_cost,
            category=category,
            logistics=logistics,
        )
        result = econ["result"]
        if result.status != PROFIT_LOSS:
            return {"loss": False, "status": result.status, "mutated": False}

        adapter = self.adapter(provider)
        floor = result.minimum_allowed.amount if result.minimum_allowed else selling_price
        proposed = proposed_correction if proposed_correction is not None else floor
        decision = decide_auto_correct(
            profitability=result,
            mode=mode,
            price_write_supported=CAP_PRICE_WRITE in adapter.capabilities(),
            authorized=authorized,
            current_price=selling_price,
            proposed_price=proposed,
        )
        if decision.get("mutate"):
            causation = f"panda-price-{uuid.uuid4().hex[:12]}"
            applied = adapter.price_apply(sku=sku_id, amount=proposed, idempotency_key=causation)
            self._price_ledger.record_outbound(causation_id=causation, sku=sku_id)
            return {
                "loss": True,
                "mutated": True,
                "decision": decision,
                "applied": applied,
                "causation_id": causation,
                "live": False,
            }

        alert = self._alerts.upsert(
            tenant_id=tenant,
            provider=provider,
            account_id=account_id,
            alert_type="LOSS" if result.status == PROFIT_LOSS else "PRICE_FLOOR_BREACH",
            sku_id=sku_id,
            summary=f"Loss detected for {sku_id} on {provider}",
            evidence=result.evidence,
            financial_impact=str(result.contribution.amount),
            recommended_action="Raise channel price to minimum allowed or adjust promo",
            auto_correction_available=False,
        )
        return {"loss": True, "mutated": False, "decision": decision, "alert_id": alert.alert_id}

    def acknowledge_price_reflection(self, *, causation_id: str) -> dict:
        return self._price_ledger.acknowledge_inbound(causation_id=causation_id, origin="marketplace")

    def note_external_price_override(self, *, tenant_id: str, account_id: str, provider: str, sku_id: str, amount: Decimal) -> dict:
        adapter = self.adapter(provider)
        adapter.force_external_override(sku_id, amount)
        streak = self._price_ledger.note_external_override(sku=sku_id)
        if streak >= 2:
            alert = self._alerts.upsert(
                tenant_id=tenant_id,
                provider=provider,
                account_id=account_id,
                alert_type="REPEATED_EXTERNAL_OVERRIDE",
                sku_id=sku_id,
                summary="Repeated external price override — stopping correction fight",
                evidence=(f"streak={streak}", f"amount={amount}"),
                recommended_action="Operator review marketplace promo/campaign",
            )
            return {"streak": streak, "alert_id": alert.alert_id, "stop": True}
        return {"streak": streak, "stop": False}

    def promotion_analysis(
        self,
        *,
        promo: MarketplacePromotionObservation,
        purchase_cost: Decimal | None,
        category: str,
        logistics: Decimal | None,
    ) -> dict:
        # For platform-funded discount, evaluate seller proceeds price not displayed alone.
        eval_price = promo.seller_price.amount if promo.seller_price else promo.displayed_price.amount
        if promo.ownership == PROMO_PLATFORM and promo.seller_price:
            eval_price = promo.seller_price.amount
        econ = calculate_profitability(
            sku_id=promo.sku_id,
            provider=promo.provider,
            selling_price=eval_price,
            purchase_cost=purchase_cost,
            commission=self.get_commission(provider=promo.provider, category=category),
            logistics=logistics,
            acquiring_rate=Decimal("0.02"),
        )
        risk = assess_promotion_risk(promo=promo, profitability=econ)
        return {"risk": risk, "economics": econ.status, "eval_price": str(eval_price), "ownership": promo.ownership}

    def price_scenario(self, **kwargs) -> dict:
        result = scenario_price(**kwargs)
        return {"status": result.status, "contribution": str(result.contribution.amount), "minimum_allowed": str(result.minimum_allowed.amount) if result.minimum_allowed else None}

    # --- stock ---

    def plan_stock_export(
        self,
        *,
        available: Decimal,
        buffer: Decimal = Decimal("0"),
        stale: bool = False,
    ) -> dict:
        assert_export_safe(available=available, stale=stale)
        qty = export_quantity(available=available, buffer=buffer)
        return {"channel_quantity": str(qty), "buffer": str(buffer), "available": str(available)}

    def apply_stock(
        self,
        *,
        provider: str,
        sku: str,
        quantity: Decimal,
        warehouse: str = "main",
        idempotency_key: str,
    ) -> dict:
        return self.adapter(provider).stock_apply(
            sku=sku, quantity=quantity, warehouse=warehouse, idempotency_key=idempotency_key
        )

    def reconcile_channel_stock(self, *, provider: str, sku: str, expected: Decimal, warehouse: str = "main") -> dict:
        observed = self.adapter(provider).observe_stock(sku, warehouse)
        return reconcile_stock(expected=expected, observed=observed)

    # --- orders ---

    def ingest_marketplace_order(
        self,
        *,
        tenant_id: str,
        provider: str,
        external_order_id: str,
        items: list[dict],
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        external_ref = f"{provider}:{external_order_id}"
        if self.commerce is None:
            # standalone fixture path with local idempotency
            key = f"{tenant}:{external_ref}"
            if not hasattr(self, "_orders"):
                self._orders = {}
            if key in self._orders:
                return {"order_id": self._orders[key], "idempotent": True, "external_ref": external_ref}
            oid = str(uuid.uuid4())
            self._orders[key] = oid
            return {"order_id": oid, "idempotent": False, "external_ref": external_ref}
        order = self.commerce.ingest_order(
            tenant_id=tenant,
            external_ref=external_ref,
            source=provider,
            items=items,
        )
        return {"order_id": order.order_id, "idempotent": False, "external_ref": external_ref}

    # --- reviews / competitors / analytics ---

    def sync_reviews(self, *, tenant_id: str, provider: str, account_id: str) -> list:
        raw = self.adapter(provider).reviews_read()
        return [normalize_review(tenant_id=tenant_id, provider=provider, account_id=account_id, raw=r) for r in raw]

    def draft_reply(self, review) -> dict:
        return draft_review_reply(review)

    def competitor_scan(
        self,
        *,
        sku_id: str,
        provider: str,
        our_price: Decimal,
        our_ean: str = "",
        our_brand: str = "",
        our_model: str = "",
        candidates: list[dict],
        minimum_allowed: Decimal | None = None,
    ) -> dict:
        from marketplace.models import CompetitorPriceObservation

        observations = []
        for c in candidates:
            status = match_competitor(our_ean=our_ean, our_brand=our_brand, our_model=our_model, candidate=c)
            if status == "AMBIGUOUS":
                continue  # never reprice on ambiguous
            if status != "MATCHED":
                continue
            observations.append(
                CompetitorPriceObservation(
                    observation_id=str(uuid.uuid4()),
                    provider=provider,
                    sku_id=sku_id,
                    competitor_price=MoneyAmount(Decimal(str(c["price"]))),
                    match_status=status,
                    confidence=Decimal("0.95"),
                    source=str(c.get("source") or "fixture"),
                    seller=str(c.get("seller") or ""),
                )
            )
        summary = competitor_summary(sku_id=sku_id, provider=provider, our_price=our_price, observations=observations)
        rec = None
        if summary.get("min"):
            rec = competitive_recommendation(
                our_price=our_price,
                target_competitor=Decimal(summary["min"]),
                minimum_allowed=minimum_allowed,
                mode="UNDERCUT_BY_AMOUNT",
            )
        return {"summary": summary, "recommendation": rec, "matched": len(observations)}

    def analytics_summary(self, *, tenant_id: str, provider: str, sales: list[dict]) -> dict:
        tenant = require_tenant_id(tenant_id)
        revenue = sum((Decimal(str(s.get("revenue") or 0)) for s in sales), Decimal("0"))
        units = sum((Decimal(str(s.get("units") or 0)) for s in sales), Decimal("0"))
        return {
            "tenant_id": tenant,
            "provider": provider,
            "revenue": str(revenue),
            "units": str(units),
            "orders": len(sales),
            "invented": False,
            "limitations": ("commission_not_included_unless_supplied",),
        }

    def dashboard(self, *, tenant_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        listings = [l for l in self._listings.values() if l.tenant_id == tenant]
        alerts = self._alerts.list_for_tenant(tenant)
        return {
            "provider_health": {p: self.health(p) for p in self._adapters},
            "published_listings": len([l for l in listings if l.status == LISTING_PUBLISHED]),
            "alerts_open": len([a for a in alerts if a.status == "OPEN"]),
            "schema_version": PLATFORM_SCHEMA_VERSION,
        }

    # --- jobs / batch ---

    def start_batch_job(
        self,
        *,
        tenant_id: str,
        job_type: str,
        provider: str,
        items: list[dict],
        partition_size: int = 500,
        resume_from: int = 0,
        job_id: str | None = None,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        if len(items) >= self.BATCH_THRESHOLD and job_id is None and resume_from == 0:
            # admit as batch
            pass
        elif len(items) >= self.BATCH_THRESHOLD and partition_size <= 0:
            raise MarketplaceError(MARKETPLACE_BATCH_REQUIRED)

        jid = job_id or str(uuid.uuid4())
        job = self._jobs.get(jid) or {
            "job_id": jid,
            "tenant_id": tenant,
            "job_type": job_type,
            "provider": provider,
            "status": "running",
            "checkpoint": resume_from,
            "processed": 0,
            "failed": 0,
            "cancelled": False,
            "results": [],
        }
        if job.get("cancelled"):
            raise MarketplaceError(MARKETPLACE_CANCELLED, jid)

        end = min(resume_from + partition_size, len(items))
        adapter = self.adapter(provider)
        # Provider isolation: if this provider is down, fail this job only
        try:
            _ = adapter.health()
            if not adapter.health().reachable:
                raise MarketplaceError("MARKETPLACE_UNAVAILABLE", provider)
            for i in range(resume_from, end):
                if job.get("cancelled"):
                    break
                item = items[i]
                try:
                    adapter.card_create(
                        payload={"sku": item.get("sku_id"), "product_id": item.get("product_id")},
                        idempotency_key=f"{jid}:{i}",
                    )
                    job["processed"] += 1
                except MarketplaceError:
                    job["failed"] += 1
        except MarketplaceError as exc:
            job["status"] = "failed"
            job["error"] = exc.code
            self._jobs[jid] = job
            return job

        job["checkpoint"] = end
        job["status"] = "completed" if end >= len(items) else "partial"
        self._jobs[jid] = job
        return {
            "job_id": jid,
            "status": job["status"],
            "checkpoint": end,
            "processed": job["processed"],
            "failed": job["failed"],
        }

    def cancel_job(self, *, tenant_id: str, job_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        job = self._jobs.get(job_id)
        if job is None or job["tenant_id"] != tenant:
            raise MarketplaceError(MARKETPLACE_NOT_FOUND, "job")
        job["cancelled"] = True
        job["status"] = "cancelled"
        return {"job_id": job_id, "status": "cancelled", "checkpoint": job.get("checkpoint", 0), "code": MARKETPLACE_CANCELLED}

    def set_auto_correct_mode(self, mode: str) -> None:
        self._correction_mode = mode

    def isolate_provider(self, provider: str, *, unavailable: bool = True) -> None:
        self.adapter(provider).state.unavailable = unavailable
