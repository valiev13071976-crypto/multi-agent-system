"""Market Intelligence service.

Pipeline, end to end:

    owner's Telegram account (read-only MTProto)
      -> discover accessible channels/groups        (discover_channels)
      -> owner opts a channel in                    (set_monitoring)
      -> read/search its messages                   (ingest_channel / search_channel)
      -> deterministic offer extraction             (market_intel.extract)
      -> EXISTING product matcher over EXISTING
         normalized catalog data                    (market_intel.catalog_adapter)
      -> normalized observations + price comparison  (this module)

Nothing here writes to Telegram, to the catalog, or to any existing
subsystem's storage.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal

from security.tenant import require_tenant_id

from market_intel.catalog_adapter import (
    attributed_product_id,
    catalog_currency,
    catalog_purchase_price,
    catalog_selling_price,
    find_product,
    load_catalog,
    match_offer,
)
from market_intel.errors import (
    MI_CHANNEL_NOT_FOUND,
    MI_CHANNEL_NOT_MONITORED,
    MI_NO_COMPARABLE_OBSERVATIONS,
    MI_PRODUCT_NOT_FOUND,
    MarketIntelError,
)
from market_intel.extract import extract_offer
from market_intel.models import (
    MONITOR_ENABLED,
    MONITOR_STATES,
    MarketObservation,
    MonitoredChannel,
    PriceComparison,
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value: object) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except ValueError:
        return 0


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


class MarketIntelligenceService:
    def __init__(
        self,
        *,
        store,
        read_client=None,
        read_client_factory=None,
        catalog=None,
        session_vault=None,
        default_tenant_id: str = "tenant-a",
        live_reading: bool = False,
    ):
        self.store = store
        self.read_client = read_client
        # Live reading resolves a client per (tenant, owner) because the
        # MTProto session belongs to one specific account; the offline
        # fixture client has no such identity and is shared.
        self.read_client_factory = read_client_factory
        self.catalog = catalog
        self.session_vault = session_vault
        self.default_tenant_id = default_tenant_id
        self.live_reading = bool(live_reading)

    def close(self) -> None:
        self.store.close()

    def _client(self, tenant_id: str, owner_id: str):
        if self.read_client_factory is not None:
            return self.read_client_factory(tenant_id, owner_id)
        return self.read_client

    # ---------------- discovery ----------------

    def discover_channels(
        self, *, tenant_id: str, owner_id: str, actor_id: str = "", limit: int = 200
    ) -> dict:
        """Enumerate what the account can currently see.

        Newly discovered dialogs are recorded as ``pending``: discovery
        answers "what is reachable", not "what may Panda read". Reading
        starts only after the owner opts a channel in.
        """
        tenant = require_tenant_id(tenant_id)
        discovered = self._client(tenant, owner_id).list_dialogs(limit=limit)
        new_ids: list[str] = []
        known_ids: list[str] = []
        for dialog in discovered:
            if not dialog.is_accessible:
                continue
            channel, is_new = self.store.upsert_channel(
                tenant_id=tenant,
                owner_id=owner_id,
                dialog_id=str(dialog.dialog_id),
                title=dialog.title,
                username=dialog.username,
                kind=dialog.kind,
            )
            (new_ids if is_new else known_ids).append(channel.channel_id)
        self.store.append_audit(
            actor_id=actor_id or owner_id,
            tenant_id=tenant,
            action="channels.discover",
            detail=f"new={len(new_ids)} known={len(known_ids)}",
        )
        return {
            "status": "ok",
            "tenant_id": tenant,
            "discovered": len(new_ids) + len(known_ids),
            "new_channel_ids": tuple(new_ids),
            "known_channel_ids": tuple(known_ids),
        }

    def list_channels(self, *, tenant_id: str, monitor_state: str = "") -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        return [asdict(c) for c in self.store.list_channels(tenant_id=tenant, monitor_state=monitor_state)]

    def set_monitoring(
        self, *, tenant_id: str, channel_id: str, monitor_state: str, actor_id: str = ""
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        if monitor_state not in MONITOR_STATES:
            raise MarketIntelError(MI_CHANNEL_NOT_FOUND, f"unknown monitor state {monitor_state!r}")
        channel = self._require_channel(tenant, channel_id)
        self.store.set_monitor_state(
            tenant_id=tenant, channel_id=channel.channel_id, monitor_state=monitor_state
        )
        self.store.append_audit(
            actor_id=actor_id,
            tenant_id=tenant,
            action="channel.monitor",
            channel_id=channel.channel_id,
            detail=monitor_state,
        )
        return {"status": "ok", "channel_id": channel.channel_id, "monitor_state": monitor_state}

    # ---------------- reading ----------------

    def ingest_channel(self, *, tenant_id: str, channel_id: str, limit: int = 100) -> dict:
        tenant = require_tenant_id(tenant_id)
        channel = self._require_channel(tenant, channel_id)
        if channel.monitor_state != MONITOR_ENABLED:
            raise MarketIntelError(
                MI_CHANNEL_NOT_MONITORED,
                f"channel {channel.channel_id} is {channel.monitor_state}; enable monitoring first",
                http_status=409,
            )
        messages = self._client(tenant, channel.owner_id).fetch_history(
            dialog_id=channel.dialog_id, min_message_id=channel.last_message_id, limit=limit
        )
        result = self._absorb(tenant=tenant, channel=channel, messages=messages)
        highest = max((_as_int(m.message_id) for m in messages), default=0)
        if highest > _as_int(channel.last_message_id):
            self.store.set_last_message_id(
                tenant_id=tenant, channel_id=channel.channel_id, last_message_id=str(highest)
            )
        self.store.append_audit(
            actor_id=channel.owner_id,
            tenant_id=tenant,
            action="channel.ingest",
            channel_id=channel.channel_id,
            detail=f"read={result['messages_read']} observed={result['observations_created']}",
        )
        return {"status": "ok", "channel_id": channel.channel_id, **result}

    def ingest_monitored(self, *, tenant_id: str, limit: int = 100) -> dict:
        tenant = require_tenant_id(tenant_id)
        channels = self.store.list_channels(tenant_id=tenant, monitor_state=MONITOR_ENABLED)
        per_channel = [
            self.ingest_channel(tenant_id=tenant, channel_id=c.channel_id, limit=limit) for c in channels
        ]
        return {
            "status": "ok",
            "channels": len(per_channel),
            "observations_created": sum(r["observations_created"] for r in per_channel),
            "results": tuple(per_channel),
        }

    def search_channel(self, *, tenant_id: str, channel_id: str, query: str, limit: int = 50) -> dict:
        """Server-side search inside one monitored channel.

        Hits are absorbed through the same idempotent path as history, so
        finding a model later still back-fills its price history exactly
        once per message.
        """
        tenant = require_tenant_id(tenant_id)
        channel = self._require_channel(tenant, channel_id)
        if channel.monitor_state != MONITOR_ENABLED:
            raise MarketIntelError(
                MI_CHANNEL_NOT_MONITORED,
                f"channel {channel.channel_id} is {channel.monitor_state}; enable monitoring first",
                http_status=409,
            )
        messages = self._client(tenant, channel.owner_id).search_messages(
            dialog_id=channel.dialog_id, query=query, limit=limit
        )
        result = self._absorb(tenant=tenant, channel=channel, messages=messages)
        return {"status": "ok", "channel_id": channel.channel_id, "query": query, **result}

    def _absorb(self, *, tenant: str, channel: MonitoredChannel, messages: list) -> dict:
        products = load_catalog(self.catalog, tenant_id=tenant)
        created = 0
        duplicates = 0
        skipped = 0
        observation_ids: list[str] = []
        for message in messages:
            offer = extract_offer(message.text)
            if offer is None:
                skipped += 1
                continue
            outcome = match_offer(offer, products)
            observation = MarketObservation(
                observation_id=f"mio_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant,
                channel_id=channel.channel_id,
                dialog_id=channel.dialog_id,
                message_id=str(message.message_id),
                observed_at=message.posted_at.isoformat(),
                title=offer.title,
                brand=offer.brand,
                model=offer.model,
                ean=offer.ean,
                price=offer.price,
                currency=offer.currency,
                matched_product_id=attributed_product_id(outcome),
                match_state=outcome.state,
                match_method=outcome.method,
                source_link=message.link,
            )
            if self.store.save_observation(observation):
                created += 1
                observation_ids.append(observation.observation_id)
            else:
                duplicates += 1
        return {
            "messages_read": len(messages),
            "observations_created": created,
            "observations_duplicate": duplicates,
            "messages_without_offer": skipped,
            "observation_ids": tuple(observation_ids),
        }

    # ---------------- reporting ----------------

    def list_observations(
        self, *, tenant_id: str, product_id: str = "", channel_id: str = ""
    ) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        rows = self.store.list_observations(
            tenant_id=tenant, product_id=product_id, channel_id=channel_id
        )
        return [self._observation_view(o) for o in rows]

    def price_comparison(self, *, tenant_id: str, product_id: str) -> PriceComparison:
        """Observed market prices for one catalog product, in ONE currency.

        Currencies are never converted here — there is no rate source in
        this subsystem — so a differing currency is reported as excluded
        rather than silently folded into min/max/median.
        """
        tenant = require_tenant_id(tenant_id)
        products = load_catalog(self.catalog, tenant_id=tenant)
        product = find_product(products, product_id)
        if product is None:
            raise MarketIntelError(
                MI_PRODUCT_NOT_FOUND, f"no catalog product {product_id!r} for this tenant", http_status=404
            )
        observations = self.store.list_observations(tenant_id=tenant, product_id=product_id)
        currency = catalog_currency(product) or self._dominant_currency(observations)
        included = []
        excluded = []
        for observation in observations:
            if observation.price is None:
                excluded.append({"observation_id": observation.observation_id, "reason": "no_price"})
            elif not observation.currency:
                excluded.append({"observation_id": observation.observation_id, "reason": "unknown_currency"})
            elif currency and observation.currency != currency:
                excluded.append(
                    {
                        "observation_id": observation.observation_id,
                        "reason": "currency_mismatch",
                        "currency": observation.currency,
                    }
                )
            else:
                included.append(observation)
        if not included:
            raise MarketIntelError(
                MI_NO_COMPARABLE_OBSERVATIONS,
                f"no comparable {currency or 'same-currency'} observations for product {product_id!r}",
                http_status=404,
            )
        prices = [o.price for o in included]
        cheapest = min(included, key=lambda o: o.price)
        return PriceComparison(
            product_id=product_id,
            tenant_id=tenant,
            currency=currency or included[0].currency,
            observation_count=len(included),
            our_selling_price=catalog_selling_price(product),
            our_purchase_price=catalog_purchase_price(product),
            min_price=min(prices),
            max_price=max(prices),
            median_price=_median(prices),
            cheapest_observation_id=cheapest.observation_id,
            sources=tuple(self._observation_view(o) for o in included),
            excluded=tuple(excluded),
        )

    # ---------------- helpers ----------------

    def _require_channel(self, tenant: str, channel_id: str) -> MonitoredChannel:
        channel = self.store.get_channel(tenant_id=tenant, channel_id=channel_id)
        if channel is None:
            raise MarketIntelError(
                MI_CHANNEL_NOT_FOUND, f"no channel {channel_id!r} for this tenant", http_status=404
            )
        return channel

    @staticmethod
    def _dominant_currency(observations: list) -> str:
        counts: dict[str, int] = {}
        for observation in observations:
            if observation.currency and observation.price is not None:
                counts[observation.currency] = counts.get(observation.currency, 0) + 1
        if not counts:
            return ""
        top = max(counts.values())
        winners = sorted(code for code, n in counts.items() if n == top)
        # A tie between currencies is not resolvable without a rate source.
        return winners[0] if len(winners) == 1 else ""

    @staticmethod
    def _observation_view(observation: MarketObservation) -> dict:
        return {
            "observation_id": observation.observation_id,
            "channel_id": observation.channel_id,
            "message_id": observation.message_id,
            "observed_at": observation.observed_at,
            "title": observation.title,
            "brand": observation.brand,
            "model": observation.model,
            "ean": observation.ean,
            "price": None if observation.price is None else str(observation.price),
            "currency": observation.currency,
            "matched_product_id": observation.matched_product_id,
            "match_state": observation.match_state,
            "match_method": observation.match_method,
        }
