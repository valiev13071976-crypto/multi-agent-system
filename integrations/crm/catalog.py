"""Tenant-scoped CRM fixture store."""

from __future__ import annotations

import copy
import uuid


def _seed() -> dict[str, dict]:
    base = {
        "contacts": {
            "cnt-1": {
                "object_type": "contact",
                "provider_id": "cnt-1",
                "name": "Ivan Petrov",
                "email": "ivan.a@example.com",
                "phone": "+79001111111",
                "company": "Acme",
            },
            "cnt-2": {
                "object_type": "contact",
                "provider_id": "cnt-2",
                "name": "Ivan Petrov",
                "email": "ivan.b@example.com",
                "phone": "+79002222222",
                "company": "Beta",
            },
            "cnt-3": {
                "object_type": "contact",
                "provider_id": "cnt-3",
                "name": "Maria Sidorova",
                "email": "maria@example.com",
                "phone": "+79003333333",
                "company": "Acme",
            },
        },
        "leads": {
            "lead-1": {
                "object_type": "lead",
                "provider_id": "lead-1",
                "title": "Website inquiry",
                "contact_id": "cnt-3",
                "status": "NEW",
                "email": "maria@example.com",
            }
        },
        "deals": {
            "deal-1": {
                "object_type": "deal",
                "provider_id": "deal-1",
                "title": "Acme supply contract",
                "amount": "150000.00",
                "currency": "RUB",
                "stage": "QUALIFICATION",
                "contact_id": "cnt-1",
            }
        },
    }
    tenant_b = copy.deepcopy(base)
    for obj_type in ("contacts", "leads", "deals"):
        for oid, obj in tenant_b[obj_type].items():
            obj["provider_id"] = f"b-{obj['provider_id']}"
            if obj_type == "contacts":
                obj["email"] = obj["email"].replace("@", "@b-")
    return {"tenant-a": copy.deepcopy(base), "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def normalize_object(raw: dict) -> dict:
    out = dict(raw)
    out["mode"] = "FIXTURE"
    out["live"] = False
    return out


class CrmStore:
    def __init__(self):
        self._data = _seed()
        self._write_counts: dict[str, int] = {}

    def bucket(self, tenant_id: str) -> dict:
        tid = tenant_id or "default"
        if tid not in self._data:
            self._data[tid] = copy.deepcopy(self._data["default"])
        return self._data[tid]

    def get(self, *, tenant_id: str, object_type: str, provider_id: str) -> dict | None:
        raw = self.bucket(tenant_id).get(object_type, {}).get(provider_id)
        return normalize_object(raw) if raw else None

    def search(self, *, tenant_id: str, object_type: str, query: str = "", page: int = 1, page_size: int = 5) -> dict:
        items = [normalize_object(v) for v in self.bucket(tenant_id).get(object_type, {}).values()]
        if query:
            q = query.casefold()
            items = [i for i in items if q in str(i.get("name", i.get("title", ""))).casefold() or q in str(i.get("email", "")).casefold()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {"items": chunk, "page": page, "next_page": next_page, "bounded": True, "mode": "FIXTURE", "live": False}

    def find_duplicates(self, *, tenant_id: str, email: str = "", phone: str = "") -> list[dict]:
        matches = []
        for c in self.bucket(tenant_id).get("contacts", {}).values():
            if email and c.get("email") == email:
                matches.append(normalize_object(c))
            elif phone and c.get("phone") == phone:
                matches.append(normalize_object(c))
        return matches

    def create_contact(self, *, tenant_id: str, payload: dict) -> dict:
        cid = f"cnt-{uuid.uuid4().hex[:6]}"
        contact = normalize_object(
            {
                "object_type": "contact",
                "provider_id": cid,
                "name": payload.get("name") or "",
                "email": payload.get("email") or "",
                "phone": payload.get("phone") or "",
                "company": payload.get("company") or "",
            }
        )
        self.bucket(tenant_id).setdefault("contacts", {})[cid] = contact
        return contact

    def patch_contact(self, *, tenant_id: str, provider_id: str, patch: dict) -> dict:
        contacts = self.bucket(tenant_id).setdefault("contacts", {})
        raw = dict(contacts.get(provider_id) or {})
        for k, v in patch.items():
            if k not in patch:
                continue
            if v is None:
                raw[k] = None
            else:
                raw[k] = v
        contacts[provider_id] = raw
        return normalize_object(raw)

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)


GLOBAL_CRM_STORE = CrmStore()
