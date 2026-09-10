"""Normalized Market Intelligence contracts.

Deliberately separate from ``telegram_interface.models``: those describe a
bot CONVERSATION turn (binding, owner, request lifecycle, callbacks). A
channel post observed through the owner's user account has no binding, no
request and no reply -- it is a read-only market fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from security.tenant import require_tenant_id

KIND_CHANNEL = "channel"
KIND_GROUP = "group"
KIND_SUPERGROUP = "supergroup"
KIND_USER = "user"
KIND_UNKNOWN = "unknown"
DIALOG_KINDS = frozenset({KIND_CHANNEL, KIND_GROUP, KIND_SUPERGROUP, KIND_USER, KIND_UNKNOWN})

# A newly discovered dialog is never read automatically: the owner opts it
# in. Discovery answers "what can this account see", monitoring answers
# "what did the owner ask Panda to read".
MONITOR_PENDING = "pending"
MONITOR_ENABLED = "enabled"
MONITOR_DISABLED = "disabled"
MONITOR_STATES = frozenset({MONITOR_PENDING, MONITOR_ENABLED, MONITOR_DISABLED})


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DiscoveredDialog:
    """One chat/channel/group the user account currently has access to."""

    dialog_id: str
    title: str
    username: str = ""
    kind: str = KIND_UNKNOWN
    is_accessible: bool = True

    def __post_init__(self):
        if self.kind not in DIALOG_KINDS:
            object.__setattr__(self, "kind", KIND_UNKNOWN)


@dataclass(frozen=True)
class RawChannelMessage:
    """One message read from a dialog. Read-only, never replied to."""

    message_id: str
    dialog_id: str
    text: str
    posted_at: datetime = field(default_factory=_utc)
    link: str = ""


@dataclass(frozen=True)
class MonitoredChannel:
    channel_id: str
    tenant_id: str
    owner_id: str
    dialog_id: str
    title: str = ""
    username: str = ""
    kind: str = KIND_UNKNOWN
    monitor_state: str = MONITOR_PENDING
    last_message_id: str = ""
    discovered_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        if self.monitor_state not in MONITOR_STATES:
            object.__setattr__(self, "monitor_state", MONITOR_PENDING)


@dataclass(frozen=True)
class ExtractedOffer:
    """What a single post deterministically claims about a product.

    Every field is optional because supplier posts are free text; the
    extractor reports only what it actually read and never guesses.
    """

    raw_text: str = ""
    title: str = ""
    brand: str = ""
    model: str = ""
    ean: str = ""
    price: Decimal | None = None
    currency: str = ""

    def has_identifier(self) -> bool:
        return bool(self.ean or self.model)


@dataclass(frozen=True)
class MarketObservation:
    """A normalized, timestamped market fact handed to Panda."""

    observation_id: str
    tenant_id: str
    channel_id: str
    dialog_id: str
    message_id: str
    observed_at: str
    title: str = ""
    brand: str = ""
    model: str = ""
    ean: str = ""
    price: Decimal | None = None
    currency: str = ""
    matched_product_id: str = ""
    match_state: str = ""
    match_method: str = ""
    source_link: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class PriceComparison:
    """Observed market prices for ONE catalog product, in ONE currency.

    Observations in another currency are never converted (no rate source
    exists here); they are reported in ``excluded`` instead.
    """

    product_id: str
    tenant_id: str
    currency: str
    observation_count: int = 0
    our_selling_price: Decimal | None = None
    our_purchase_price: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    median_price: Decimal | None = None
    cheapest_observation_id: str = ""
    sources: tuple[dict, ...] = ()
    excluded: tuple[dict, ...] = ()
