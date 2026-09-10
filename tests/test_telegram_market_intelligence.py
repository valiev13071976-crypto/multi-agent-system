"""Telegram Wholesale / Market Intelligence foundation.

Covers the new read-only path end to end:

    owner's Telegram account -> discover joined channels/groups
    -> read/search messages -> deterministic offer extraction
    -> EXISTING product matcher over EXISTING normalized catalog data
    -> normalized observations -> price comparison

Zero network and zero Telegram credentials: the offline
``FixtureTelegramReadClient`` stands in for the account, and the MTProto
adapter is driven through its ``client_factory`` seam with a
Telethon-shaped fake.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from fastapi.testclient import TestClient
from telethon.sessions import StringSession

from security.api_auth import configure_security
from security.auth import AuthService

from product_intel.platform_models import (
    MATCH_STATE_EXACT,
    PriceInfo,
    Product,
)
from product_intel.store import InMemoryProductCatalogStore

from market_intel.catalog_adapter import offer_candidate_fields
from market_intel.config import (
    market_intel_secret_contract,
    telegram_api_credentials_configured,
)
from market_intel.errors import (
    MI_CHANNEL_NOT_MONITORED,
    MI_CLIENT_UNAVAILABLE,
    MI_LIVE_FORBIDDEN,
    MI_NO_COMPARABLE_OBSERVATIONS,
    MI_SESSION_ENCRYPTION_REQUIRED,
    MI_SESSION_MISSING,
    MarketIntelError,
)
from market_intel.extract import extract_offer
from market_intel.models import (
    MONITOR_DISABLED,
    MONITOR_ENABLED,
    MONITOR_PENDING,
    KIND_CHANNEL,
    KIND_SUPERGROUP,
    KIND_USER,
    DiscoveredDialog,
    RawChannelMessage,
)
from market_intel.read_client import (
    READ_ONLY_OPERATIONS,
    FixtureTelegramReadClient,
    MTProtoTelegramReadClient,
    TelegramReadClient,
    select_telegram_read_client,
)
from market_intel.router import configure_market_intel_router
from market_intel.runtime import build_market_intelligence_runtime
from market_intel.service import MarketIntelligenceService
from market_intel.session_vault import TelegramSessionVault
from market_intel.store import SqliteMarketIntelStore

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
OWNER = "owner-1"

TV_EAN = "8806096824788"
TV_MODEL = "55MRGB86B6A.ARUG"

TV_POST = (
    "\U0001f4fa Телевизор LG 55MRGB86B6A.ARUG\n"
    "Артикул: 55MRGB86B6A.ARUG\n"
    f"EAN: {TV_EAN}\n"
    "Оптовая цена: 89 900 руб."
)
TV_POST_CHEAPER = (
    "Телевизор LG 55MRGB86B6A.ARUG в наличии\n"
    f"EAN: {TV_EAN}\n"
    "Цена: 84 500 руб."
)
HEADPHONES_POST = "Наушники Sony WH-1000XM5\nАртикул: WH-1000XM5\nЦена: 24 500 руб."
CHATTER_POST = "Доброе утро! Отгрузки сегодня до 18:00, склад работает."


def _encryption_key_env() -> dict[str, str]:
    return {"PANDA_ENCRYPTION_KEY": base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()}


def _catalog() -> InMemoryProductCatalogStore:
    store = InMemoryProductCatalogStore()
    store.save_product(
        Product(
            product_id="p-tv",
            tenant_id=TENANT,
            title="Телевизор LG 55MRGB86B6A",
            brand="LG",
            sku="TV-LG-01",
            gtin=TV_EAN,
            mpn=TV_MODEL,
            price=PriceInfo(
                currency="RUB",
                selling_price=Decimal("99900"),
                purchase_price=Decimal("70000"),
            ),
        )
    )
    store.save_product(
        Product(
            product_id="p-headphones",
            tenant_id=TENANT,
            title="Наушники Sony WH-1000XM5",
            brand="Sony",
            sku="HP-SONY-01",
            mpn="WH-1000XM5",
            price=PriceInfo(currency="RUB", selling_price=Decimal("29990")),
        )
    )
    return store


def _message(message_id: int, dialog_id: str, text: str, day: int = 1) -> RawChannelMessage:
    return RawChannelMessage(
        message_id=str(message_id),
        dialog_id=dialog_id,
        text=text,
        posted_at=datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc),
    )


def _fixture_client() -> FixtureTelegramReadClient:
    return FixtureTelegramReadClient(
        dialogs=[
            DiscoveredDialog(dialog_id="-100777", title="Опт Электроника", username="opt_el", kind=KIND_CHANNEL),
            DiscoveredDialog(dialog_id="-100888", title="Поставщики ТВ", kind=KIND_SUPERGROUP),
        ],
        messages={
            "-100777": [
                _message(10, "-100777", CHATTER_POST, day=1),
                _message(11, "-100777", TV_POST, day=2),
                _message(12, "-100777", HEADPHONES_POST, day=3),
            ],
            "-100888": [_message(5, "-100888", TV_POST_CHEAPER, day=4)],
        },
    )


class _TempStore:
    """Real SQLite on disk so schema/idempotency are genuinely exercised."""

    def __enter__(self) -> SqliteMarketIntelStore:
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "market_intel.sqlite")
        self.store = SqliteMarketIntelStore(self.path)
        return self.store

    def __exit__(self, *exc):
        self.store.close()
        self._dir.cleanup()
        return False


def _service(store, *, catalog=None, client=None) -> MarketIntelligenceService:
    return MarketIntelligenceService(
        store=store,
        read_client=client or _fixture_client(),
        catalog=catalog if catalog is not None else _catalog(),
        default_tenant_id=TENANT,
    )


def _enable_all(service, tenant: str = TENANT) -> list:
    service.discover_channels(tenant_id=tenant, owner_id=OWNER)
    channels = service.store.list_channels(tenant_id=tenant)
    for channel in channels:
        service.set_monitoring(
            tenant_id=tenant, channel_id=channel.channel_id, monitor_state=MONITOR_ENABLED, actor_id=OWNER
        )
    return service.store.list_channels(tenant_id=tenant)


# --------------------------------------------------------------------------
# read-side transport
# --------------------------------------------------------------------------


class ReadSideIsReadOnlyTests(unittest.TestCase):
    """A user-account session can read everything the owner can, so the
    transport seam must not be able to grow a write."""

    FORBIDDEN = (
        "send",
        "send_message",
        "reply",
        "join",
        "leave",
        "delete",
        "edit",
        "forward",
        "post",
    )

    def test_protocol_declares_only_read_operations(self):
        declared = {
            name
            for name in vars(TelegramReadClient)
            if not name.startswith("_") and callable(getattr(TelegramReadClient, name, None))
        }
        self.assertEqual(declared, set(READ_ONLY_OPERATIONS))

    def test_no_client_exposes_a_write_operation(self):
        for client in (FixtureTelegramReadClient, MTProtoTelegramReadClient):
            for name in self.FORBIDDEN:
                with self.subTest(client=client.__name__, operation=name):
                    self.assertFalse(hasattr(client, name))

    def test_mtproto_client_never_reveals_its_credentials(self):
        client = MTProtoTelegramReadClient(api_id=1, api_hash="hash-value", session_string="session-value")
        rendered = repr(client)
        self.assertNotIn("hash-value", rendered)
        self.assertNotIn("session-value", rendered)


class ClientSelectionFailsClosedTests(unittest.TestCase):
    def test_without_live_authorization_the_fixture_client_is_used(self):
        for env in (
            {},
            {"TELEGRAM_USER_CLIENT_ENABLED": "true"},
            {"TELEGRAM_USER_LIVE_ACTIVE": "true"},
        ):
            with self.subTest(env=sorted(env)):
                self.assertIsInstance(select_telegram_read_client(env), FixtureTelegramReadClient)

    def test_live_without_api_credentials_fails_closed(self):
        env = {"TELEGRAM_USER_CLIENT_ENABLED": "true", "TELEGRAM_USER_LIVE_ACTIVE": "true"}
        with self.assertRaises(MarketIntelError) as ctx:
            select_telegram_read_client(env, session_string="s")
        self.assertEqual(ctx.exception.code, MI_LIVE_FORBIDDEN)

    def test_live_without_a_stored_session_fails_closed(self):
        env = {
            "TELEGRAM_USER_CLIENT_ENABLED": "true",
            "TELEGRAM_USER_LIVE_ACTIVE": "true",
            "TELEGRAM_USER_API_ID": "12345",
            "TELEGRAM_USER_API_HASH": "abc",
        }
        with self.assertRaises(MarketIntelError) as ctx:
            select_telegram_read_client(env, session_string="")
        self.assertEqual(ctx.exception.code, MI_SESSION_MISSING)

    def test_fully_authorized_live_never_falls_back_to_the_fixture(self):
        env = {
            "TELEGRAM_USER_CLIENT_ENABLED": "true",
            "TELEGRAM_USER_LIVE_ACTIVE": "true",
            "TELEGRAM_USER_API_ID": "12345",
            "TELEGRAM_USER_API_HASH": "abc",
        }
        client = select_telegram_read_client(env, session_string="stored-session")
        self.assertIsInstance(client, MTProtoTelegramReadClient)

    def test_broken_telethon_install_fails_closed(self):
        """Telethon ships in requirements, but a broken or partial install
        must be an explicit error rather than a silent degrade to fixtures."""
        client = MTProtoTelegramReadClient(api_id=1, api_hash="h", session_string="s")
        absent = {"telethon": None, "telethon.sync": None, "telethon.sessions": None}
        with patch.dict(sys.modules, absent, clear=False):
            with self.assertRaises(MarketIntelError) as ctx:
                client.list_dialogs()
        self.assertEqual(ctx.exception.code, MI_CLIENT_UNAVAILABLE)

    def test_unreadable_stored_session_fails_closed(self):
        """Telethon rejects a corrupt session string with a bare ValueError;
        it must not escape as a 500."""
        client = MTProtoTelegramReadClient(api_id=1, api_hash="h", session_string="not-a-session")
        with self.assertRaises(MarketIntelError) as ctx:
            client.list_dialogs()
        self.assertEqual(ctx.exception.code, MI_SESSION_MISSING)
        self.assertEqual(ctx.exception.http_status, 403)


class TelethonIsADeployableDependencyTests(unittest.TestCase):
    """The MTProto read side has to be runnable after merge, so the client
    library is a normal production dependency rather than a manual step."""

    def test_declared_in_production_requirements(self):
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
        declared = [
            line.strip()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip().lower().startswith("telethon")
        ]
        self.assertEqual(len(declared), 1, declared)
        # Telethon 2.x drops ``telethon.sync``, which this client uses.
        self.assertIn("<2", declared[0])

    def test_the_import_path_the_client_uses_actually_resolves(self):
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient

        self.assertTrue(callable(TelegramClient))
        self.assertTrue(callable(StringSession))

    def test_a_real_client_is_constructed_without_any_network_call(self):
        session = StringSession()
        client = MTProtoTelegramReadClient(
            api_id=12345, api_hash="hash", session_string=session.save()
        )
        built = client._build_client()
        self.assertTrue(hasattr(built, "connect"))
        self.assertTrue(hasattr(built, "iter_messages"))


class ProductionCredentialNamesTests(unittest.TestCase):
    """The owner sets these by hand in the deployment environment, and a
    wrong name degrades silently to the fixture client rather than raising.
    Pin the exact names so a rename cannot quietly disable live reading."""

    API_ID = "TELEGRAM_USER_API_ID"
    API_HASH = "TELEGRAM_USER_API_HASH"

    def test_credential_check_reads_exactly_the_production_names(self):
        self.assertTrue(
            telegram_api_credentials_configured({self.API_ID: "12345", self.API_HASH: "abc"})
        )
        for partial in ({self.API_ID: "12345"}, {self.API_HASH: "abc"}, {}):
            with self.subTest(env=sorted(partial)):
                self.assertFalse(telegram_api_credentials_configured(partial))

    def test_bot_token_credentials_do_not_satisfy_the_user_client(self):
        """The Bot API integration is a separate credential set; its
        variables must never authorize account reading."""
        self.assertFalse(
            telegram_api_credentials_configured(
                {"TELEGRAM_BOT_TOKEN": "irrelevant", "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x"}
            )
        )

    def test_contract_advertises_the_production_names(self):
        advertised = {row["VARIABLE_NAME"] for row in market_intel_secret_contract()}
        self.assertIn(self.API_ID, advertised)
        self.assertIn(self.API_HASH, advertised)

    def test_contract_exposes_names_only_and_never_values(self):
        env = {self.API_ID: "12345", self.API_HASH: "super-secret-hash"}
        with patch.dict(os.environ, env, clear=False):
            rendered = repr(market_intel_secret_contract())
        self.assertNotIn("super-secret-hash", rendered)
        self.assertNotIn("12345", rendered)
        for row in market_intel_secret_contract():
            with self.subTest(variable=row["VARIABLE_NAME"]):
                self.assertEqual(sorted(row), ["REQUIRED", "STATUS", "VARIABLE_NAME"])


class _FakeEntity:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _FakeDialog:
    def __init__(self, dialog_id, name, entity):
        self.id = dialog_id
        self.name = name
        self.entity = entity


class _FakeMessage:
    def __init__(self, message_id, text, date=None):
        self.id = message_id
        self.message = text
        self.date = date or datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class _FakeTelethonClient:
    """Shaped like the small slice of Telethon the adapter actually uses."""

    def __init__(self, *, dialogs=(), messages=(), authorized=True):
        self._dialogs = list(dialogs)
        self._messages = list(messages)
        self._authorized = authorized
        self.connected = False
        self.disconnected = False
        self.message_kwargs: list[dict] = []
        self.entities: list = []

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def is_user_authorized(self):
        return self._authorized

    def iter_dialogs(self, limit=None):
        return self._dialogs[:limit]

    def get_entity(self, ident):
        self.entities.append(ident)
        return ident

    def iter_messages(self, entity, **kwargs):
        self.message_kwargs.append(kwargs)
        return self._messages


class MTProtoAdapterMappingTests(unittest.TestCase):
    def _client(self, fake) -> MTProtoTelegramReadClient:
        return MTProtoTelegramReadClient(
            api_id=1, api_hash="h", session_string="s", client_factory=lambda: fake
        )

    def test_dialogs_are_mapped_with_their_kind(self):
        fake = _FakeTelethonClient(
            dialogs=[
                _FakeDialog(-100777, "Опт Электроника", _FakeEntity(broadcast=True, username="opt_el")),
                _FakeDialog(-100888, "Поставщики", _FakeEntity(megagroup=True, title="Поставщики")),
                _FakeDialog(555, "Иван", _FakeEntity(first_name="Иван", bot=False)),
            ]
        )
        dialogs = self._client(fake).list_dialogs()
        self.assertEqual(
            [(d.dialog_id, d.kind) for d in dialogs],
            [("-100777", KIND_CHANNEL), ("-100888", KIND_SUPERGROUP), ("555", KIND_USER)],
        )
        self.assertEqual(dialogs[0].username, "opt_el")
        self.assertTrue(fake.connected)
        self.assertTrue(fake.disconnected)

    def test_history_is_mapped_sorted_and_empty_messages_dropped(self):
        fake = _FakeTelethonClient(
            messages=[
                _FakeMessage(12, "второе"),
                _FakeMessage(13, "   "),
                _FakeMessage(11, "первое"),
            ]
        )
        messages = self._client(fake).fetch_history(dialog_id="-100777", min_message_id="10", limit=50)
        self.assertEqual([m.message_id for m in messages], ["11", "12"])
        self.assertEqual(fake.message_kwargs[0], {"min_id": 10, "limit": 50, "reverse": True})
        self.assertEqual(fake.entities, [-100777])

    def test_search_passes_the_query_through_to_the_server(self):
        fake = _FakeTelethonClient(messages=[_FakeMessage(7, "LG 55MRGB86B6A.ARUG")])
        messages = self._client(fake).search_messages(dialog_id="-100777", query="55MRGB86B6A", limit=20)
        self.assertEqual([m.text for m in messages], ["LG 55MRGB86B6A.ARUG"])
        self.assertEqual(fake.message_kwargs[0], {"search": "55MRGB86B6A", "limit": 20})

    def test_unauthorized_session_fails_closed_and_still_disconnects(self):
        fake = _FakeTelethonClient(authorized=False)
        with self.assertRaises(MarketIntelError) as ctx:
            self._client(fake).list_dialogs()
        self.assertEqual(ctx.exception.code, MI_SESSION_MISSING)
        self.assertTrue(fake.disconnected)


# --------------------------------------------------------------------------
# deterministic extraction
# --------------------------------------------------------------------------


class OfferExtractionTests(unittest.TestCase):
    def test_russian_supplier_post_is_fully_extracted(self):
        offer = extract_offer(TV_POST)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.model, TV_MODEL)
        self.assertEqual(offer.ean, TV_EAN)
        self.assertEqual(offer.price, Decimal("89900"))
        self.assertEqual(offer.currency, "RUB")
        self.assertEqual(offer.title, "Телевизор LG 55MRGB86B6A.ARUG")

    def test_english_post_with_symbol_currency(self):
        offer = extract_offer("Sony WH-1000XM5 headphones\nSKU: WH-1000XM5\nPrice: $249.90")
        self.assertEqual(offer.model, "WH-1000XM5")
        self.assertEqual(offer.price, Decimal("249.90"))
        self.assertEqual(offer.currency, "USD")

    def test_chatter_without_an_offer_is_not_a_market_fact(self):
        self.assertIsNone(extract_offer(CHATTER_POST))
        self.assertIsNone(extract_offer(""))

    def test_number_without_a_currency_marker_is_never_read_as_a_price(self):
        """A diagonal, a capacity and a quantity are all bare numbers."""
        self.assertIsNone(extract_offer("Телевизор LG 55MRGB86B6A.ARUG 55 дюймов 3 шт"))

    def test_unit_suffixed_token_is_not_mistaken_for_a_model(self):
        offer = extract_offer("Смартфон Samsung 256GB\nАртикул: SM-S921B\nЦена: 74 900 руб.")
        self.assertEqual(offer.model, "SM-S921B")

    def test_two_competing_codes_yield_no_model_rather_than_a_guess(self):
        offer = extract_offer("Комплект 55MRGB86B6A.ARUG и OLED55C4RLA\nЦена: 150 000 руб.")
        self.assertIsNone(offer)

    def test_invalid_barcode_check_digit_is_rejected(self):
        offer = extract_offer("Телевизор\nEAN: 8806096824789\nАртикул: X1234Z\nЦена: 1 000 руб.")
        self.assertEqual(offer.ean, "")
        self.assertEqual(offer.model, "X1234Z")

    def test_price_without_an_identifier_is_not_an_observation(self):
        self.assertIsNone(extract_offer("Скидки до 30%!\nЦена: 1 000 руб."))


# --------------------------------------------------------------------------
# discovery and the monitoring gate
# --------------------------------------------------------------------------


class ChannelDiscoveryTests(unittest.TestCase):
    def test_discovered_channels_start_pending_and_are_never_read_automatically(self):
        with _TempStore() as store:
            client = _fixture_client()
            service = _service(store, client=client)
            result = service.discover_channels(tenant_id=TENANT, owner_id=OWNER)

            self.assertEqual(result["discovered"], 2)
            channels = store.list_channels(tenant_id=TENANT)
            self.assertEqual({c.monitor_state for c in channels}, {MONITOR_PENDING})
            self.assertEqual([call[0] for call in client.calls], ["list_dialogs"])

    def test_rediscovery_is_idempotent_and_preserves_the_owners_decision(self):
        with _TempStore() as store:
            service = _service(store)
            service.discover_channels(tenant_id=TENANT, owner_id=OWNER)
            first = store.list_channels(tenant_id=TENANT)[0]
            service.set_monitoring(
                tenant_id=TENANT, channel_id=first.channel_id, monitor_state=MONITOR_ENABLED, actor_id=OWNER
            )

            again = service.discover_channels(tenant_id=TENANT, owner_id=OWNER)

            self.assertEqual(again["new_channel_ids"], ())
            self.assertEqual(len(store.list_channels(tenant_id=TENANT)), 2)
            self.assertEqual(
                store.get_channel(tenant_id=TENANT, channel_id=first.channel_id).monitor_state,
                MONITOR_ENABLED,
            )

    def test_reading_a_channel_the_owner_did_not_opt_in_is_refused(self):
        with _TempStore() as store:
            client = _fixture_client()
            service = _service(store, client=client)
            service.discover_channels(tenant_id=TENANT, owner_id=OWNER)
            pending = store.list_channels(tenant_id=TENANT)[0]

            with self.assertRaises(MarketIntelError) as ctx:
                service.ingest_channel(tenant_id=TENANT, channel_id=pending.channel_id)

            self.assertEqual(ctx.exception.code, MI_CHANNEL_NOT_MONITORED)
            self.assertNotIn("fetch_history", [call[0] for call in client.calls])

    def test_disabling_monitoring_stops_further_reading(self):
        with _TempStore() as store:
            service = _service(store)
            channels = _enable_all(service)
            service.set_monitoring(
                tenant_id=TENANT,
                channel_id=channels[0].channel_id,
                monitor_state=MONITOR_DISABLED,
                actor_id=OWNER,
            )
            with self.assertRaises(MarketIntelError) as ctx:
                service.ingest_channel(tenant_id=TENANT, channel_id=channels[0].channel_id)
            self.assertEqual(ctx.exception.code, MI_CHANNEL_NOT_MONITORED)


# --------------------------------------------------------------------------
# observations over the EXISTING catalog contract
# --------------------------------------------------------------------------


class ObservationPipelineTests(unittest.TestCase):
    def test_posts_become_observations_matched_by_the_existing_matcher(self):
        with _TempStore() as store:
            service = _service(store)
            channels = _enable_all(service)
            opt = next(c for c in channels if c.dialog_id == "-100777")

            result = service.ingest_channel(tenant_id=TENANT, channel_id=opt.channel_id)

            self.assertEqual(result["messages_read"], 3)
            self.assertEqual(result["observations_created"], 2)
            self.assertEqual(result["messages_without_offer"], 1)

            by_product = {
                o["matched_product_id"]: o for o in service.list_observations(tenant_id=TENANT)
            }
            self.assertEqual(set(by_product), {"p-tv", "p-headphones"})
            self.assertEqual(by_product["p-tv"]["price"], "89900")
            self.assertEqual(by_product["p-tv"]["match_state"], MATCH_STATE_EXACT)
            self.assertEqual(by_product["p-tv"]["match_method"], "exact_ean")
            self.assertEqual(by_product["p-headphones"]["match_method"], "exact_mpn")

    def test_reingesting_the_same_channel_does_not_duplicate_history(self):
        with _TempStore() as store:
            service = _service(store)
            channels = _enable_all(service)
            opt = next(c for c in channels if c.dialog_id == "-100777")

            service.ingest_channel(tenant_id=TENANT, channel_id=opt.channel_id)
            second = service.ingest_channel(tenant_id=TENANT, channel_id=opt.channel_id)

            self.assertEqual(second["messages_read"], 0)
            self.assertEqual(second["observations_created"], 0)
            self.assertEqual(len(service.list_observations(tenant_id=TENANT)), 2)
            self.assertEqual(
                store.get_channel(tenant_id=TENANT, channel_id=opt.channel_id).last_message_id, "12"
            )

    def test_search_backfills_history_exactly_once_per_message(self):
        with _TempStore() as store:
            service = _service(store)
            channels = _enable_all(service)
            opt = next(c for c in channels if c.dialog_id == "-100777")

            first = service.search_channel(tenant_id=TENANT, channel_id=opt.channel_id, query="55MRGB86B6A")
            repeat = service.search_channel(tenant_id=TENANT, channel_id=opt.channel_id, query="55MRGB86B6A")

            self.assertEqual(first["observations_created"], 1)
            self.assertEqual(repeat["observations_created"], 0)
            self.assertEqual(repeat["observations_duplicate"], 1)

    def test_an_unmatched_offer_is_recorded_but_never_attributed(self):
        with _TempStore() as store:
            client = FixtureTelegramReadClient(
                dialogs=[DiscoveredDialog(dialog_id="-100999", title="Прочее", kind=KIND_CHANNEL)],
                messages={
                    "-100999": [
                        _message(1, "-100999", "Пылесос Dyson\nАртикул: V15DETECT\nЦена: 55 000 руб.")
                    ]
                },
            )
            service = _service(store, client=client)
            channels = _enable_all(service)
            service.ingest_channel(tenant_id=TENANT, channel_id=channels[0].channel_id)

            observation = service.list_observations(tenant_id=TENANT)[0]
            self.assertEqual(observation["model"], "V15DETECT")
            self.assertEqual(observation["matched_product_id"], "")


class PriceComparisonTests(unittest.TestCase):
    def _ingest_everything(self, store) -> MarketIntelligenceService:
        service = _service(store)
        for channel in _enable_all(service):
            service.ingest_channel(tenant_id=TENANT, channel_id=channel.channel_id)
        return service

    def test_market_prices_are_compared_against_our_catalog_price(self):
        with _TempStore() as store:
            service = self._ingest_everything(store)

            comparison = service.price_comparison(tenant_id=TENANT, product_id="p-tv")

            self.assertEqual(comparison.currency, "RUB")
            self.assertEqual(comparison.observation_count, 2)
            self.assertEqual(comparison.min_price, Decimal("84500"))
            self.assertEqual(comparison.max_price, Decimal("89900"))
            self.assertEqual(comparison.median_price, Decimal("87200"))
            self.assertEqual(comparison.our_selling_price, Decimal("99900"))
            self.assertEqual(comparison.our_purchase_price, Decimal("70000"))
            cheapest = next(
                s for s in comparison.sources if s["observation_id"] == comparison.cheapest_observation_id
            )
            self.assertEqual(cheapest["price"], "84500")

    def test_a_foreign_currency_observation_is_excluded_never_converted(self):
        with _TempStore() as store:
            client = FixtureTelegramReadClient(
                dialogs=[DiscoveredDialog(dialog_id="-100777", title="Опт", kind=KIND_CHANNEL)],
                messages={
                    "-100777": [
                        _message(1, "-100777", TV_POST),
                        _message(2, "-100777", f"LG 55MRGB86B6A.ARUG\nEAN: {TV_EAN}\nPrice: $940"),
                    ]
                },
            )
            service = _service(store, client=client)
            channels = _enable_all(service)
            service.ingest_channel(tenant_id=TENANT, channel_id=channels[0].channel_id)

            comparison = service.price_comparison(tenant_id=TENANT, product_id="p-tv")

            self.assertEqual(comparison.currency, "RUB")
            self.assertEqual(comparison.observation_count, 1)
            self.assertEqual(
                [e["reason"] for e in comparison.excluded], ["currency_mismatch"]
            )

    def test_a_product_with_no_observations_fails_closed(self):
        with _TempStore() as store:
            service = _service(store)
            with self.assertRaises(MarketIntelError) as ctx:
                service.price_comparison(tenant_id=TENANT, product_id="p-tv")
            self.assertEqual(ctx.exception.code, MI_NO_COMPARABLE_OBSERVATIONS)


class TenantIsolationTests(unittest.TestCase):
    def test_another_tenant_sees_neither_channels_nor_observations(self):
        with _TempStore() as store:
            service = _service(store)
            for channel in _enable_all(service):
                service.ingest_channel(tenant_id=TENANT, channel_id=channel.channel_id)

            self.assertEqual(service.list_channels(tenant_id=OTHER_TENANT), [])
            self.assertEqual(service.list_observations(tenant_id=OTHER_TENANT), [])


# --------------------------------------------------------------------------
# the existing catalog pipeline is only ever READ
# --------------------------------------------------------------------------


class _ReadOnlyCatalogGuard:
    """Fails the test if Market Intelligence touches anything on the
    existing catalog store other than the read method."""

    ALLOWED = "list_products"

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def list_products(self, **kwargs):
        self.calls.append(self.ALLOWED)
        return self._inner.list_products(**kwargs)

    def __getattr__(self, name):
        raise AssertionError(f"Market Intelligence must not call {name!r} on the product catalog")


class ExistingCatalogIsOnlyReadTests(unittest.TestCase):
    def test_the_whole_pipeline_uses_a_single_read_method(self):
        with _TempStore() as store:
            guard = _ReadOnlyCatalogGuard(_catalog())
            service = _service(store, catalog=guard)
            for channel in _enable_all(service):
                service.ingest_channel(tenant_id=TENANT, channel_id=channel.channel_id)
            service.price_comparison(tenant_id=TENANT, product_id="p-tv")

            self.assertTrue(guard.calls)
            self.assertEqual(set(guard.calls), {"list_products"})

    def test_observed_offers_are_translated_to_the_existing_matcher_vocabulary(self):
        offer = extract_offer(TV_POST)
        fields = offer_candidate_fields(offer)
        self.assertEqual(fields["ean"], TV_EAN)
        self.assertEqual(fields["mpn"], TV_MODEL)
        self.assertEqual(fields["sku"], TV_MODEL)
        self.assertEqual(fields["product_name"], offer.title)


# --------------------------------------------------------------------------
# encrypted user session
# --------------------------------------------------------------------------


class SessionVaultTests(unittest.TestCase):
    def test_session_is_encrypted_at_rest_and_round_trips(self):
        secret = "1BVtsOMTProtoSessionValue=="
        with _TempStore() as store, patch.dict(os.environ, _encryption_key_env(), clear=False):
            vault = TelegramSessionVault(store)
            vault.store_session(tenant_id=TENANT, owner_id=OWNER, session_string=secret, actor_id=OWNER)

            self.assertEqual(vault.load_session(tenant_id=TENANT, owner_id=OWNER), secret)
            stored = store.get_encrypted_session(tenant_id=TENANT, owner_id=OWNER)
            self.assertNotIn(secret, stored)
            with open(store.db_path, "rb") as handle:
                self.assertNotIn(secret.encode(), handle.read())

    def test_session_is_never_stored_without_an_encryption_key(self):
        with _TempStore() as store, patch.dict(os.environ, {"PANDA_ENCRYPTION_KEY": ""}, clear=False):
            vault = TelegramSessionVault(store)
            with self.assertRaises(MarketIntelError) as ctx:
                vault.store_session(tenant_id=TENANT, owner_id=OWNER, session_string="secret")
            self.assertEqual(ctx.exception.code, MI_SESSION_ENCRYPTION_REQUIRED)
            self.assertEqual(store.get_encrypted_session(tenant_id=TENANT, owner_id=OWNER), "")

    def test_missing_session_fails_closed_and_revocation_clears_it(self):
        with _TempStore() as store, patch.dict(os.environ, _encryption_key_env(), clear=False):
            vault = TelegramSessionVault(store)
            with self.assertRaises(MarketIntelError) as ctx:
                vault.load_session(tenant_id=TENANT, owner_id=OWNER)
            self.assertEqual(ctx.exception.code, MI_SESSION_MISSING)

            vault.store_session(tenant_id=TENANT, owner_id=OWNER, session_string="secret")
            vault.revoke(tenant_id=TENANT, owner_id=OWNER, actor_id=OWNER)
            self.assertFalse(vault.has_session(tenant_id=TENANT, owner_id=OWNER))

    def test_vault_never_renders_its_contents(self):
        with _TempStore() as store:
            self.assertEqual(repr(TelegramSessionVault(store)), "TelegramSessionVault(sessions=[REDACTED])")


# --------------------------------------------------------------------------
# storage is additive
# --------------------------------------------------------------------------


class AdditiveStorageTests(unittest.TestCase):
    def test_every_new_table_is_namespaced_and_created_additively(self):
        with _TempStore() as store:
            names = {
                row[0]
                for row in sqlite3.connect(store.db_path)
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
                if not row[0].startswith("sqlite_")
            }
            self.assertTrue(names)
            self.assertTrue(all(name.startswith("mi_") for name in names), names)

    def test_reopening_an_existing_database_preserves_its_rows(self):
        with _TempStore() as store:
            store.upsert_channel(
                tenant_id=TENANT,
                owner_id=OWNER,
                dialog_id="-100777",
                title="Опт",
                username="",
                kind=KIND_CHANNEL,
            )
            store.close()
            reopened = SqliteMarketIntelStore(store.db_path)
            try:
                self.assertEqual(len(reopened.list_channels(tenant_id=TENANT)), 1)
            finally:
                reopened.close()
            store._conn = sqlite3.connect(store.db_path)


# --------------------------------------------------------------------------
# runtime + HTTP surface
# --------------------------------------------------------------------------


class RuntimeCompositionTests(unittest.TestCase):
    def test_the_subsystem_is_off_unless_explicitly_enabled(self):
        with self.assertRaises(RuntimeError):
            build_market_intelligence_runtime(env={})

    def test_enabled_but_not_live_composes_with_the_offline_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = build_market_intelligence_runtime(
                env={"MARKET_INTEL_ENABLED": "true"},
                db_path=os.path.join(tmp, "mi.sqlite"),
            )
            try:
                self.assertFalse(runtime.live_reading)
                self.assertIsInstance(runtime.service.read_client, FixtureTelegramReadClient)
                self.assertIsNone(runtime.service.read_client_factory)
            finally:
                runtime.close()

    def test_live_reading_requires_a_durable_database(self):
        with self.assertRaises(RuntimeError):
            build_market_intelligence_runtime(
                env={
                    "MARKET_INTEL_ENABLED": "true",
                    "TELEGRAM_USER_CLIENT_ENABLED": "true",
                    "TELEGRAM_USER_LIVE_ACTIVE": "true",
                },
                db_path="./market_intel.sqlite",
            )

    def test_live_reading_resolves_a_client_per_owner_and_needs_a_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, _encryption_key_env(), clear=False
        ):
            runtime = build_market_intelligence_runtime(
                env={
                    "MARKET_INTEL_ENABLED": "true",
                    "TELEGRAM_USER_CLIENT_ENABLED": "true",
                    "TELEGRAM_USER_LIVE_ACTIVE": "true",
                    "TELEGRAM_USER_API_ID": "12345",
                    "TELEGRAM_USER_API_HASH": "abc",
                    "PANDA_DATA_DIR": tmp,
                },
                db_path=os.path.join(tmp, "mi.sqlite"),
            )
            try:
                self.assertTrue(runtime.live_reading)
                with self.assertRaises(MarketIntelError) as ctx:
                    runtime.service.discover_channels(tenant_id=TENANT, owner_id=OWNER)
                self.assertEqual(ctx.exception.code, MI_SESSION_MISSING)

                runtime.session_vault.store_session(
                    tenant_id=TENANT, owner_id=OWNER, session_string="stored-session"
                )
                self.assertIsInstance(
                    runtime.service._client(TENANT, OWNER), MTProtoTelegramReadClient
                )
            finally:
                runtime.close()


def _auth_env() -> dict[str, str]:
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-admin|tenant-a|admin-a|admin|secret-admin;"
            "key-user|tenant-a|user-a|user|secret-user;"
            "key-admin-b|tenant-b|admin-b|admin|secret-admin-b"
        ),
    }


class HttpSurfaceTests(unittest.TestCase):
    def test_unconfigured_router_is_published_but_unavailable(self):
        app = FastAPI()
        app.include_router(configure_market_intel_router(None))
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        self.assertIn("/api/v1/market-intel/channels/discover", spec["paths"])
        self.assertIn("/api/v1/market-intel/price-comparison/{product_id}", spec["paths"])
        self.assertEqual(client.get("/openapi.json").status_code, 200)

    def test_full_http_flow_is_rbac_and_tenant_scoped(self):
        with _TempStore() as store, patch.dict(os.environ, _encryption_key_env(), clear=False):
            service = _service(store)
            service.session_vault = TelegramSessionVault(store)
            configure_security(auth=AuthService(env=_auth_env()))
            app = FastAPI()
            app.include_router(configure_market_intel_router(service))
            client = TestClient(app)
            admin = {"X-API-Key": "secret-admin"}
            user = {"X-API-Key": "secret-user"}

            self.assertEqual(
                client.post("/api/v1/market-intel/channels/discover", headers=user).status_code, 403
            )

            discovered = client.post("/api/v1/market-intel/channels/discover", headers=admin)
            self.assertEqual(discovered.status_code, 200)
            self.assertEqual(discovered.json()["discovered"], 2)
            self.assertEqual(discovered.headers["Cache-Control"], "no-store, private")

            channels = client.get("/api/v1/market-intel/channels", headers=admin).json()["channels"]
            opt = next(c for c in channels if c["dialog_id"] == "-100777")
            self.assertEqual(opt["monitor_state"], MONITOR_PENDING)

            blocked = client.post(
                f"/api/v1/market-intel/channels/{opt['channel_id']}/ingest", headers=admin
            )
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.json()["detail"]["code"], MI_CHANNEL_NOT_MONITORED)

            client.post(
                "/api/v1/market-intel/channels/monitoring",
                json={"channel_id": opt["channel_id"], "monitor_state": MONITOR_ENABLED},
                headers=admin,
            )
            ingested = client.post(
                f"/api/v1/market-intel/channels/{opt['channel_id']}/ingest", headers=admin
            )
            self.assertEqual(ingested.json()["observations_created"], 2)

            comparison = client.get("/api/v1/market-intel/price-comparison/p-tv", headers=admin).json()
            self.assertEqual(comparison["min_price"], "89900")
            self.assertEqual(comparison["our_selling_price"], "99900")

            # tenant-b is a valid admin but must see nothing of tenant-a
            other = client.get(
                "/api/v1/market-intel/channels", headers={"X-API-Key": "secret-admin-b"}
            )
            self.assertEqual(other.json()["channels"], [])

    def test_stored_session_is_never_echoed_back(self):
        with _TempStore() as store, patch.dict(os.environ, _encryption_key_env(), clear=False):
            service = _service(store)
            service.session_vault = TelegramSessionVault(store)
            configure_security(auth=AuthService(env=_auth_env()))
            app = FastAPI()
            app.include_router(configure_market_intel_router(service))
            client = TestClient(app)
            secret = "MTProtoSessionValue123=="

            stored = client.post(
                "/api/v1/market-intel/session",
                json={"session_string": secret},
                headers={"X-API-Key": "secret-admin"},
            )
            self.assertEqual(stored.status_code, 200)
            self.assertNotIn(secret, stored.text)

            revoked = client.delete(
                "/api/v1/market-intel/session", headers={"X-API-Key": "secret-admin"}
            )
            self.assertEqual(revoked.json()["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
