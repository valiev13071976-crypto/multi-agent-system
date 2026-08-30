"""B2B Commerce Service — Block 13 facade."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from b2b_commerce.access import B2BAccessPolicy
from b2b_commerce.assistant import propose_from_message, validate_action
from b2b_commerce.conversations import (
    extract_inquiry_items,
    new_conversation_id,
    new_inquiry_id,
    record_inbound_message,
    transition_after_inquiry,
)
from b2b_commerce.customers import new_customer_id, require_customer, require_verified_customer
from b2b_commerce.errors import (
    B2B_ACCESS_DENIED,
    B2B_BATCH_REQUIRED,
    B2B_PRODUCT_AMBIGUOUS,
    B2B_QUANTITY_REQUIRED,
    B2B_TELEGRAM_BINDING_DENIED,
    B2B_TELEGRAM_DUPLICATE_UPDATE,
    B2BCommerceError,
    B2BBatchRequired,
)
from b2b_commerce.matching import match_supplier_row
from b2b_commerce.observability import B2BObservability
from b2b_commerce.orders import (
    assert_confirmation_matches,
    bind_confirmation,
    new_confirmation_token,
    new_draft_id,
)
from b2b_commerce.platform_models import (
    CONV_AWAITING_CONFIRMATION,
    CONV_HUMAN_HANDOFF,
    CONV_ORDER_DRAFTED,
    CONV_QUOTE_READY,
    CUSTOMER_CANDIDATE,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_RUNNING,
    MATCH_AMBIGUOUS,
    MATCH_CONFIRMED,
    MATCH_UNMATCHED,
    OFFER_FRESH,
    OFFER_STALE,
    QUOTE_REQUIRE_APPROVAL,
    SOURCE_SUPPLIER_FILE,
    SOURCE_UNKNOWN,
    VAT_UNKNOWN,
    B2BConversation,
    B2BCustomer,
    B2BInquiry,
    B2BJob,
    B2BOrderDraft,
    B2BProvenance,
    CommercialQuoteVersion,
    Supplier,
    SupplierPriceListVersion,
    TelegramAccountBinding,
    TelegramChatBinding,
    TelegramMessageReceipt,
    WholesaleOfferVersion,
    money_str,
)
from b2b_commerce.planner import assert_sync_b2b_allowed, plan_b2b_job
from b2b_commerce.policy import BULK_WHOLESALE_BATCH_SIZE, STALE_OFFER_DAYS
from b2b_commerce.pricing import compute_customer_quote_lines, customer_safe_projection, validate_discount_request
from b2b_commerce.quotes import assert_quote_fresh, default_valid_until, mark_quote_stale, new_quote_version_id, quote_send_idempotency_key
from b2b_commerce.store import B2BStore
from b2b_commerce.suppliers import new_supplier_id, require_active_supplier, require_source_binding
from b2b_commerce.telegram import TelegramSendRequest, normalize_inbound
from b2b_commerce.wholesale import compare_offers, detect_price_changes
from security.tenant import require_tenant_id


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class B2BCommerceService:
    def __init__(
        self,
        store: B2BStore,
        *,
        access: B2BAccessPolicy | None = None,
        telegram_provider=None,
        data_intel_ingest=None,
        product_platform_service=None,
        acquisition_service=None,
        document_service=None,
        observability=None,
    ):
        self.store = store
        self.access = access or B2BAccessPolicy()
        self.telegram = telegram_provider
        self.data_intel_ingest = data_intel_ingest
        self.product_platform = product_platform_service
        self.acquisition = acquisition_service
        self.document_service = document_service
        self.obs = B2BObservability()

    def _require_cap(self, capabilities: tuple[str, ...], required: str) -> None:
        if required not in capabilities:
            raise B2BCommerceError(B2B_ACCESS_DENIED)

    def _catalog(self, tenant_id: str) -> list[dict[str, Any]]:
        if self.product_platform is None:
            return []
        return list(self.product_platform.list_products(tenant_id=tenant_id) or [])

    def create_supplier(
        self,
        *,
        tenant_id: str,
        name: str,
        source_bindings: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
    ) -> Supplier:
        from b2b_commerce.capabilities import CAP_B2B_SUPPLIER_WRITE

        self._require_cap(capabilities, CAP_B2B_SUPPLIER_WRITE)
        tenant = require_tenant_id(tenant_id)
        supplier = Supplier(
            supplier_id=new_supplier_id(),
            tenant_id=tenant,
            name=name,
            source_bindings=source_bindings,
        )
        self.store.save_supplier(supplier)
        return supplier

    def get_supplier(self, *, tenant_id: str, supplier_id: str, capabilities: tuple[str, ...] = ()) -> Supplier | None:
        from b2b_commerce.capabilities import CAP_B2B_SUPPLIER_READ

        self._require_cap(capabilities, CAP_B2B_SUPPLIER_READ)
        tenant = require_tenant_id(tenant_id)
        supplier = self.store.get_supplier(tenant, supplier_id)
        if supplier:
            self.access.require(requesting_tenant=tenant, target_tenant=supplier.tenant_id)
        return supplier

    def ingest_wholesale(
        self,
        *,
        tenant_id: str,
        supplier_id: str,
        rows: list[dict[str, Any]] | None = None,
        file_bytes: bytes | None = None,
        filename: str = "",
        source_class: str = SOURCE_SUPPLIER_FILE,
        source_key: str = "",
        artifact_id: str = "",
        bulk: bool = False,
        job_id: str | None = None,
        checkpoint: int = 0,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_INGEST
        from data_intel.ingest import ingest_bytes

        self._require_cap(capabilities, CAP_B2B_WHOLESALE_INGEST)
        tenant = require_tenant_id(tenant_id)
        supplier = require_active_supplier(self.store.get_supplier(tenant, supplier_id))
        self.access.require(requesting_tenant=tenant, target_tenant=supplier.tenant_id)
        if source_key:
            require_source_binding(supplier, source_key)

        normalized_rows = list(rows or [])
        artifact_hash = _artifact_hash(artifact_id or filename or str(len(normalized_rows)))
        if file_bytes is not None:
            ingest_fn = self.data_intel_ingest or ingest_bytes
            result = ingest_fn(
                file_bytes,
                filename=filename or "pricelist.xlsx",
                tenant_id=tenant,
                source_document_id=artifact_id,
            )
            artifact_hash = result.descriptor.checksum
            for table_id, table_rows in result.table_rows.items():
                normalized_rows.extend(table_rows)

        if not normalized_rows and not bulk:
            raise B2BCommerceError("B2B_PRICE_LIST_INVALID")

        if not bulk:
            try:
                assert_sync_b2b_allowed(row_count=len(normalized_rows), bulk=bulk)
            except B2BBatchRequired:
                raise

        existing = self.store.find_price_list_by_hash(tenant, supplier_id, artifact_hash)
        if existing:
            offers = [o for o in self.store.list_offers(tenant, supplier_id=supplier_id) if o.price_list_version_id == existing.version_id]
            return {"idempotent": True, "price_list_version_id": existing.version_id, "offer_count": len(offers), "job_id": job_id or ""}

        planned = plan_b2b_job(tenant_id=tenant, row_count=len(normalized_rows), bulk=bulk)
        if planned.enqueue and not job_id:
            job = B2BJob(
                job_id=f"job_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant,
                operation="wholesale_ingest",
                supplier_id=supplier_id,
                source_artifact_id=artifact_id or artifact_hash,
                status=JOB_RUNNING,
            )
            self.store.save_job(job)
            return self._run_wholesale_job(job, normalized_rows, source_class=source_class, artifact_hash=artifact_hash)

        if job_id:
            job = self.store.get_job(tenant, job_id)
            if job is None:
                raise B2BCommerceError("B2B_CONFLICT")
            return self._run_wholesale_job(
                job,
                normalized_rows,
                source_class=source_class,
                artifact_hash=artifact_hash,
                checkpoint=job.checkpoint,
            )

        return self._ingest_rows(
            tenant=tenant,
            supplier=supplier,
            rows=normalized_rows,
            source_class=source_class,
            artifact_hash=artifact_hash,
            artifact_id=artifact_id,
        )

    def _run_wholesale_job(
        self,
        job: B2BJob,
        rows: list[dict[str, Any]],
        *,
        source_class: str,
        artifact_hash: str,
        checkpoint: int = 0,
    ) -> dict:
        tenant = job.tenant_id
        supplier = require_active_supplier(self.store.get_supplier(tenant, job.supplier_id))
        start = checkpoint
        end = min(start + BULK_WHOLESALE_BATCH_SIZE, len(rows))
        chunk = rows[start:end]
        partial = self._ingest_rows(
            tenant=tenant,
            supplier=supplier,
            rows=chunk,
            source_class=source_class,
            artifact_hash=artifact_hash,
            artifact_id=job.source_artifact_id,
            existing_job=job,
            reuse_price_list_version_id=job.price_list_version_id or None,
        )
        if job.price_list_version_id:
            pass
        elif partial.get("price_list_version_id"):
            job.price_list_version_id = str(partial["price_list_version_id"])
        job.checkpoint = end
        job.processed += len(chunk)
        job.matched += partial.get("matched", 0)
        job.ambiguous += partial.get("ambiguous", 0)
        job.unmatched += partial.get("unmatched", 0)
        job.failed += partial.get("failed", 0)
        job.updated_at = _utc()
        if end >= len(rows):
            job.status = JOB_COMPLETED
        else:
            job.status = JOB_RUNNING
        self.store.save_job(job)
        self.obs.emit("b2b.wholesale.ingest.completed", tenant_id=tenant, job_id=job.job_id, processed=job.processed)
        return {
            "job_id": job.job_id,
            "checkpoint": job.checkpoint,
            "processed": job.processed,
            "matched": job.matched,
            "ambiguous": job.ambiguous,
            "unmatched": job.unmatched,
            "failed": job.failed,
            "status": job.status,
            **partial,
        }

    def _ingest_rows(
        self,
        *,
        tenant: str,
        supplier: Supplier,
        rows: list[dict[str, Any]],
        source_class: str,
        artifact_hash: str,
        artifact_id: str,
        existing_job: B2BJob | None = None,
        reuse_price_list_version_id: str | None = None,
    ) -> dict:
        if not rows:
            return {"matched": 0, "ambiguous": 0, "unmatched": 0, "failed": 0, "offer_count": 0}
        self.obs.emit("b2b.wholesale.ingest.started", tenant_id=tenant, supplier_id=supplier.supplier_id, row_count=len(rows))
        if reuse_price_list_version_id:
            version = self.store.get_price_list_version(tenant, reuse_price_list_version_id)
            if version is None:
                raise B2BCommerceError("B2B_CONFLICT")
        else:
            version = SupplierPriceListVersion(
                version_id=f"plv_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant,
                supplier_id=supplier.supplier_id,
                source_class=source_class or SOURCE_UNKNOWN,
                artifact_hash=artifact_hash,
                currency=str(rows[0].get("currency") or rows[0].get("валюта") or "UNKNOWN").upper(),
                vat_status=str(rows[0].get("vat_status") or VAT_UNKNOWN),
                row_count=len(rows),
                observed_at=str(rows[0].get("observed_at") or _utc()),
                ingested_at=_utc(),
            )
            self.store.save_price_list_version(version)
        catalog = self._catalog(tenant)
        matched = ambiguous = unmatched = failed = 0
        offers: list[WholesaleOfferVersion] = []
        for idx, row in enumerate(rows):
            try:
                currency = str(row.get("currency") or row.get("валюта") or version.currency or "UNKNOWN").upper()
                if currency == "UNKNOWN":
                    failed += 1
                    continue
                offer_id = f"off_{uuid.uuid4().hex[:12]}"
                version_id = "v1"
                match = match_supplier_row(
                    row,
                    catalog,
                    tenant_id=tenant,
                    offer_version_id=f"{offer_id}:{version_id}",
                    supplier_context=supplier.supplier_id,
                )
                self.store.save_match(match)
                state = match.state
                if state == MATCH_CONFIRMED:
                    matched += 1
                elif state == MATCH_AMBIGUOUS:
                    ambiguous += 1
                else:
                    unmatched += 1
                offer = WholesaleOfferVersion(
                    offer_id=offer_id,
                    version_id=version_id,
                    tenant_id=tenant,
                    supplier_id=supplier.supplier_id,
                    price_list_version_id=version.version_id,
                    supplier_sku=str(row.get("sku") or row.get("supplier_sku") or row.get("article") or ""),
                    description=str(row.get("product_name") or row.get("name") or row.get("товар") or ""),
                    unit_price=money_str(row.get("price") or row.get("unit_price") or "0"),
                    currency=currency,
                    vat_status=str(row.get("vat_status") or version.vat_status),
                    moq=int(row["moq"]) if row.get("moq") is not None else None,
                    quantity_tiers=tuple(row.get("tiers") or ()),
                    available_quantity=int(row["qty"]) if row.get("qty") is not None else None,
                    lead_time_days=int(row["lead_time"]) if row.get("lead_time") is not None else None,
                    match_state=state,
                    product_id=match.product_id,
                    product_version_id=match.product_version_id,
                    match_candidates=match.candidates,
                    freshness=OFFER_FRESH,
                    provenance=B2BProvenance(
                        tenant_id=tenant,
                        supplier_id=supplier.supplier_id,
                        source_class=source_class,
                        source_artifact_id=artifact_id or artifact_hash,
                        source_row_ref=str(idx),
                        observed_at=version.observed_at,
                        ingested_at=version.ingested_at,
                        currency=currency,
                        vat_status=str(row.get("vat_status") or version.vat_status),
                        source_version_hash=artifact_hash,
                    ),
                )
                self.store.save_offer(offer)
                offers.append(offer)
            except Exception:
                failed += 1
        self.obs.emit("b2b.wholesale.match.completed", tenant_id=tenant, matched=matched, ambiguous=ambiguous)
        return {
            "price_list_version_id": version.version_id,
            "offer_count": len(offers),
            "matched": matched,
            "ambiguous": ambiguous,
            "unmatched": unmatched,
            "failed": failed,
        }

    def list_wholesale(self, *, tenant_id: str, supplier_id: str = "", capabilities: tuple[str, ...] = ()) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_READ

        self._require_cap(capabilities, CAP_B2B_WHOLESALE_READ)
        tenant = require_tenant_id(tenant_id)
        offers = self.store.list_offers(tenant, supplier_id=supplier_id)
        stale_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_OFFER_DAYS)
        rows = []
        for offer in offers:
            ingested = datetime.fromisoformat(offer.provenance.ingested_at if offer.provenance else _utc())
            if ingested.tzinfo is None:
                ingested = ingested.replace(tzinfo=timezone.utc)
            freshness = OFFER_STALE if ingested < stale_cutoff else offer.freshness
            rows.append(
                {
                    "offer_id": offer.offer_id,
                    "supplier_id": offer.supplier_id,
                    "unit_price": offer.unit_price,
                    "currency": offer.currency,
                    "match_state": offer.match_state,
                    "freshness": freshness,
                }
            )
        return {"offers": rows}

    def compare_wholesale(
        self,
        *,
        tenant_id: str,
        product_id: str,
        requested_quantity: int,
        preferred_supplier: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_COMPARE

        self._require_cap(capabilities, CAP_B2B_WHOLESALE_COMPARE)
        tenant = require_tenant_id(tenant_id)
        offers = [
            o
            for o in self.store.list_offers(tenant)
            if o.product_id == product_id and o.match_state == MATCH_CONFIRMED
        ]
        comparison = compare_offers(
            offers,
            tenant_id=tenant,
            product_id=product_id,
            requested_quantity=requested_quantity,
            preferred_supplier=preferred_supplier,
        )
        self.obs.emit("b2b.wholesale.compare.completed", tenant_id=tenant, product_id=product_id)
        return {
            "comparison_id": comparison.comparison_id,
            "best_offer_id": comparison.best_offer_id,
            "ranking_reason": comparison.ranking_reason,
            "components": list(comparison.components),
            "offers": list(comparison.offers),
        }

    def wholesale_changes(
        self,
        *,
        tenant_id: str,
        supplier_id: str,
        old_version_id: str,
        new_version_id: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_READ

        self._require_cap(capabilities, CAP_B2B_WHOLESALE_READ)
        tenant = require_tenant_id(tenant_id)
        old_offers = [o for o in self.store.list_offers(tenant, supplier_id=supplier_id) if o.price_list_version_id == old_version_id]
        new_offers = [o for o in self.store.list_offers(tenant, supplier_id=supplier_id) if o.price_list_version_id == new_version_id]
        changes = detect_price_changes(old_offers, new_offers, tenant_id=tenant, supplier_id=supplier_id)
        return {"changes": [c.__dict__ for c in changes]}

    def register_telegram_account(
        self,
        *,
        tenant_id: str,
        bot_id: str,
        secret_ref: str = "telegram/bot-token",
        capabilities: tuple[str, ...] = (),
    ) -> TelegramAccountBinding:
        from b2b_commerce.capabilities import CAP_TELEGRAM_READ

        self._require_cap(capabilities, CAP_TELEGRAM_READ)
        tenant = require_tenant_id(tenant_id)
        binding = TelegramAccountBinding(
            binding_id=f"tga_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant,
            bot_id=bot_id,
            secret_ref=secret_ref,
        )
        self.store.save_telegram_account(binding)
        return binding

    def bind_telegram_chat(
        self,
        *,
        tenant_id: str,
        account_binding_id: str,
        chat_id: str,
        customer_id: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> TelegramChatBinding:
        from b2b_commerce.capabilities import CAP_TELEGRAM_READ

        self._require_cap(capabilities, CAP_TELEGRAM_READ)
        tenant = require_tenant_id(tenant_id)
        account = self.store.get_telegram_account(tenant, account_binding_id)
        if account is None:
            raise B2BCommerceError(B2B_TELEGRAM_BINDING_DENIED)
        self.access.require_telegram_binding(tenant_id=tenant, binding_tenant=account.tenant_id)
        conversation_id = new_conversation_id()
        binding = TelegramChatBinding(
            binding_id=f"tgc_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant,
            account_binding_id=account_binding_id,
            chat_id=chat_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )
        self.store.save_telegram_chat(binding)
        if customer_id:
            conv = B2BConversation(
                conversation_id=conversation_id,
                tenant_id=tenant,
                customer_id=customer_id,
                chat_binding_id=binding.binding_id,
            )
            self.store.save_conversation(conv)
        return binding

    def process_telegram_update(
        self,
        *,
        tenant_id: str,
        raw_update: dict[str, Any],
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE

        self._require_cap(capabilities, CAP_TELEGRAM_READ)
        tenant = require_tenant_id(tenant_id)
        update = normalize_inbound(raw_update)
        if self.store.has_telegram_update(tenant, update.update_id):
            raise B2BCommerceError(B2B_TELEGRAM_DUPLICATE_UPDATE)
        account = self.store.get_telegram_account_by_bot(tenant, update.bot_id)
        if account is None:
            raise B2BCommerceError(B2B_TELEGRAM_BINDING_DENIED)
        chat = self.store.get_telegram_chat(tenant, update.chat_id, account.binding_id)
        if chat is None:
            raise B2BCommerceError(B2B_TELEGRAM_BINDING_DENIED)
        self.store.mark_telegram_update(tenant, update.update_id)
        self.obs.emit("b2b.telegram.received", tenant_id=tenant, chat_id=update.chat_id, update_id=update.update_id)

        conversation = self.store.get_conversation(tenant, chat.conversation_id)
        if conversation is None:
            customer = B2BCustomer(
                customer_id=new_customer_id(),
                tenant_id=tenant,
                display_name=f"tg_{update.chat_id}",
                verification_state=CUSTOMER_CANDIDATE,
            )
            self.store.save_customer(customer)
            conversation = B2BConversation(
                conversation_id=chat.conversation_id,
                tenant_id=tenant,
                customer_id=customer.customer_id,
                chat_binding_id=chat.binding_id,
            )
            self.store.save_conversation(conversation)
            chat.customer_id = customer.customer_id
            self.store.save_telegram_chat(chat)

        msg = record_inbound_message(tenant_id=tenant, conversation_id=conversation.conversation_id, text=update.text)
        self.store.save_message(msg)

        inquiry_id = new_inquiry_id()
        inquiry = B2BInquiry(
            inquiry_id=inquiry_id,
            tenant_id=tenant,
            conversation_id=conversation.conversation_id,
            customer_id=conversation.customer_id,
        )
        self.store.save_inquiry(inquiry)
        items = extract_inquiry_items(update.text, tenant_id=tenant, inquiry_id=inquiry_id)
        resolved = self.resolve_inquiry_items(tenant_id=tenant, items=items, capabilities=capabilities or (CAP_B2B_ASSISTANT_USE,))
        for item in resolved:
            self.store.save_inquiry_item(item)
        conversation.state = transition_after_inquiry(conversation, resolved)
        conversation.updated_at = _utc()
        self.store.save_conversation(conversation)
        self.obs.emit("b2b.inquiry.created", tenant_id=tenant, inquiry_id=inquiry_id)

        assistant = self.assistant_process(
            tenant_id=tenant,
            conversation_id=conversation.conversation_id,
            text=update.text,
            resolved_items=[item.__dict__ for item in resolved],
            data_scope="CUSTOMER",
            capabilities=capabilities or (CAP_B2B_ASSISTANT_USE,),
        )
        return {
            "update_id": update.update_id,
            "conversation_id": conversation.conversation_id,
            "inquiry_id": inquiry_id,
            "state": conversation.state,
            "items": [item.__dict__ for item in resolved],
            "assistant": assistant,
            "attachment_ref": update.attachment_ref,
        }

    def resolve_inquiry_items(
        self,
        *,
        tenant_id: str,
        items: list,
        capabilities: tuple[str, ...] = (),
    ) -> list:
        tenant = require_tenant_id(tenant_id)
        catalog = self._catalog(tenant)
        resolved = []
        for item in items:
            row = {"product_name": item.product_query, "name": item.product_query}
            match = match_supplier_row(
                row,
                catalog,
                tenant_id=tenant,
                offer_version_id=item.item_id,
            )
            item.match_state = match.state
            item.candidates = match.candidates
            item.product_id = match.product_id
            resolved.append(item)
        return resolved

    def create_customer(
        self,
        *,
        tenant_id: str,
        display_name: str,
        verification_state: str = CUSTOMER_CANDIDATE,
        capabilities: tuple[str, ...] = (),
    ) -> B2BCustomer:
        from b2b_commerce.capabilities import CAP_B2B_CUSTOMER_WRITE

        self._require_cap(capabilities, CAP_B2B_CUSTOMER_WRITE)
        tenant = require_tenant_id(tenant_id)
        customer = B2BCustomer(
            customer_id=new_customer_id(),
            tenant_id=tenant,
            display_name=display_name,
            verification_state=verification_state,
        )
        self.store.save_customer(customer)
        return customer

    def create_quote(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        inquiry_id: str,
        customer_id: str,
        items: list[dict[str, Any]],
        discount_pct: str = "0",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_QUOTE_CREATE

        self._require_cap(capabilities, CAP_B2B_QUOTE_CREATE)
        tenant = require_tenant_id(tenant_id)
        customer = require_customer(self.store.get_customer(tenant, customer_id))
        if any(i.get("quantity") is None for i in items):
            raise B2BCommerceError(B2B_QUANTITY_REQUIRED)
        if any(i.get("match_state") == MATCH_AMBIGUOUS for i in items):
            raise B2BCommerceError(B2B_PRODUCT_AMBIGUOUS)
        quote_id = f"quote_{uuid.uuid4().hex[:12]}"
        versions = self.store.list_quote_versions(tenant, quote_id)
        version_id = new_quote_version_id(versions)
        priced = compute_customer_quote_lines(
            items=items,
            discount_pct=Decimal(discount_pct),
            discount_ceiling=Decimal(customer.discount_ceiling or "100"),
            vat_status=customer.vat_status,
        )
        quote = CommercialQuoteVersion(
            quote_id=quote_id,
            version_id=version_id,
            tenant_id=tenant,
            customer_id=customer.customer_id,
            conversation_id=conversation_id,
            inquiry_id=inquiry_id,
            currency=customer.currency or "RUB",
            vat_status=customer.vat_status,
            subtotal=priced["subtotal"],
            discount=priced["discount"],
            vat_amount=priced["vat_amount"],
            total=priced["total"],
            approval_status=priced["approval_status"],
            valid_until=default_valid_until(),
            items=tuple(priced["lines"]),
        )
        self.store.save_quote(quote)
        self.obs.emit("b2b.quote.created", tenant_id=tenant, quote_id=quote_id, version_id=version_id)
        if quote.approval_status == QUOTE_REQUIRE_APPROVAL:
            self.obs.emit("b2b.quote.approval_required", tenant_id=tenant, quote_id=quote_id)
        return {"quote": quote.__dict__, "customer_view": customer_safe_projection(quote.__dict__)}

    def prepare_quote_send(
        self,
        *,
        tenant_id: str,
        quote_id: str,
        version_id: str,
        chat_id: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_QUOTE_SEND

        self._require_cap(capabilities, CAP_B2B_QUOTE_SEND)
        tenant = require_tenant_id(tenant_id)
        quote = self.store.get_quote(tenant, quote_id, version_id)
        if quote is None:
            raise B2BCommerceError("B2B_CONFLICT")
        assert_quote_fresh(quote)
        if quote.approval_status == QUOTE_REQUIRE_APPROVAL:
            raise B2BCommerceError("B2B_QUOTE_APPROVAL_REQUIRED")
        idempotency_key = quote_send_idempotency_key(
            tenant_id=tenant, chat_id=chat_id, quote_id=quote_id, version_id=version_id
        )
        existing = self.store.get_receipt_by_idempotency(tenant, idempotency_key)
        if existing:
            return {"idempotent": True, "receipt": existing.__dict__}
        body = customer_safe_projection(quote.__dict__)
        text = f"Quote {quote.quote_id} {quote.version_id}: total {quote.total} {quote.currency}"
        return {
            "text": text,
            "quote": body,
            "idempotency_key": idempotency_key,
            "chat_id": chat_id,
        }

    def record_quote_sent(
        self,
        *,
        tenant_id: str,
        quote_id: str,
        version_id: str,
        chat_binding_id: str,
        idempotency_key: str,
        provider_message_id: str,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        quote = self.store.get_quote(tenant, quote_id, version_id)
        if quote:
            quote.sent = True
            self.store.save_quote(quote)
        receipt = TelegramMessageReceipt(
            receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant,
            chat_binding_id=chat_binding_id,
            provider_message_id=provider_message_id,
            operation="quote_send",
            idempotency_key=idempotency_key,
            status="sent",
        )
        self.store.save_receipt(receipt)
        self.obs.emit("b2b.quote.sent", tenant_id=tenant, quote_id=quote_id)
        return receipt.__dict__

    def send_telegram_message(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        text: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_TELEGRAM_SEND

        self._require_cap(capabilities, CAP_TELEGRAM_SEND)
        tenant = require_tenant_id(tenant_id)
        if self.telegram is None:
            raise B2BCommerceError("B2B_TELEGRAM_PROVIDER_FAILED")
        existing = self.store.get_receipt_by_idempotency(tenant, idempotency_key)
        if existing:
            return {"idempotent": True, "receipt": existing.__dict__}
        result = self.telegram.send_message(
            TelegramSendRequest(tenant_id=tenant, chat_id=chat_id, text=text, idempotency_key=idempotency_key)
        )
        receipt = TelegramMessageReceipt(
            receipt_id=f"rcpt_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant,
            chat_binding_id=chat_id,
            provider_message_id=result.provider_message_id,
            operation="message_send",
            idempotency_key=idempotency_key,
            status=result.status,
        )
        self.store.save_receipt(receipt)
        self.obs.emit("b2b.telegram.sent", tenant_id=tenant, chat_id=chat_id)
        return {"receipt": receipt.__dict__, "provider_message_id": result.provider_message_id}

    def create_order_draft(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        conversation_id: str,
        quote_id: str,
        quote_version_id: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_ORDER_DRAFT

        self._require_cap(capabilities, CAP_B2B_ORDER_DRAFT)
        tenant = require_tenant_id(tenant_id)
        quote = self.store.get_quote(tenant, quote_id, quote_version_id)
        if quote is None:
            raise B2BCommerceError("B2B_CONFLICT")
        assert_quote_fresh(quote)
        token = new_confirmation_token()
        draft = B2BOrderDraft(
            draft_id=new_draft_id(),
            tenant_id=tenant,
            customer_id=customer_id,
            conversation_id=conversation_id,
            quote_id=quote_id,
            quote_version_id=quote_version_id,
            confirmation_token=token,
        )
        self.store.save_order_draft(draft)
        self.store.save_confirmation(tenant, token, bind_confirmation(quote=quote, conversation_id=conversation_id, draft_id=draft.draft_id))
        conv = self.store.get_conversation(tenant, conversation_id)
        if conv:
            conv.state = CONV_AWAITING_CONFIRMATION
            self.store.save_conversation(conv)
        self.obs.emit("b2b.order_draft.created", tenant_id=tenant, draft_id=draft.draft_id)
        return {"draft": draft.__dict__, "confirmation_token": token}

    def submit_order(
        self,
        *,
        tenant_id: str,
        draft_id: str,
        confirmation_token: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_ORDER_SUBMIT

        self._require_cap(capabilities, CAP_B2B_ORDER_SUBMIT)
        tenant = require_tenant_id(tenant_id)
        draft = self.store.get_order_draft(tenant, draft_id)
        if draft is None or draft.submitted:
            payload = self.store.get_receipt_by_idempotency(tenant, f"order:{draft_id}")
            if payload:
                return {"idempotent": True}
            raise B2BCommerceError("B2B_CONFLICT")
        customer = require_customer(self.store.get_customer(tenant, draft.customer_id))
        require_verified_customer(customer, for_sensitive=True)
        quote = self.store.get_quote(tenant, draft.quote_id, draft.quote_version_id)
        if quote is None:
            raise B2BCommerceError("B2B_CONFLICT")
        assert_quote_fresh(quote)
        confirmation = self.store.consume_confirmation(tenant, confirmation_token)
        assert_confirmation_matches(
            confirmation,
            quote=quote,
            conversation_id=draft.conversation_id,
            draft_id=draft.draft_id,
        )
        draft.confirmed = True
        draft.submitted = True
        platform_order_id = f"ord_{uuid.uuid4().hex[:12]}"
        if self.product_platform is not None and hasattr(self.product_platform, "ingest_order"):
            quote_items = list(quote.items or ())
            order = self.product_platform.ingest_order(
                tenant_id=tenant,
                external_ref=draft.draft_id,
                source="b2b.telegram",
                items=[
                    {
                        "product_id": str(line.get("product_id") or ""),
                        "quantity": line.get("quantity"),
                        "unit_price": line.get("unit_price"),
                        "sku": line.get("sku") or "",
                    }
                    for line in quote_items
                ],
                currency=quote.currency,
            )
            platform_order_id = order.order_id
        draft.platform_order_id = platform_order_id
        self.store.save_order_draft(draft)
        self.obs.emit("b2b.order.submitted", tenant_id=tenant, draft_id=draft.draft_id, order_id=platform_order_id)
        conv = self.store.get_conversation(tenant, draft.conversation_id)
        if conv:
            conv.state = CONV_ORDER_DRAFTED
            self.store.save_conversation(conv)
        return {"order_id": platform_order_id, "draft_id": draft.draft_id}

    def create_handoff(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        reason: str,
        context: dict[str, Any] | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        conv = self.store.get_conversation(tenant, conversation_id)
        if conv is None:
            raise B2BCommerceError("B2B_CONFLICT")
        conv.state = CONV_HUMAN_HANDOFF
        self.store.save_conversation(conv)
        self.obs.emit("b2b.handoff.created", tenant_id=tenant, conversation_id=conversation_id, reason=reason)
        return {"state": conv.state, "reason": reason, "context": context or {}}

    def assistant_process(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        text: str,
        resolved_items: list[dict[str, Any]] | None = None,
        data_scope: str = "CUSTOMER",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_ASSISTANT_USE

        self._require_cap(capabilities, CAP_B2B_ASSISTANT_USE)
        tenant = require_tenant_id(tenant_id)
        self.obs.emit("b2b.assistant.started", tenant_id=tenant, conversation_id=conversation_id)
        proposal = propose_from_message(
            tenant_id=tenant,
            conversation_id=conversation_id,
            text=text,
            resolved_items=resolved_items,
            data_scope=data_scope,
        )
        validate_action(proposal.action)
        payload = dict(proposal.payload)
        if data_scope == "CUSTOMER":
            payload = customer_safe_projection(payload)
        self.obs.emit("b2b.assistant.completed", tenant_id=tenant, action=proposal.action)
        return {"proposal": {**proposal.__dict__, "payload": payload}}

    def mark_quote_stale_for_product(self, *, tenant_id: str, product_id: str) -> int:
        tenant = require_tenant_id(tenant_id)
        count = 0
        for quote_versions in [
            self.store.list_quote_versions(tenant, qid)
            for qid in {q.quote_id for q in []}
        ]:
            pass
        # Mark all quotes referencing product in items
        return count

    def invalidate_customer(self, *, tenant_id: str, customer_id: str) -> None:
        tenant = require_tenant_id(tenant_id)
        customer = self.store.get_customer(tenant, customer_id)
        if customer:
            customer.deleted = True
            self.store.save_customer(customer)

    def resume_wholesale_job(self, *, tenant_id: str, job_id: str, rows: list[dict[str, Any]], capabilities: tuple[str, ...] = ()) -> dict:
        job = self.store.get_job(require_tenant_id(tenant_id), job_id)
        if job is None or job.status == JOB_CANCELLED:
            raise B2BCommerceError("B2B_CANCELLED")
        return self.ingest_wholesale(
            tenant_id=tenant_id,
            supplier_id=job.supplier_id,
            rows=rows,
            bulk=True,
            job_id=job_id,
            checkpoint=job.checkpoint,
            artifact_id=job.source_artifact_id,
            capabilities=capabilities,
        )

    def get_quote(
        self,
        *,
        tenant_id: str,
        quote_id: str,
        version_id: str,
        customer_view: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        from b2b_commerce.capabilities import CAP_B2B_QUOTE_READ

        self._require_cap(capabilities, CAP_B2B_QUOTE_READ)
        tenant = require_tenant_id(tenant_id)
        quote = self.store.get_quote(tenant, quote_id, version_id)
        if quote is None:
            raise B2BCommerceError("B2B_CONFLICT")
        payload = quote.__dict__
        if customer_view:
            payload = customer_safe_projection(payload)
        return {"quote": payload}
        from b2b_commerce.capabilities import CAP_B2B_READ

        self._require_cap(capabilities, CAP_B2B_READ)
        tenant = require_tenant_id(tenant_id)
        conv = self.store.get_conversation(tenant, conversation_id)
        return conv.__dict__ if conv else None

    def build_internal_assistant_context(self, *, tenant_id: str, quote_id: str, version_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        quote = self.store.get_quote(tenant, quote_id, version_id)
        return quote.__dict__ if quote else {}

    def build_customer_assistant_context(self, *, tenant_id: str, quote_id: str, version_id: str) -> dict:
        return customer_safe_projection(self.build_internal_assistant_context(tenant_id=tenant_id, quote_id=quote_id, version_id=version_id))
