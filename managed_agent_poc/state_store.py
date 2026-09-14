"""Durable, Panda-owned business/session state for the managed-agent
POC — separate from the OpenAI Agents SDK's own ``Session`` (which only
persists conversation MESSAGE HISTORY, never arbitrary application
state).

PROVEN FINDING (see the POC writeup): running one Agents-SDK ``Runner``
turn per subprocess invocation means ``ConversationState`` (a plain
Python dataclass passed as ``context=``) is recreated from scratch on
every call -- it does NOT survive across turns just because a
``SQLiteSession`` is attached. The very first version of this POC
reproduced exactly PR #72's already-diagnosed defect shape: a fresh,
empty ``shown_identifiers`` list every turn caused "give me a different
product" to re-select the SAME product on turn 2. The session's
persisted transcript is not a substitute for durable business state.

Fix: the SAME architectural answer PR #72 already established for
Panda's own ``ActiveTaskStore`` -- a dedicated, tenant/conversation-
scoped SQLite table, loaded before the run and saved after it. This is
exactly the "Panda remains the business-control layer; business state
remains authoritative in Panda" principle in code, not merely in
prose: the managed agent runtime is trusted for language understanding
and tool selection ONLY, never for owning durable state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_agent_poc_conversation_state (
  tenant_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL DEFAULT '',
  shown_identifiers_json TEXT NOT NULL DEFAULT '[]',
  current_identifier TEXT NOT NULL DEFAULT '',
  retail_price_overrides_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, conversation_id)
);
"""

# Production defect closure (explicit user retail-price refinement lost
# between turns): a durable table created by an EARLIER build of this
# module never had ``retail_price_overrides_json`` -- ``CREATE TABLE IF
# NOT EXISTS`` above is a no-op against such a pre-existing table, so a
# real deployment's already-running conversations need this column added
# in place. ``ALTER TABLE ... ADD COLUMN`` is the standard SQLite
# migration for exactly this case; the ``duplicate column name`` error it
# raises when the column already exists (fresh DB created with the
# schema above, or a second process racing the same migration) is the
# expected/safe outcome and is swallowed, never re-raised.
_MIGRATIONS = (
    "ALTER TABLE managed_agent_poc_conversation_state "
    "ADD COLUMN retail_price_overrides_json TEXT NOT NULL DEFAULT '{}'",
)


@dataclass
class PersistedState:
    dataset_id: str = ""
    shown_identifiers: list = field(default_factory=list)
    current_identifier: str = ""
    # Production defect closure (explicit user retail-price refinement
    # lost between turns): an explicit, user-confirmed retail-price
    # override, keyed by the SAME product ``current_identifier``/row-
    # identity value this store already tracks -- never a new, separate
    # product/pricing state. Populated only by ``ConversationStateStore.
    # set_retail_price_override`` (a targeted column write -- see that
    # method's own docstring for why ``save()`` below deliberately never
    # touches this column), so an ordinary ``save()`` call from the
    # existing dataset/selection bookkeeping (``runtime_subprocess.py``,
    # this POC's own callers) can never accidentally wipe a previously
    # persisted override.
    retail_price_overrides: dict = field(default_factory=dict)


class ConversationStateStore:
    def __init__(self, path: str):
        self.path = path
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    def load(self, *, tenant_id: str, conversation_id: str) -> PersistedState:
        if not conversation_id:
            return PersistedState()
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT dataset_id, shown_identifiers_json, current_identifier, retail_price_overrides_json "
                "FROM managed_agent_poc_conversation_state WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return PersistedState()
        return PersistedState(
            dataset_id=str(row[0] or ""),
            shown_identifiers=list(json.loads(row[1] or "[]")),
            current_identifier=str(row[2] or ""),
            retail_price_overrides=dict(json.loads(row[3] or "{}")),
        )

    def save(self, *, tenant_id: str, conversation_id: str, state: PersistedState) -> None:
        """Persists the dataset/selection bookkeeping fields ONLY
        (``dataset_id``/``shown_identifiers``/``current_identifier``) --
        deliberately UNCHANGED by the retail-price-override defect
        closure below. ``state.retail_price_overrides`` is never read
        here and the ``UPDATE SET`` clause never lists ``retail_price_
        overrides_json``, so an ordinary caller (``runtime_subprocess.
        py``'s own dataset/selection persistence, or any existing test
        double built before that column existed) can keep constructing
        a fresh ``PersistedState(...)`` -- with the override field at
        its empty-dict default -- without ever wiping out a previously
        persisted override; see ``set_retail_price_override`` for the
        ONLY write path that column has."""
        if not conversation_id:
            return
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                INSERT INTO managed_agent_poc_conversation_state (
                    tenant_id, conversation_id, dataset_id, shown_identifiers_json, current_identifier
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, conversation_id) DO UPDATE SET
                    dataset_id = excluded.dataset_id,
                    shown_identifiers_json = excluded.shown_identifiers_json,
                    current_identifier = excluded.current_identifier
                """,
                (tenant_id, conversation_id, state.dataset_id, json.dumps(list(state.shown_identifiers)), state.current_identifier),
            )
            conn.commit()
        finally:
            conn.close()

    def set_retail_price_override(
        self, *, tenant_id: str, conversation_id: str, product_identifier: str, price: str
    ) -> None:
        """Durably persists ONE explicit, already-validated user retail-
        price override for ONE product (``product_identifier`` -- the
        SAME row-identity value ``current_identifier``/``shown_
        identifiers`` already use), product-scoped and surviving
        repeated turns/process restarts. A targeted column write (never
        goes through ``save()``/``PersistedState`` for the OTHER three
        fields) so this can never race with or be wiped by the dataset/
        selection bookkeeping ``save()`` above performs on every turn.
        A later call with the SAME ``product_identifier`` replaces the
        earlier value (latest explicit override wins); a DIFFERENT
        ``product_identifier`` is a distinct dict key, so switching to a
        different product never inherits this one's override."""
        if not conversation_id or not product_identifier:
            return
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT retail_price_overrides_json FROM managed_agent_poc_conversation_state "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()
            overrides = dict(json.loads((row[0] if row else None) or "{}"))
            overrides[product_identifier] = price
            conn.execute(
                """
                INSERT INTO managed_agent_poc_conversation_state (
                    tenant_id, conversation_id, retail_price_overrides_json
                ) VALUES (?, ?, ?)
                ON CONFLICT (tenant_id, conversation_id) DO UPDATE SET
                    retail_price_overrides_json = excluded.retail_price_overrides_json
                """,
                (tenant_id, conversation_id, json.dumps(overrides)),
            )
            conn.commit()
        finally:
            conn.close()
