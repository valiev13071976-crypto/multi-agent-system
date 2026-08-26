"""Supplier discovery — knowledge/document first; no crawler; no arbitrary URLs."""

from __future__ import annotations

import uuid

from memory.models import utc_now
from procurement.models import (
    SUPPLIER_CANDIDATE,
    SUPPLIER_KNOWN,
    SUPPLIER_RESTRICTED,
    TRUST_DOCUMENT,
    TRUST_KNOWN,
    TRUST_UNVERIFIED,
    Supplier,
    content_hash_text,
)


class SupplierDiscoveryService:
    def __init__(
        self,
        *,
        supplier_repo,
        knowledge_service=None,
        document_service=None,
        excluded_names: tuple[str, ...] = (),
    ):
        self.supplier_repo = supplier_repo
        self.knowledge_service = knowledge_service
        self.document_service = document_service
        self.excluded_names = {n.lower() for n in excluded_names}

    def discover(
        self,
        *,
        scope,
        requirement,
        seed_suppliers: tuple[Supplier, ...] = (),
        excluded_supplier_ids: tuple[str, ...] = (),
    ) -> tuple[Supplier, ...]:
        found: dict[str, Supplier] = {}
        excluded = set(excluded_supplier_ids)

        for s in seed_suppliers:
            if s.supplier_id in excluded or s.name.lower() in self.excluded_names:
                continue
            if s.scope.key() != scope.key():
                continue
            found[s.supplier_id] = s
            self.supplier_repo.upsert(s)

        for existing in self.supplier_repo.list_for_scope(scope):
            if existing.supplier_id in excluded:
                continue
            if existing.name.lower() in self.excluded_names:
                continue
            found[existing.supplier_id] = existing

        # Knowledge-first evidence (same scope)
        if self.knowledge_service is not None:
            try:
                from knowledge.models import KnowledgeQuery

                hits = self.knowledge_service.retrieve(
                    KnowledgeQuery(
                        query_text=f"supplier {requirement.normalized_item}",
                        scope=scope,
                        limit=10,
                    ),
                    requesting_scope=scope,
                )
                for hit in hits:
                    name = self._extract_supplier_name(hit.content)
                    if not name or name.lower() in self.excluded_names:
                        continue
                    sid = f"know-{content_hash_text(name)[:12]}"
                    if sid in excluded:
                        continue
                    status = SUPPLIER_KNOWN
                    trust = TRUST_KNOWN
                    if "restrict" in hit.content.lower():
                        status = SUPPLIER_RESTRICTED
                        trust = TRUST_UNVERIFIED
                    supplier = Supplier(
                        supplier_id=sid,
                        scope=scope,
                        name=name,
                        source="knowledge",
                        source_ref=hit.citation_ref,
                        categories=(requirement.category,),
                        trust_level=trust,
                        status=status,
                        provenance={
                            "citation_ref": hit.citation_ref,
                            "source_id": hit.source_id,
                            "trust_level": hit.trust_level,
                        },
                        metadata_safe={"from_knowledge": True},
                    )
                    found[sid] = supplier
                    self.supplier_repo.upsert(supplier)
            except Exception:
                pass

        # Document-backed supplier mentions
        if self.document_service is not None:
            try:
                from documents.models import DocumentSearchRequest

                docs = self.document_service.search(
                    DocumentSearchRequest(
                        scope=scope,
                        query=requirement.normalized_item,
                        limit=10,
                    ),
                    requesting_scope=scope,
                )
                for doc in docs:
                    name = self._extract_supplier_name(doc.snippet_safe)
                    if not name or name.lower() in self.excluded_names:
                        continue
                    sid = f"doc-{content_hash_text(name + doc.document_id)[:12]}"
                    if sid in excluded:
                        continue
                    supplier = Supplier(
                        supplier_id=sid,
                        scope=scope,
                        name=name,
                        source="document",
                        source_ref=doc.citation_ref,
                        categories=(requirement.category,),
                        trust_level=TRUST_DOCUMENT,
                        status=SUPPLIER_CANDIDATE,
                        provenance={
                            "document_id": doc.document_id,
                            "chunk_id": doc.chunk_id,
                            "citation_ref": doc.citation_ref,
                        },
                        metadata_safe={"from_document": True},
                    )
                    found.setdefault(sid, supplier)
                    self.supplier_repo.upsert(found[sid])
            except Exception:
                pass

        return tuple(sorted(found.values(), key=lambda s: s.supplier_id))

    def _extract_supplier_name(self, text: str) -> str | None:
        raw = str(text or "")
        # Patterns: "Supplier: Acme" or "Vendor Acme Corp"
        lower = raw.lower()
        for marker in ("supplier:", "vendor:", "from "):
            idx = lower.find(marker)
            if idx >= 0:
                rest = raw[idx + len(marker) :].strip()
                token = rest.split("\n")[0].split(",")[0].strip()
                if 2 <= len(token) <= 80:
                    return token
        return None

    def register_manual(self, supplier: Supplier) -> Supplier:
        return self.supplier_repo.upsert(supplier)
