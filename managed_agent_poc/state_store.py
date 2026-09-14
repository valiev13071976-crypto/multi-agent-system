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
  PRIMARY KEY (tenant_id, conversation_id)
);
"""


@dataclass
class PersistedState:
    dataset_id: str = ""
    shown_identifiers: list = field(default_factory=list)
    current_identifier: str = ""


class ConversationStateStore:
    def __init__(self, path: str):
        self.path = path
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def load(self, *, tenant_id: str, conversation_id: str) -> PersistedState:
        if not conversation_id:
            return PersistedState()
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT dataset_id, shown_identifiers_json, current_identifier "
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
        )

    def save(self, *, tenant_id: str, conversation_id: str, state: PersistedState) -> None:
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
