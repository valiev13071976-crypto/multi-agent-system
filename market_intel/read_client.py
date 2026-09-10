"""MTProto read-side transport for the owner's OWN Telegram account.

The existing Bot API provider cannot do this at all: a bot cannot
enumerate the chats a user has joined, cannot read a channel it was not
added to, and has no message-search API. That is why this is a new
transport rather than an extension of
``integrations.production.adapters.telegram``.

The contract is deliberately READ-ONLY. There is no send, no join, no
leave and no edit here, and ``READ_ONLY_OPERATIONS`` below is asserted by
the test suite so a write cannot be added to this seam by accident.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from market_intel.config import (
    telegram_api_credentials_configured,
    telegram_user_live_selected,
)
from market_intel.errors import (
    MI_CLIENT_UNAVAILABLE,
    MI_LIVE_FORBIDDEN,
    MI_SESSION_MISSING,
    MarketIntelError,
)
from market_intel.models import (
    KIND_CHANNEL,
    KIND_GROUP,
    KIND_SUPERGROUP,
    KIND_UNKNOWN,
    KIND_USER,
    DiscoveredDialog,
    RawChannelMessage,
)

READ_ONLY_OPERATIONS = ("list_dialogs", "fetch_history", "search_messages")


class TelegramReadClient(Protocol):
    """Everything Market Intelligence is allowed to ask Telegram to do."""

    def list_dialogs(self, *, limit: int = 200) -> list[DiscoveredDialog]:
        """Channels/groups this account currently has access to."""

    def fetch_history(
        self, *, dialog_id: str, min_message_id: str = "", limit: int = 100
    ) -> list[RawChannelMessage]:
        """Messages newer than ``min_message_id``, oldest first."""

    def search_messages(
        self, *, dialog_id: str, query: str, limit: int = 50
    ) -> list[RawChannelMessage]:
        """Server-side search within one accessible dialog."""


@dataclass
class FixtureTelegramReadClient:
    """Offline client used whenever live account reading is not selected.

    Same role ``FakeTelegramProvider`` plays for the Bot API path: the
    whole pipeline stays exercisable with zero network and zero account
    credentials.
    """

    dialogs: list[DiscoveredDialog] = field(default_factory=list)
    messages: dict[str, list[RawChannelMessage]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def list_dialogs(self, *, limit: int = 200) -> list[DiscoveredDialog]:
        self.calls.append(("list_dialogs", ""))
        return list(self.dialogs)[: max(0, int(limit))]

    def fetch_history(
        self, *, dialog_id: str, min_message_id: str = "", limit: int = 100
    ) -> list[RawChannelMessage]:
        self.calls.append(("fetch_history", str(dialog_id)))
        items = sorted(self.messages.get(str(dialog_id), []), key=lambda m: _as_int(m.message_id))
        floor = _as_int(min_message_id)
        return [m for m in items if _as_int(m.message_id) > floor][: max(0, int(limit))]

    def search_messages(
        self, *, dialog_id: str, query: str, limit: int = 50
    ) -> list[RawChannelMessage]:
        self.calls.append(("search_messages", str(dialog_id)))
        needle = str(query or "").strip().casefold()
        if not needle:
            return []
        items = sorted(self.messages.get(str(dialog_id), []), key=lambda m: _as_int(m.message_id))
        return [m for m in items if needle in (m.text or "").casefold()][: max(0, int(limit))]


def _as_int(value: object) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except ValueError:
        return 0


def _entity_kind(entity: Any) -> str:
    """Map a Telethon entity onto our own vocabulary using only the public
    boolean attributes Telethon documents, never its class names."""
    if getattr(entity, "broadcast", False):
        return KIND_CHANNEL
    if getattr(entity, "megagroup", False):
        return KIND_SUPERGROUP
    if getattr(entity, "title", None) is not None and not hasattr(entity, "bot"):
        return KIND_GROUP
    if hasattr(entity, "bot") or getattr(entity, "first_name", None) is not None:
        return KIND_USER
    return KIND_UNKNOWN


def _posted_at(message: Any) -> datetime:
    raw = getattr(message, "date", None)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


@dataclass
class MTProtoTelegramReadClient:
    """Real MTProto client, backed by Telethon.

    Telethon is an OPTIONAL dependency: it is imported lazily so that a
    deployment which has not authorized live account reading never needs
    it, and its absence fails closed with ``mi_client_unavailable``
    instead of degrading to a silent fixture.

    ``client_factory`` is the seam the tests drive: the risky part of this
    adapter is mapping Telethon objects onto our own contracts, and that
    mapping is exercised without the dependency.
    """

    api_id: int
    api_hash: str
    session_string: str
    client_factory: Callable[[], Any] | None = None

    def __repr__(self) -> str:
        return f"MTProtoTelegramReadClient(api_id={self.api_id!r}, api_hash=[REDACTED], session=[REDACTED])"

    def _build_client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        try:  # pragma: no cover - exercised via the fail-closed path below
            from telethon.sessions import StringSession
            from telethon.sync import TelegramClient
        except ImportError as exc:
            raise MarketIntelError(
                MI_CLIENT_UNAVAILABLE,
                "telethon is not installed; live Telegram account reading is unavailable",
                http_status=503,
            ) from exc
        return TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

    @contextmanager
    def _session(self):
        client = self._build_client()
        connect = getattr(client, "connect", None)
        if callable(connect):
            connect()
        try:
            authorized = getattr(client, "is_user_authorized", None)
            if callable(authorized) and not authorized():
                raise MarketIntelError(
                    MI_SESSION_MISSING,
                    "stored Telegram user session is not authorized",
                    http_status=403,
                )
            yield client
        finally:
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                disconnect()

    def list_dialogs(self, *, limit: int = 200) -> list[DiscoveredDialog]:
        out: list[DiscoveredDialog] = []
        with self._session() as client:
            for dialog in _iterable(client.iter_dialogs(limit=int(limit))):
                entity = getattr(dialog, "entity", dialog)
                dialog_id = getattr(dialog, "id", None)
                if dialog_id is None:
                    dialog_id = getattr(entity, "id", "")
                out.append(
                    DiscoveredDialog(
                        dialog_id=str(dialog_id),
                        title=str(getattr(dialog, "name", "") or getattr(entity, "title", "") or ""),
                        username=str(getattr(entity, "username", "") or ""),
                        kind=_entity_kind(entity),
                        is_accessible=not bool(getattr(entity, "left", False)),
                    )
                )
        return out

    def fetch_history(
        self, *, dialog_id: str, min_message_id: str = "", limit: int = 100
    ) -> list[RawChannelMessage]:
        with self._session() as client:
            entity = client.get_entity(_as_int(dialog_id) or str(dialog_id))
            raw = client.iter_messages(
                entity, min_id=_as_int(min_message_id), limit=int(limit), reverse=True
            )
            return self._map_messages(raw, dialog_id)

    def search_messages(
        self, *, dialog_id: str, query: str, limit: int = 50
    ) -> list[RawChannelMessage]:
        with self._session() as client:
            entity = client.get_entity(_as_int(dialog_id) or str(dialog_id))
            raw = client.iter_messages(entity, search=str(query or ""), limit=int(limit))
            return self._map_messages(raw, dialog_id)

    @staticmethod
    def _map_messages(raw: Iterable[Any], dialog_id: str) -> list[RawChannelMessage]:
        out: list[RawChannelMessage] = []
        for message in _iterable(raw):
            text = str(getattr(message, "message", "") or getattr(message, "text", "") or "")
            if not text.strip():
                continue
            out.append(
                RawChannelMessage(
                    message_id=str(getattr(message, "id", "")),
                    dialog_id=str(dialog_id),
                    text=text,
                    posted_at=_posted_at(message),
                )
            )
        out.sort(key=lambda m: _as_int(m.message_id))
        return out


def _iterable(raw: Any) -> Iterable[Any]:
    return raw if raw is not None else ()


def select_telegram_read_client(
    env: dict,
    *,
    session_string: str = "",
    client_factory: Callable[[], Any] | None = None,
    fixture: FixtureTelegramReadClient | None = None,
) -> TelegramReadClient:
    """Fixture unless live account reading is explicitly selected; then the
    real MTProto client or a fail-closed error — never a silent fallback."""
    if not telegram_user_live_selected(env):
        return fixture or FixtureTelegramReadClient()
    if not telegram_api_credentials_configured(env):
        raise MarketIntelError(
            MI_LIVE_FORBIDDEN,
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required when the Telegram user client is live",
            http_status=403,
        )
    if not str(session_string or "").strip():
        raise MarketIntelError(
            MI_SESSION_MISSING,
            "no stored Telegram user session for this owner",
            http_status=403,
        )
    return MTProtoTelegramReadClient(
        api_id=_as_int(env.get("TELEGRAM_API_ID")),
        api_hash=str(env.get("TELEGRAM_API_HASH") or ""),
        session_string=str(session_string),
        client_factory=client_factory,
    )
