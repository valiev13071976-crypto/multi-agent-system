"""Fake supplier API provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSupplierProvider:
    price_lists: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def fetch_price_list(self, *, supplier_key: str) -> list[dict[str, Any]]:
        return list(self.price_lists.get(supplier_key) or [])
