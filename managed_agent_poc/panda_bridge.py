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

Production defect closure (real Railway production, ``PANDA_MANAGED_
AGENT_ENABLED=true``, real XLSX attachment: the managed-agent path was
silently never entered -- every eligible turn still got the old generic
Excel min/max/average analysis): root cause was that the isolated OpenAI
Agents SDK install this module's ``ManagedAgentPOC.real_model_available()``
depends on was never provisioned in the deployed container (Railway/
Nixpacks builds only ever run ``pip install -r requirements.txt``; the
one-time ``scripts/setup_isolated_env.py`` step was manual-only and
nothing in the deploy path called it) -- so ``available`` was always
``False`` on every single production request, and the existing fail-open
``return None`` below (correct in isolation -- an optional feature must
degrade, never outage) silently swallowed that specific, fixable,
non-secret reason with no trace in any log. Two additive fixes, both
still fully fail-open and still gated by the SAME outer flag:
1. ``isolated_env.ensure_installed()`` is now called here, lazily, before
   the availability check -- a self-healing, at-most-once-per-process
   bootstrap of the exact SAME one-time install (see that module for why
   this is safe: idempotent, never retried after one attempt, a fast
   no-op once already installed).
2. A fallback for ANY reason (still/again unavailable, disabled,
   subprocess error, timeout, non-COMPLETED result) is now logged via the
   standard ``logging`` module at WARNING -- text only ever drawn from
   already-secret-free sources (``describe_unavailable()``'s static
   message, an exception's type name, a bounded/redacted stderr tail)
   so this fallback is finally OBSERVABLE in production logs without
   ever risking a leaked ``OPENAI_API_KEY``/``BITRIX_WEBHOOK_URL``/
   Telegram secret.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Mapping

from managed_agent_poc import isolated_env

logger = logging.getLogger(__name__)

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


def _redact_for_log(text: str) -> str:
    """Defensive scrub applied to EVERY string this module logs (never the
    raw exception/subprocess text directly): strips any occurrence of the
    current process's own ``OPENAI_API_KEY`` value, if set, before the
    text is handed to ``logging``. Diagnostic log text here is already
    drawn only from secret-free sources (see module docstring), but this
    is a zero-cost extra guard against ever repeating the prior incident
    where a key was accidentally printed while debugging."""
    safe = str(text or "")
    key = os.environ.get("OPENAI_API_KEY") or ""
    if key:
        safe = safe.replace(key, "[REDACTED]")
    return safe[:1000]


# Production defect closure (degraded raw-row card): the isolated
# subprocess's 3 tools (see ``runtime_subprocess.py``) are deliberately
# thin, read-only ROW PROJECTIONS over the already-ingested spreadsheet --
# they carry no pricing/category/media/characteristics/Bitrix-mapping
# logic at all, by design (module docstring: "Business rules stay in
# Panda"). ``select_product`` (a specific product becomes the
# conversation's current selection) and ``explain_bitrix_write_plan`` (the
# user asks to see/confirm what would be written to Bitrix/Aspro for the
# selected product) are the two tool calls whose OWN docstrings already
# describe the exact semantic intent the required flow calls "selected
# product / active product state" -- once either fires, this module
# DELEGATES the resolved raw fields into the EXISTING, unmodified
# ``business_assistant.product_enrichment_bridge.prepare_complete_card``
# capability (the SAME one ``WorkflowPandaConversationGateway.
# _invoke_product_enrichment`` already calls for the legacy
# CALL_PRODUCT_ENRICHMENT decision) instead of ever answering from the
# raw tool output directly. No pricing/category/media/characteristics/
# Bitrix-mapping logic is added here -- only field-name translation and a
# pass-through call.
_PRODUCT_RESOLVING_TOOLS = ("select_product", "explain_bitrix_write_plan")


def _selected_product_raw_fields(tool_calls: list) -> dict | None:
    """Finds the LAST tool call in this turn that resolved a specific
    product -- ``select_product``'s own ``SELECTED`` status, or
    ``explain_bitrix_write_plan``'s own ``WRITE_PLAN`` status -- and
    returns its raw, un-enriched field dict (the SAME row-projection
    shape ``managed_agent_poc.runtime_subprocess._row_product_fields``
    already produces: name/sku/ean/barcode/category/brand/purchase_price/
    retail_price/price). Returns ``None`` when no product was resolved
    this turn at all (e.g. a pure ``analyze_spreadsheet`` turn, or a
    NOT_FOUND/NO_PRODUCT_SELECTED lookup) -- the caller must then fall
    back to the model's own ``final_output`` text unchanged."""
    resolved: dict | None = None
    for call in tool_calls or []:
        if not isinstance(call, Mapping):
            continue
        tool = str(call.get("tool") or "")
        output = call.get("output")
        if tool not in _PRODUCT_RESOLVING_TOOLS or not isinstance(output, Mapping):
            continue
        if tool == "select_product" and output.get("status") == "SELECTED":
            resolved = {k: v for k, v in output.items() if k not in ("status", "matched_by")}
        elif tool == "explain_bitrix_write_plan" and output.get("status") == "WRITE_PLAN":
            would_write = output.get("would_write")
            if isinstance(would_write, Mapping):
                resolved = dict(would_write)
    return resolved


def _canonical_fields_and_retail_price(raw: Mapping) -> tuple[dict, str]:
    """Translates the managed-agent's raw row-projection field NAMES onto
    the EXACT canonical field-name contract
    ``data_intel.service._row_lookup_result``'s own ``product_fields``
    already uses -- the SAME shape
    ``business_assistant.product_enrichment_bridge``/
    ``business_assistant.controlled_bitrix_write`` already consume for
    the legacy conversational path. Pure renaming -- no value is
    computed, derived, or invented here."""
    product_fields = {
        "title": str(raw.get("name") or raw.get("title") or ""),
        "sku": str(raw.get("sku") or ""),
        "ean": str(raw.get("ean") or raw.get("barcode") or ""),
        "category": str(raw.get("category") or ""),
        "brand": str(raw.get("brand") or ""),
        "purchase_price": str(raw.get("purchase_price") or ""),
    }
    # Retail price follows the EXACT SAME priority the existing
    # ``data_intel.service._row_lookup_result`` already establishes for
    # this same raw shape: an explicit selling-price value (here, either
    # of the two keys the isolated tools may have populated) before an
    # empty string -- Panda never derives/invents a retail price here.
    retail_price = str(raw.get("retail_price") or raw.get("price") or "")
    return product_fields, retail_price


async def _delegate_to_existing_product_preparation(
    raw_fields: Mapping,
    *,
    tenant_id: str,
    tool_gateway=None,
    bitrix_bridge=None,
    media_fetcher=None,
    enrichment_cache=None,
) -> dict | None:
    """Delegates a managed-agent-resolved product selection into the
    EXISTING, unmodified deterministic Product Enrichment / controlled
    Bitrix write-plan pipeline
    (``business_assistant.product_enrichment_bridge.prepare_complete_card``).

    Owns NO pricing/category/media/characteristics/Bitrix-mapping logic
    of its own -- it only translates field names (see
    ``_canonical_fields_and_retail_price``) and calls straight through to
    the SAME capability the legacy CALL_PRODUCT_ENRICHMENT decision
    already uses. Never mutates Bitrix (``prepare_complete_card`` itself
    is read-only -- see its own docstring). Returns ``None`` (never
    raises) when the resolved fields lack a title/sku the existing
    pipeline requires, or when the existing pipeline itself raises for
    any reason -- callers must fall back to the raw tool/model output
    unchanged, exactly like every other failure mode in this module."""
    from business_assistant.product_enrichment_bridge import prepare_complete_card

    product_fields, retail_price = _canonical_fields_and_retail_price(raw_fields)
    if not product_fields.get("title") or not product_fields.get("sku"):
        return None
    try:
        return await prepare_complete_card(
            tenant_id=tenant_id,
            product_fields=product_fields,
            retail_price=retail_price,
            bitrix_bridge=bitrix_bridge,
            tool_gateway=tool_gateway,
            media_fetcher=media_fetcher,
            cache=enrichment_cache,
        )
    except Exception:  # noqa: BLE001 -- an optional enrichment delegation must never crash a turn
        logger.warning(
            "managed_agent_poc: delegation into product_enrichment_bridge.prepare_complete_card "
            "failed for tenant=%s -- falling back to the raw managed-agent tool output",
            _safe_path_component(tenant_id),
            exc_info=True,
        )
        return None


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
    # Production defect closure (degraded raw-row card): optional handles
    # to the SAME EXISTING deterministic capabilities the legacy
    # ``resolve_action_turn``/``CALL_PRODUCT_ENRICHMENT`` path already
    # uses (``business_assistant.conversation_gateway.
    # WorkflowPandaConversationGateway`` passes its OWN ``self._tool_
    # gateway``/``self._bitrix_bridge``/``self._media_fetcher``/``self.
    # _enrichment_cache`` straight through -- never a second instance of
    # any of them). All default ``None`` so existing callers that do not
    # pass them (e.g. any direct test of this function written before
    # this defect closure) keep working exactly as before, just without
    # delegation (falls back to the raw tool output, same as a resolution
    # failure).
    tool_gateway=None,
    bitrix_bridge=None,
    media_fetcher=None,
    enrichment_cache=None,
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

    When the managed agent resolves a specific product this turn (via
    ``select_product``/``explain_bitrix_write_plan`` -- see
    ``_selected_product_raw_fields``), the response text/metadata are
    built by DELEGATING that resolved product into the EXISTING
    deterministic ``business_assistant.product_enrichment_bridge.
    prepare_complete_card`` capability (see
    ``_delegate_to_existing_product_preparation``) instead of the raw
    tool/model output -- Managed Agent stays the semantic/orchestration
    layer; Panda's existing business capabilities stay the source of
    truth. Falls back to the raw model output unchanged whenever no
    product was resolved this turn, the resolved fields are incomplete,
    or the existing pipeline itself fails for any reason.
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

    # Production defect closure: self-healing, lazy, at-most-once-per-
    # process bootstrap of the isolated SDK install this availability
    # check depends on (see module + isolated_env.py docstrings). A no-op
    # in any environment where it is already installed (e.g. this repo's
    # own tests/dev sandbox), so this call is safe to make unconditionally
    # here.
    isolated_env.ensure_installed()

    available, reason = ManagedAgentPOC.real_model_available()
    if not available:
        logger.warning(
            "managed_agent_poc: eligible turn (tenant=%s, conversation has "
            "attachment=%s) fell back to the legacy conversational path -- "
            "managed-agent runtime unavailable: %s",
            _safe_path_component(tenant_id),
            spreadsheet_ref is not None,
            _redact_for_log(reason),
        )
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
    except Exception as exc:
        logger.warning(
            "managed_agent_poc: eligible turn (tenant=%s) fell back to the "
            "legacy conversational path -- run_turn raised %s: %s",
            _safe_path_component(tenant_id),
            type(exc).__name__,
            _redact_for_log(str(exc)),
        )
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if turn_result is None or turn_result.status != "COMPLETED":
        logger.warning(
            "managed_agent_poc: eligible turn (tenant=%s) fell back to the "
            "legacy conversational path -- run_turn returned status=%s error=%s",
            _safe_path_component(tenant_id),
            getattr(turn_result, "status", None),
            _redact_for_log(getattr(turn_result, "error", "")),
        )
        return None

    selected_tool = turn_result.tool_calls[0]["tool"] if turn_result.tool_calls else ""
    response_text = turn_result.final_output or ""
    metadata: dict = {
        "action_decision": "MANAGED_AGENT",
        "managed_agent_tool": selected_tool,
        "artifacts": [],
        "mutated": False,
    }

    raw_fields = _selected_product_raw_fields(turn_result.tool_calls)
    if raw_fields is not None:
        delegated = await _delegate_to_existing_product_preparation(
            raw_fields,
            tenant_id=tenant_id,
            tool_gateway=tool_gateway,
            bitrix_bridge=bitrix_bridge,
            media_fetcher=media_fetcher,
            enrichment_cache=enrichment_cache,
        )
        if delegated is not None:
            response_text = str(delegated.get("text") or "")
            metadata["delegated_to"] = "product_enrichment_bridge.prepare_complete_card"
            write_preview = delegated.get("write_preview") or {}
            if write_preview:
                metadata["bitrix_write_preview"] = write_preview

    return {"text": response_text, "metadata": metadata}
