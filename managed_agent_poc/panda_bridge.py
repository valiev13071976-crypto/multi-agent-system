"""Panda <-> Managed Agent integration boundary (PR #74 integration block).

THE ONLY file that couples ``business_assistant/conversation_gateway.py``
to ``managed_agent_poc/``. Everything else in ``managed_agent_poc/`` stays
exactly as proven in the live semantic evaluation (see the PR #74 report);
nothing in that package is modified by this integration.

Feature flag: ``PANDA_MANAGED_AGENT_ENABLED`` (default ``false``, see
``managed_agent_enabled()`` below). This is the ONE flag this integration
block adds. It is a different, outer flag from the POC's own internal
``PANDA_MANAGED_AGENT_POC_ENABLED`` safety gate (``managed_agent_poc.flags``)
-- when the outer flag is the deliberate opt-in a caller has made, this
module sets the inner one too (exactly what
``managed_agent_poc/scripts/run_live_eval.py`` already does for its own
deliberate opt-in), so operators only ever need to set ONE variable.

When ``PANDA_MANAGED_AGENT_ENABLED`` is unset/false (the default):
``maybe_respond_via_managed_agent`` is never called by
``conversation_gateway.py`` at all -- production behavior is byte-for-byte
identical to before this module existed.

When it is true AND the current turn is "eligible" (see
``_is_eligible_turn`` -- a purely STATE/attachment-based check, never a
text/phrase check, per the "no phrase-specific routing" requirement):
the turn is handed to the existing, unmodified
``managed_agent_poc.adapter.ManagedAgentPOC.run_turn`` (real model, real
Agents SDK, the SAME 3 read-only tools already proven in the PR #74 live
evaluation: ``analyze_spreadsheet``, ``select_product``,
``explain_bitrix_write_plan``). If that path is unavailable for ANY
reason (SDK not installed, no API key, disabled, subprocess error,
timeout, ...), this module returns ``None`` and the caller falls back to
the existing, unmodified conversational routing -- an optional feature
degrading gracefully must never turn into an outage.

Business-lifecycle independence ("palm + fingers"): this module never
reads or writes ``business_assistant.action_continuation.ActiveTaskStore``
(PR #72's own durable active-task/dataset-id responsibility) and never
calls ``business_assistant.controlled_bitrix_write``/
``business_assistant.product_enrichment_bridge`` or any Bitrix/Aspro
mapping code. It reuses only:
- ``managed_agent_poc.adapter.ManagedAgentPOC`` (unmodified, from #74)
- ``managed_agent_poc.state_store.ConversationStateStore`` (unmodified,
  #74's own durable dataset/session state -- reused here with a durable
  file path instead of a temp dir; NOT a new, second state store)
- ``artifacts.service.ArtifactService.get_blob`` (the SAME trusted
  attachment boundary ``data_intel.tools.DataIntelToolAdapter`` already
  uses for the existing Excel path -- never a second upload/trust path)

Zero mutation capability is exposed: the 3 tools this module can reach
are exactly the read-only set proven in PR #74's live evaluation. There
is no write/publish/price-mutation/Telegram tool anywhere on this path.
"""

from __future__ import annotations

import os
import re
import tempfile

ENABLED_ENV_VAR = "PANDA_MANAGED_AGENT_ENABLED"

# Default per-turn subprocess timeout. A module-level constant (rather than
# only a function default) so tests can raise it for their own process
# without changing the production call site in conversation_gateway.py
# (which never passes ``timeout_s`` and always gets this value).
DEFAULT_TURN_TIMEOUT_S = 60.0


def managed_agent_enabled(env: dict | None = None) -> bool:
    """Outer, production-facing flag for this integration block. Default
    OFF. Mirrors the exact boolean-parsing convention already used by
    ``managed_agent_poc.flags.is_enabled()`` and
    ``data_intel.runtime.data_intel_enabled()``."""
    source = env if env is not None else os.environ
    raw = str(source.get(ENABLED_ENV_VAR, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _safe_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "")) or "default"
    return cleaned[:128]


def _durable_paths(*, tenant_id: str, conversation_id: str) -> tuple[str, str, str]:
    """(dataset_store_path, session_db_path, state_store_path) under a
    dedicated ``managed_agent/<tenant>/<conversation>/`` directory inside
    ``PANDA_DATA_DIR`` -- the SAME "dedicated SQLite file(s) under
    PANDA_DATA_DIR" pattern ``main.py`` already uses for
    ``active_tasks.sqlite3``/``ba_api.sqlite``/``artifacts.sqlite3``.
    These three files ARE "the durable state/session work already
    implemented in #74" (``ConversationStateStore`` + the Agents SDK's own
    ``SQLiteSession`` + the dataset store) -- this function only chooses a
    durable location for them; it introduces no new store type."""
    base = os.path.join(
        os.environ.get("PANDA_DATA_DIR", "."),
        "managed_agent",
        _safe_path_component(tenant_id),
        _safe_path_component(conversation_id),
    )
    os.makedirs(base, exist_ok=True)
    return (
        os.path.join(base, "dataset.sqlite3"),
        os.path.join(base, "session.sqlite3"),
        os.path.join(base, "state.sqlite3"),
    )


def _has_existing_managed_agent_dataset(*, tenant_id: str, conversation_id: str) -> bool:
    """Cheap, direct, synchronous read of #74's OWN durable state (never
    PR #72's ``ActiveTaskStore``) -- true only when an EARLIER turn in
    THIS conversation already ingested a spreadsheet through this same
    managed-agent path. Purely state-based eligibility signal, never a
    text/phrase check."""
    if not conversation_id:
        return False
    _, _, state_path = _durable_paths(tenant_id=tenant_id, conversation_id=conversation_id)
    if not os.path.isfile(state_path):
        return False
    from managed_agent_poc.state_store import ConversationStateStore

    store = ConversationStateStore(state_path)
    persisted = store.load(tenant_id=tenant_id, conversation_id=conversation_id)
    return bool(persisted.dataset_id)


def _is_eligible_turn(*, tenant_id: str, conversation_id: str, has_spreadsheet_attachment: bool) -> bool:
    """Eligibility is decided ENTIRELY from state/attachment presence --
    never from the text of the message (no ``is_explicit_*``, no
    ``_wants_*``, no regex/stem/keyword matching of any kind). A turn is
    eligible when it attaches a new spreadsheet this turn, or when this
    conversation already has a managed-agent dataset from an earlier
    turn."""
    if has_spreadsheet_attachment:
        return True
    return _has_existing_managed_agent_dataset(tenant_id=tenant_id, conversation_id=conversation_id)


async def maybe_respond_via_managed_agent(
    *,
    text: str,
    tenant_id: str,
    owner_id: str,
    conversation_id: str,
    artifact_service=None,
    spreadsheet_ref: dict | None = None,
    timeout_s: float | None = None,
) -> dict | None:
    """Returns ``None`` when the managed-agent path should not/cannot
    handle this turn (caller must fall back to the existing conversational
    routing unchanged), or a plain ``{"text": ..., "metadata": {...}}``
    dict when it did. Never raises -- every failure mode (disabled, not
    installed, no key, subprocess error/timeout) degrades to ``None``.

    Deliberately returns a plain dict rather than a
    ``business_assistant.conversation_gateway.ConversationResult`` to
    avoid a circular import between this module and
    ``conversation_gateway.py`` (this module must stay importable and
    side-effect-free on its own, without pulling in Business Assistant).
    """
    if not conversation_id:
        return None
    effective_timeout = float(timeout_s) if timeout_s is not None else DEFAULT_TURN_TIMEOUT_S
    if not _is_eligible_turn(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        has_spreadsheet_attachment=spreadsheet_ref is not None,
    ):
        return None

    # This function is only ever reached when the caller already checked
    # managed_agent_enabled() -- that IS the deliberate opt-in the POC's
    # own inner flag exists to gate (see module docstring); mirrors
    # scripts/run_live_eval.py's identical opt-in pattern.
    os.environ["PANDA_MANAGED_AGENT_POC_ENABLED"] = "true"

    from managed_agent_poc.adapter import ManagedAgentPOC

    available, _reason = ManagedAgentPOC.real_model_available()
    if not available:
        return None

    dataset_path, session_path, state_path = _durable_paths(
        tenant_id=tenant_id, conversation_id=conversation_id
    )
    poc = ManagedAgentPOC(
        dataset_store_path=dataset_path,
        session_db_path=session_path,
        state_store_path=state_path,
    )

    artifact_bytes_path = ""
    artifact_filename = ""
    tmp_path = None
    if spreadsheet_ref is not None and artifact_service is not None:
        try:
            rec, blob = artifact_service.get_blob(
                tenant_id=tenant_id, artifact_id=str(spreadsheet_ref.get("artifact_id") or "")
            )
        except Exception:
            rec, blob = None, None
        if blob:
            fd, tmp_path = tempfile.mkstemp(prefix="panda_managed_agent_upload_", suffix=".xlsx")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                artifact_bytes_path = tmp_path
                artifact_filename = (
                    getattr(rec, "safe_filename", "") or str(spreadsheet_ref.get("filename") or "upload.xlsx")
                )
            except Exception:
                artifact_bytes_path = ""

    try:
        import asyncio
        import functools

        loop = asyncio.get_running_loop()
        turn_result = await loop.run_in_executor(
            None,
            functools.partial(
                poc.run_turn,
                text=text,
                tenant_id=tenant_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                artifact_bytes_path=artifact_bytes_path,
                artifact_filename=artifact_filename,
                timeout_s=effective_timeout,
            ),
        )
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if turn_result is None or turn_result.status != "COMPLETED":
        return None

    selected_tool = turn_result.tool_calls[0]["tool"] if turn_result.tool_calls else ""
    return {
        "text": turn_result.final_output or "",
        "metadata": {
            "action_decision": "MANAGED_AGENT",
            "managed_agent_tool": selected_tool,
            "artifacts": [],
            "mutated": False,
        },
    }
