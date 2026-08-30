"""SQLite B2B store — tenant partitioned."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict

from b2b_commerce.platform_models import (
    B2BConversation,
    B2BConversationMessage,
    B2BCustomer,
    B2BInquiry,
    B2BInquiryItem,
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
    WholesaleProductMatch,
)
from security.tenant import require_tenant_id


def _j(value) -> str:
    return json.dumps(value, default=str)


def _from_provenance(data: dict | None) -> B2BProvenance | None:
    if not data:
        return None
    return B2BProvenance(**data)


class SqliteB2BStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    def _conn(self):
        return self._connection

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS b2b_suppliers(
                    tenant_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, supplier_id));
                CREATE TABLE IF NOT EXISTS b2b_price_lists(
                    tenant_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    supplier_id TEXT NOT NULL, artifact_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, version_id));
                CREATE TABLE IF NOT EXISTS b2b_offers(
                    tenant_id TEXT NOT NULL, offer_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, offer_id, version_id));
                CREATE TABLE IF NOT EXISTS b2b_matches(
                    tenant_id TEXT NOT NULL, match_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, match_id));
                CREATE TABLE IF NOT EXISTS b2b_customers(
                    tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, customer_id));
                CREATE TABLE IF NOT EXISTS b2b_tg_accounts(
                    tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, binding_id));
                CREATE TABLE IF NOT EXISTS b2b_tg_chats(
                    tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, account_binding_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, binding_id));
                CREATE TABLE IF NOT EXISTS b2b_conversations(
                    tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, conversation_id));
                CREATE TABLE IF NOT EXISTS b2b_messages(
                    tenant_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, message_id));
                CREATE TABLE IF NOT EXISTS b2b_inquiries(
                    tenant_id TEXT NOT NULL, inquiry_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, inquiry_id));
                CREATE TABLE IF NOT EXISTS b2b_inquiry_items(
                    tenant_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    inquiry_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, item_id));
                CREATE TABLE IF NOT EXISTS b2b_quotes(
                    tenant_id TEXT NOT NULL, quote_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, quote_id, version_id));
                CREATE TABLE IF NOT EXISTS b2b_order_drafts(
                    tenant_id TEXT NOT NULL, draft_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, draft_id));
                CREATE TABLE IF NOT EXISTS b2b_jobs(
                    tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, job_id));
                CREATE TABLE IF NOT EXISTS b2b_receipts(
                    tenant_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, receipt_id));
                CREATE TABLE IF NOT EXISTS b2b_tg_updates(
                    tenant_id TEXT NOT NULL, update_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, update_id));
                CREATE TABLE IF NOT EXISTS b2b_confirmations(
                    tenant_id TEXT NOT NULL, token TEXT NOT NULL,
                    payload_json TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, token));
                """
            )
            conn.commit()

    def save_supplier(self, supplier: Supplier) -> None:
        tenant = require_tenant_id(supplier.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_suppliers VALUES (?,?,?)",
                (tenant, supplier.supplier_id, _j(asdict(supplier))),
            )
            self._conn().commit()

    def get_supplier(self, tenant_id: str, supplier_id: str) -> Supplier | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_suppliers WHERE tenant_id=? AND supplier_id=?",
            (tenant, supplier_id),
        ).fetchone()
        return Supplier(**json.loads(row["payload_json"])) if row else None

    def list_suppliers(self, tenant_id: str) -> list[Supplier]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM b2b_suppliers WHERE tenant_id=?", (tenant,)
        ).fetchall()
        return [Supplier(**json.loads(r["payload_json"])) for r in rows]

    def save_price_list_version(self, version: SupplierPriceListVersion) -> None:
        tenant = require_tenant_id(version.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_price_lists VALUES (?,?,?,?,?)",
                (tenant, version.version_id, version.supplier_id, version.artifact_hash, _j(asdict(version))),
            )
            self._conn().commit()

    def get_price_list_version(self, tenant_id: str, version_id: str) -> SupplierPriceListVersion | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_price_lists WHERE tenant_id=? AND version_id=?",
            (tenant, version_id),
        ).fetchone()
        return SupplierPriceListVersion(**json.loads(row["payload_json"])) if row else None

    def find_price_list_by_hash(self, tenant_id: str, supplier_id: str, artifact_hash: str) -> SupplierPriceListVersion | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_price_lists WHERE tenant_id=? AND supplier_id=? AND artifact_hash=?",
            (tenant, supplier_id, artifact_hash),
        ).fetchone()
        return SupplierPriceListVersion(**json.loads(row["payload_json"])) if row else None

    def save_offer(self, offer: WholesaleOfferVersion) -> None:
        tenant = require_tenant_id(offer.tenant_id)
        payload = asdict(offer)
        if offer.provenance:
            payload["provenance"] = asdict(offer.provenance)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_offers VALUES (?,?,?,?)",
                (tenant, offer.offer_id, offer.version_id, _j(payload)),
            )
            self._conn().commit()

    def _load_offer(self, payload: dict) -> WholesaleOfferVersion:
        prov = payload.pop("provenance", None)
        offer = WholesaleOfferVersion(**payload)
        offer.provenance = _from_provenance(prov)
        return offer

    def get_offer(self, tenant_id: str, offer_id: str, version_id: str) -> WholesaleOfferVersion | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_offers WHERE tenant_id=? AND offer_id=? AND version_id=?",
            (tenant, offer_id, version_id),
        ).fetchone()
        return self._load_offer(json.loads(row["payload_json"])) if row else None

    def list_offers(self, tenant_id: str, *, supplier_id: str = "") -> list[WholesaleOfferVersion]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute("SELECT payload_json FROM b2b_offers WHERE tenant_id=?", (tenant,)).fetchall()
        offers = [self._load_offer(json.loads(r["payload_json"])) for r in rows]
        if supplier_id:
            offers = [o for o in offers if o.supplier_id == supplier_id]
        return offers

    def list_offer_versions(self, tenant_id: str, offer_id: str) -> list[WholesaleOfferVersion]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM b2b_offers WHERE tenant_id=? AND offer_id=?",
            (tenant, offer_id),
        ).fetchall()
        return [self._load_offer(json.loads(r["payload_json"])) for r in rows]

    def save_match(self, match: WholesaleProductMatch) -> None:
        tenant = require_tenant_id(match.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_matches VALUES (?,?,?)",
                (tenant, match.match_id, _j(asdict(match))),
            )
            self._conn().commit()

    def get_match(self, tenant_id: str, match_id: str) -> WholesaleProductMatch | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_matches WHERE tenant_id=? AND match_id=?",
            (tenant, match_id),
        ).fetchone()
        return WholesaleProductMatch(**json.loads(row["payload_json"])) if row else None

    def save_customer(self, customer: B2BCustomer) -> None:
        tenant = require_tenant_id(customer.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_customers VALUES (?,?,?)",
                (tenant, customer.customer_id, _j(asdict(customer))),
            )
            self._conn().commit()

    def get_customer(self, tenant_id: str, customer_id: str) -> B2BCustomer | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_customers WHERE tenant_id=? AND customer_id=?",
            (tenant, customer_id),
        ).fetchone()
        return B2BCustomer(**json.loads(row["payload_json"])) if row else None

    def save_telegram_account(self, binding: TelegramAccountBinding) -> None:
        tenant = require_tenant_id(binding.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_tg_accounts VALUES (?,?,?,?)",
                (tenant, binding.binding_id, binding.bot_id, _j(asdict(binding))),
            )
            self._conn().commit()

    def get_telegram_account_by_bot(self, tenant_id: str, bot_id: str) -> TelegramAccountBinding | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_tg_accounts WHERE tenant_id=? AND bot_id=?",
            (tenant, bot_id),
        ).fetchone()
        return TelegramAccountBinding(**json.loads(row["payload_json"])) if row else None

    def get_telegram_account(self, tenant_id: str, binding_id: str) -> TelegramAccountBinding | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_tg_accounts WHERE tenant_id=? AND binding_id=?",
            (tenant, binding_id),
        ).fetchone()
        return TelegramAccountBinding(**json.loads(row["payload_json"])) if row else None

    def save_telegram_chat(self, binding: TelegramChatBinding) -> None:
        tenant = require_tenant_id(binding.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_tg_chats VALUES (?,?,?,?,?)",
                (tenant, binding.binding_id, binding.chat_id, binding.account_binding_id, _j(asdict(binding))),
            )
            self._conn().commit()

    def get_telegram_chat(self, tenant_id: str, chat_id: str, account_binding_id: str) -> TelegramChatBinding | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_tg_chats WHERE tenant_id=? AND chat_id=? AND account_binding_id=?",
            (tenant, chat_id, account_binding_id),
        ).fetchone()
        return TelegramChatBinding(**json.loads(row["payload_json"])) if row else None

    def save_conversation(self, conversation: B2BConversation) -> None:
        tenant = require_tenant_id(conversation.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_conversations VALUES (?,?,?)",
                (tenant, conversation.conversation_id, _j(asdict(conversation))),
            )
            self._conn().commit()

    def get_conversation(self, tenant_id: str, conversation_id: str) -> B2BConversation | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_conversations WHERE tenant_id=? AND conversation_id=?",
            (tenant, conversation_id),
        ).fetchone()
        return B2BConversation(**json.loads(row["payload_json"])) if row else None

    def save_message(self, message: B2BConversationMessage) -> None:
        tenant = require_tenant_id(message.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_messages VALUES (?,?,?,?)",
                (tenant, message.message_id, message.conversation_id, _j(asdict(message))),
            )
            self._conn().commit()

    def list_messages(self, tenant_id: str, conversation_id: str) -> list[B2BConversationMessage]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM b2b_messages WHERE tenant_id=? AND conversation_id=?",
            (tenant, conversation_id),
        ).fetchall()
        return [B2BConversationMessage(**json.loads(r["payload_json"])) for r in rows]

    def save_inquiry(self, inquiry: B2BInquiry) -> None:
        tenant = require_tenant_id(inquiry.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_inquiries VALUES (?,?,?)",
                (tenant, inquiry.inquiry_id, _j(asdict(inquiry))),
            )
            self._conn().commit()

    def get_inquiry(self, tenant_id: str, inquiry_id: str) -> B2BInquiry | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_inquiries WHERE tenant_id=? AND inquiry_id=?",
            (tenant, inquiry_id),
        ).fetchone()
        return B2BInquiry(**json.loads(row["payload_json"])) if row else None

    def save_inquiry_item(self, item: B2BInquiryItem) -> None:
        tenant = require_tenant_id(item.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_inquiry_items VALUES (?,?,?,?)",
                (tenant, item.item_id, item.inquiry_id, _j(asdict(item))),
            )
            self._conn().commit()

    def list_inquiry_items(self, tenant_id: str, inquiry_id: str) -> list[B2BInquiryItem]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM b2b_inquiry_items WHERE tenant_id=? AND inquiry_id=?",
            (tenant, inquiry_id),
        ).fetchall()
        return [B2BInquiryItem(**json.loads(r["payload_json"])) for r in rows]

    def save_quote(self, quote: CommercialQuoteVersion) -> None:
        tenant = require_tenant_id(quote.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_quotes VALUES (?,?,?,?)",
                (tenant, quote.quote_id, quote.version_id, _j(asdict(quote))),
            )
            self._conn().commit()

    def get_quote(self, tenant_id: str, quote_id: str, version_id: str) -> CommercialQuoteVersion | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_quotes WHERE tenant_id=? AND quote_id=? AND version_id=?",
            (tenant, quote_id, version_id),
        ).fetchone()
        return CommercialQuoteVersion(**json.loads(row["payload_json"])) if row else None

    def list_quote_versions(self, tenant_id: str, quote_id: str) -> list[CommercialQuoteVersion]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM b2b_quotes WHERE tenant_id=? AND quote_id=?",
            (tenant, quote_id),
        ).fetchall()
        return [CommercialQuoteVersion(**json.loads(r["payload_json"])) for r in rows]

    def save_order_draft(self, draft: B2BOrderDraft) -> None:
        tenant = require_tenant_id(draft.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_order_drafts VALUES (?,?,?)",
                (tenant, draft.draft_id, _j(asdict(draft))),
            )
            self._conn().commit()

    def get_order_draft(self, tenant_id: str, draft_id: str) -> B2BOrderDraft | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_order_drafts WHERE tenant_id=? AND draft_id=?",
            (tenant, draft_id),
        ).fetchone()
        return B2BOrderDraft(**json.loads(row["payload_json"])) if row else None

    def save_job(self, job: B2BJob) -> None:
        tenant = require_tenant_id(job.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_jobs VALUES (?,?,?)",
                (tenant, job.job_id, _j(asdict(job))),
            )
            self._conn().commit()

    def get_job(self, tenant_id: str, job_id: str) -> B2BJob | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_jobs WHERE tenant_id=? AND job_id=?",
            (tenant, job_id),
        ).fetchone()
        return B2BJob(**json.loads(row["payload_json"])) if row else None

    def save_receipt(self, receipt: TelegramMessageReceipt) -> None:
        tenant = require_tenant_id(receipt.tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_receipts VALUES (?,?,?,?)",
                (tenant, receipt.receipt_id, receipt.idempotency_key, _j(asdict(receipt))),
            )
            self._conn().commit()

    def get_receipt_by_idempotency(self, tenant_id: str, idempotency_key: str) -> TelegramMessageReceipt | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM b2b_receipts WHERE tenant_id=? AND idempotency_key=?",
            (tenant, idempotency_key),
        ).fetchone()
        return TelegramMessageReceipt(**json.loads(row["payload_json"])) if row else None

    def has_telegram_update(self, tenant_id: str, update_id: str) -> bool:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT 1 FROM b2b_tg_updates WHERE tenant_id=? AND update_id=?",
            (tenant, update_id),
        ).fetchone()
        return row is not None

    def mark_telegram_update(self, tenant_id: str, update_id: str) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR IGNORE INTO b2b_tg_updates VALUES (?,?)",
                (tenant, update_id),
            )
            self._conn().commit()

    def save_confirmation(self, tenant_id: str, token: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO b2b_confirmations VALUES (?,?,?,0)",
                (tenant, token, _j(payload)),
            )
            self._conn().commit()

    def get_confirmation(self, tenant_id: str, token: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json, consumed FROM b2b_confirmations WHERE tenant_id=? AND token=?",
            (tenant, token),
        ).fetchone()
        if not row or row["consumed"]:
            return None
        return json.loads(row["payload_json"])

    def consume_confirmation(self, tenant_id: str, token: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        payload = self.get_confirmation(tenant, token)
        if not payload:
            return None
        with self._lock:
            self._conn().execute(
                "UPDATE b2b_confirmations SET consumed=1 WHERE tenant_id=? AND token=?",
                (tenant, token),
            )
            self._conn().commit()
        return payload
