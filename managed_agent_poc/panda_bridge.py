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

import asyncio
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

# Production defect closure #2 (real production still showed a degraded
# raw-row card after #79's delegation was added): the delegation call
# into the EXISTING ``prepare_complete_card`` pipeline has NO bound of
# its own -- it may perform real research/media-fetch network I/O in a
# real deployment (unlike this repo's own tests, which use fixtures/
# Null providers). A module-level constant (mirrors ``DEFAULT_TURN_
# TIMEOUT_S`` above) so a slow/hanging real dependency degrades to an
# explicit, honest ``PRODUCT_PREPARATION_DELEGATION_FAILED`` (see
# ``_delegate_to_existing_product_preparation``) instead of either
# hanging the whole turn or -- the actual reported defect -- silently
# falling back to presenting the raw, un-enriched tool/model projection
# as if it were the completed card.
DEFAULT_DELEGATION_TIMEOUT_S = 45.0

# Safe, non-secret, greppable diagnostic event names (production defect
# closure #2's own requirement: an OBSERVABLE execution decision at each
# stage of the delegation boundary, so a real production occurrence of
# this defect can be pinpointed to one of the documented root-cause
# hypotheses instead of staying silent). Logged via the standard
# ``logging`` module only, at INFO/WARNING -- never printed, never
# returned to the end user, never containing an API key, Bitrix webhook
# secret, Telegram secret, user credential, or a full environment dump;
# see ``_redact_for_log``/``_safe_path_component`` for the same scrubbing
# already used by every other log line in this module.
EVENT_MANAGED_PRODUCT_SELECTED = "MANAGED_PRODUCT_SELECTED"
EVENT_PRODUCT_PREPARATION_DELEGATION_STARTED = "PRODUCT_PREPARATION_DELEGATION_STARTED"
EVENT_PRODUCT_PREPARATION_DELEGATION_SUCCEEDED = "PRODUCT_PREPARATION_DELEGATION_SUCCEEDED"
EVENT_PRODUCT_PREPARATION_DELEGATION_FAILED = "PRODUCT_PREPARATION_DELEGATION_FAILED"
EVENT_PRODUCT_PREPARATION_REQUIRED_BUT_NOT_REACHED = "PRODUCT_PREPARATION_REQUIRED_BUT_NOT_REACHED"


def _log_event(event: str, *, tenant_id: str, level: int = logging.INFO, **safe_fields) -> None:
    """ONE consistent, greppable log line per diagnostic event (see the
    ``EVENT_*`` constants above). ``safe_fields`` values are stringified
    and passed through ``_redact_for_log`` -- callers must only ever pass
    already-non-secret data (a reason code, a tool name, a boolean, an
    exception class name -- never a raw exception message that might
    embed a URL/token, never any environment variable value)."""
    parts = " ".join(f"{k}={_redact_for_log(str(v))}" for k, v in safe_fields.items())
    logger.log(
        level,
        "managed_agent_poc: %s tenant=%s%s",
        event,
        _safe_path_component(tenant_id),
        f" {parts}" if parts else "",
    )


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


def _iter_resolved_product_calls(tool_calls: list):
    """Shared scan (used by BOTH ``_selected_product_raw_fields`` and
    ``_resolved_product_tool`` below) that yields ``(tool_name,
    resolved_fields)`` for every tool call in this turn that ACTUALLY
    resolved a specific product -- ``select_product``'s own ``SELECTED``
    status, or ``explain_bitrix_write_plan``'s own ``WRITE_PLAN``
    status -- in call order. A call that does not resolve a product
    (``NOT_FOUND``/``NO_PRODUCT_SELECTED``, or any other tool) is simply
    skipped, never recorded.

    Production defect closure #5 (competing-path defect after PR #82):
    the ONE thing this shared scan exists to guarantee is that "which
    fields to delegate" and "which tool produced that resolution" can
    NEVER describe two different tool calls within the same turn --
    before this helper existed, ``maybe_respond_via_managed_agent`` used
    ``turn_result.tool_calls[0]["tool"]`` for that second question, which
    silently disagreed with this scan whenever the turn's FIRST tool
    call was a distinct, unrelated, or failed attempt (e.g. a model that
    tries ``explain_bitrix_write_plan`` first -- gets ``NO_PRODUCT_
    SELECTED`` on a brand-new conversation -- and only then calls
    ``select_product``, which succeeds)."""
    for call in tool_calls or []:
        if not isinstance(call, Mapping):
            continue
        tool = str(call.get("tool") or "")
        output = call.get("output")
        if tool not in _PRODUCT_RESOLVING_TOOLS or not isinstance(output, Mapping):
            continue
        if tool == "select_product" and output.get("status") == "SELECTED":
            yield tool, {k: v for k, v in output.items() if k not in ("status", "matched_by")}
        elif tool == "explain_bitrix_write_plan" and output.get("status") == "WRITE_PLAN":
            would_write = output.get("would_write")
            if isinstance(would_write, Mapping):
                yield tool, dict(would_write)


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
    for _tool, fields in _iter_resolved_product_calls(tool_calls):
        resolved = fields
    return resolved


def _resolved_product_tool(tool_calls: list) -> str:
    """The name of the tool call that produced the CURRENT resolution --
    i.e. the exact SAME call ``_selected_product_raw_fields`` used above
    (both are driven by ``_iter_resolved_product_calls``, so they can
    never disagree) -- NOT necessarily ``tool_calls[0]``. This is the
    routing signal ``maybe_respond_via_managed_agent`` must use to
    distinguish PREPARE_PRODUCT (``select_product``) from SHOW_WRITE_PLAN
    (``explain_bitrix_write_plan``); see ``_iter_resolved_product_calls``'s
    own docstring for the exact competing-path defect this closes.
    Returns ``""`` when no product was resolved this turn (mirrors
    ``_selected_product_raw_fields``'s own ``None`` in that case)."""
    tool = ""
    for name, _fields in _iter_resolved_product_calls(tool_calls):
        tool = name
    return tool


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
    timeout_s: float | None = None,
) -> tuple[dict | None, str]:
    """Delegates a managed-agent-resolved product selection into the
    EXISTING, unmodified deterministic Product Enrichment / controlled
    Bitrix write-plan pipeline
    (``business_assistant.product_enrichment_bridge.prepare_complete_card``).

    Owns NO pricing/category/media/characteristics/Bitrix-mapping logic
    of its own -- it only translates field names (see
    ``_canonical_fields_and_retail_price``) and calls straight through to
    the SAME capability the legacy CALL_PRODUCT_ENRICHMENT decision
    already uses. Never mutates Bitrix (``prepare_complete_card`` itself
    is read-only -- see its own docstring).

    Returns ``(result, reason)``. ``result`` is the ``prepare_complete_
    card`` dict on success and ``reason`` is ``""``. On failure ``result``
    is ``None`` (never raises) and ``reason`` is one of a small, static,
    non-secret set of codes a caller/operator can act on:
    - ``"missing_title_or_sku"``: the resolved fields lack a title/sku the
      existing pipeline requires (upstream data problem, not a bug here);
    - ``"timeout"``: the existing pipeline did not finish within
      ``timeout_s`` (real research/media-fetch network I/O can be slow in
      a real deployment; see ``DEFAULT_DELEGATION_TIMEOUT_S``);
    - ``f"exception:{type(exc).__name__}"``: the existing pipeline itself
      raised -- production defect closure #2's own hypothesis (F): this
      is exactly the case the prior version of this function swallowed
      with no trace at all. Callers must fall back to an EXPLICIT,
      honest controlled-preparation-failure response, never silently
      re-present the raw tool/model output as if it were the completed
      card (see ``_controlled_preparation_failure_text``)."""
    from business_assistant.product_enrichment_bridge import prepare_complete_card

    product_fields, retail_price = _canonical_fields_and_retail_price(raw_fields)
    if not product_fields.get("title") or not product_fields.get("sku"):
        return None, "missing_title_or_sku"
    coro = prepare_complete_card(
        tenant_id=tenant_id,
        product_fields=product_fields,
        retail_price=retail_price,
        bitrix_bridge=bitrix_bridge,
        tool_gateway=tool_gateway,
        media_fetcher=media_fetcher,
        cache=enrichment_cache,
    )
    try:
        if timeout_s is not None:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        else:
            result = await coro
        return result, ""
    except asyncio.TimeoutError:
        logger.warning(
            "managed_agent_poc: delegation into product_enrichment_bridge.prepare_complete_card "
            "timed out after %.1fs for tenant=%s -- falling back to an explicit controlled "
            "preparation failure (never the raw managed-agent tool output)",
            float(timeout_s or 0.0),
            _safe_path_component(tenant_id),
        )
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 -- an optional enrichment delegation must never crash a turn
        logger.warning(
            "managed_agent_poc: delegation into product_enrichment_bridge.prepare_complete_card "
            "failed for tenant=%s with %s -- falling back to an explicit controlled preparation "
            "failure (never the raw managed-agent tool output)",
            _safe_path_component(tenant_id),
            type(exc).__name__,
            exc_info=True,
        )
        return None, f"exception:{type(exc).__name__}"


async def _delegate_to_existing_write_plan(
    raw_fields: Mapping,
    *,
    tenant_id: str,
    tool_gateway=None,
    bitrix_bridge=None,
    media_fetcher=None,
    enrichment_cache=None,
    timeout_s: float | None = None,
) -> tuple[dict | None, str]:
    """Production defect closure #4: PREPARE_PRODUCT (``select_product``)
    and SHOW_WRITE_PLAN (``explain_bitrix_write_plan``) are two DISTINCT
    semantic actions and must not collapse into the same response.

    Real Railway evidence after #81: for a SHOW_WRITE_PLAN follow-up turn,
    ``MANAGED_PRODUCT_SELECTED tool=explain_bitrix_write_plan`` and
    ``PRODUCT_PREPARATION_DELEGATION_SUCCEEDED`` both fired, yet the
    user-visible response was still the generic prepared-card preview.
    Root cause: both tools were delegated into the exact SAME function
    (``_delegate_to_existing_product_preparation`` ->
    ``product_enrichment_bridge.prepare_complete_card`` ->
    ``format_combined_preview_text``) -- there was no dispatch anywhere
    in this module distinguishing which of the two semantic actions the
    model actually chose, even though that decision is already fully
    observable as ``selected_tool`` (the ``ManagedAgentPOC.run_turn()``
    tool-call shape both #79/#80/#81 already log/consume -- see
    ``maybe_respond_via_managed_agent``'s own ``selected_tool`` local).

    This function instead delegates into the EXISTING, unmodified
    deterministic controlled Bitrix write-plan RENDERER
    (``business_assistant.product_enrichment_bridge.format_write_plan_
    text`` -- the SAME one ``WorkflowPandaConversationGateway.
    _explain_bitrix_write_plan`` already uses for the legacy
    conversational path) instead of ``prepare_complete_card``'s own
    combined full-card preview text. Owns NO pricing/category/media/
    characteristics/Bitrix-mapping/SIMPLE_PRODUCT logic of its own --
    only calls straight through to the same EXISTING functions
    ``_invoke_product_enrichment``/``_explain_bitrix_write_plan`` already
    call: ``run_enrichment``, ``build_enriched_write_request``,
    ``prepare_single_product_write``, ``format_write_plan_text``.

    Product Enrichment is NOT unnecessarily re-run: ``run_enrichment``
    is called with the SAME ``enrichment_cache`` instance the caller's
    earlier ``select_product``/PREPARE_PRODUCT turn already populated
    (``product_enrichment.cache.EnrichmentCache``, keyed by
    ``(tenant_id, identity_key)`` -- see ``product_enrichment.
    orchestrator.enrich_product``'s own cache-hit branch) -- a product
    already enriched earlier in this SAME conversation/process resolves
    from that cache (``cache_hit=True``), so no additional research/
    media-fetch network calls happen; only the (cheap, deterministic,
    no-network) write-request build + read-only section/preview lookup +
    text rendering run again. When no prior enrichment exists yet (e.g.
    SHOW_WRITE_PLAN is the very first turn), this legitimately runs
    enrichment once -- exactly the same as ``select_product`` already
    does today.

    Identical ``(result, reason)`` contract to ``_delegate_to_existing_
    product_preparation`` above (see its own docstring for the meaning
    of each ``reason`` code) so the caller's success/failure handling is
    fully shared between the two semantic actions."""
    from business_assistant.controlled_bitrix_write import prepare_single_product_write
    from business_assistant.product_enrichment_bridge import (
        build_enriched_write_request,
        format_write_plan_text,
        run_enrichment,
        serialize_characteristic_status,
    )
    from product_enrichment.preview import enrichment_preview_dict

    product_fields, retail_price = _canonical_fields_and_retail_price(raw_fields)
    if not product_fields.get("title") or not product_fields.get("sku"):
        return None, "missing_title_or_sku"

    async def _build_and_render() -> dict:
        enrichment = await run_enrichment(
            tenant_id=tenant_id,
            product_fields=product_fields,
            tool_gateway=tool_gateway,
            media_fetcher=media_fetcher,
            cache=enrichment_cache,
        )
        write_request = build_enriched_write_request(
            product_fields, tenant_id=tenant_id, retail_price=retail_price, enrichment=enrichment
        )
        write_preview: dict = {}
        if bitrix_bridge is not None:
            # Mirrors ``_explain_bitrix_write_plan``'s own reasoning
            # exactly: always attempt the EXISTING, read-only
            # ``prepare_single_product_write`` preview -- even when
            # ``retail_price`` is still unknown -- so category/section
            # resolution and the EAN echo are never hidden just because
            # the price is not yet known.
            try:
                write_preview = prepare_single_product_write(
                    bitrix_bridge, tenant_id=tenant_id, request=write_request
                )
            except Exception:  # noqa: BLE001 -- a read-only explanation must never fail on the preview call
                write_preview = {}
        text = format_write_plan_text(
            write_request=write_request,
            write_preview=write_preview,
            characteristic_status=serialize_characteristic_status(enrichment),
            enrichment_preview=enrichment_preview_dict(enrichment),
        )
        return {"text": text, "write_preview": write_preview}

    try:
        if timeout_s is not None:
            result = await asyncio.wait_for(_build_and_render(), timeout=timeout_s)
        else:
            result = await _build_and_render()
        return result, ""
    except asyncio.TimeoutError:
        logger.warning(
            "managed_agent_poc: SHOW_WRITE_PLAN delegation into product_enrichment_bridge timed out "
            "after %.1fs for tenant=%s -- falling back to an explicit controlled preparation failure "
            "(never the raw managed-agent tool output)",
            float(timeout_s or 0.0),
            _safe_path_component(tenant_id),
        )
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 -- an optional write-plan delegation must never crash a turn
        logger.warning(
            "managed_agent_poc: SHOW_WRITE_PLAN delegation into product_enrichment_bridge failed for "
            "tenant=%s with %s -- falling back to an explicit controlled preparation failure (never the "
            "raw managed-agent tool output)",
            _safe_path_component(tenant_id),
            type(exc).__name__,
            exc_info=True,
        )
        return None, f"exception:{type(exc).__name__}"


def _controlled_preparation_failure_text(raw_fields: Mapping, *, reason: str) -> str:
    """Production defect closure #2's central contract fix: renders an
    EXPLICIT, honest "preparation did not complete" message instead of
    ever silently presenting the Managed Agent's raw, un-enriched tool/
    model projection as though it were the finished product card (the
    exact defect real production exposed after #79: a raw-row card with
    an unresolved retail price and near-empty media/characteristics,
    presented as if it were complete, even falsely claiming ``"Нет таких
    полей"`` for the read-only Bitrix mapping).

    Only echoes fields ALREADY confirmed by the raw row projection itself
    (never invented, never a fabricated price/category ID/characteristic)
    and is explicit that retail price, media, characteristics, and the
    Bitrix/Aspro mapping were NOT produced in this response."""
    name = str(raw_fields.get("name") or raw_fields.get("title") or "")
    sku = str(raw_fields.get("sku") or "")
    ean = str(raw_fields.get("ean") or raw_fields.get("barcode") or "")
    brand = str(raw_fields.get("brand") or "")
    category = str(raw_fields.get("category") or "")
    purchase_price = str(raw_fields.get("purchase_price") or "")

    lines = [
        "ПОДГОТОВКА ПОЛНОЙ КАРТОЧКИ ТОВАРА НЕ ЗАВЕРШЕНА.",
        (
            "Существующий конвейер Product Enrichment/подготовки товара не "
            f"смог выполниться (причина: {reason or 'unknown'}). Ниже -- "
            "только то, что уже подтверждено в прайсе; это НЕ подготовленная "
            "карточка."
        ),
    ]
    if name:
        lines.append(f"Товар: {name}")
    if sku:
        lines.append(f"Артикул/SKU: {sku}")
    if ean:
        lines.append(f"EAN: {ean}")
    if brand:
        lines.append(f"Бренд: {brand}")
    if category:
        lines.append(f"Категория (из прайса, раздел Bitrix НЕ определён): {category}")
    if purchase_price:
        lines.append(f"Закупочная цена: {purchase_price}")
    lines.append(
        "Розничная цена, основное изображение, галерея, подробное описание, "
        "характеристики и итоговый план записи в Bitrix/Aspro в этом ответе "
        "НЕ подготовлены -- повторите запрос позже или обратитесь к "
        "администратору, если это повторяется."
    )
    lines.append("В Bitrix/Aspro ничего не записано и не опубликовано.")
    return "\n".join(lines)


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

    # Production defect closure #2, step 1 (prove root cause before
    # editing): wrapped so an unexpected bug in selection extraction
    # itself is also OBSERVABLE (EVENT_PRODUCT_PREPARATION_REQUIRED_BUT_
    # NOT_REACHED) rather than silently producing the exact "raw tool/
    # model output presented as complete" defect this closure exists to
    # eliminate.
    try:
        raw_fields = _selected_product_raw_fields(turn_result.tool_calls)
    except Exception as exc:  # noqa: BLE001 -- selection extraction must never crash a turn
        _log_event(
            EVENT_PRODUCT_PREPARATION_REQUIRED_BUT_NOT_REACHED,
            tenant_id=tenant_id,
            level=logging.WARNING,
            stage="selection_extraction",
            error=type(exc).__name__,
        )
        raw_fields = None

    if raw_fields is not None:
        _log_event(
            EVENT_MANAGED_PRODUCT_SELECTED,
            tenant_id=tenant_id,
            tool=selected_tool,
            has_sku=bool(raw_fields.get("sku")),
            has_name=bool(raw_fields.get("name") or raw_fields.get("title")),
            has_retail_price=bool(raw_fields.get("retail_price") or raw_fields.get("price")),
        )
        # Production defect closure #4: PREPARE_PRODUCT and SHOW_WRITE_PLAN
        # are two distinct semantic actions -- dispatch on the ALREADY
        # observable resolved-product tool (no phrase/regex/stem routing;
        # this is the exact same tool-name signal #79/#80/#81 already log)
        # instead of always delegating into ``prepare_complete_card``'s
        # own full-card preview text.
        #
        # Production defect closure #5 (competing-path defect found after
        # #82 shipped): this MUST be the tool that actually produced
        # ``raw_fields`` above (``_resolved_product_tool`` -- the exact
        # same scan ``_selected_product_raw_fields`` used), never
        # ``selected_tool``/``tool_calls[0]``. A real turn can contain
        # MORE than one tool call (the Agents SDK's own agentic loop --
        # already proven live for other shapes in
        # ``ManagedAgentRealSubprocessContractTests``); when the model's
        # FIRST call is a distinct/failed attempt (e.g.
        # ``explain_bitrix_write_plan`` returning ``NO_PRODUCT_SELECTED``
        # on a brand-new conversation) and only a LATER call actually
        # resolves the product (``select_product`` -> ``SELECTED``),
        # ``tool_calls[0]`` silently disagreed with the resolution this
        # dispatch must honor -- wrongly diverting a genuine PREPARE_
        # PRODUCT/full-card turn into the SHOW_WRITE_PLAN renderer.
        is_write_plan_request = _resolved_product_tool(turn_result.tool_calls) == "explain_bitrix_write_plan"
        delegate = _delegate_to_existing_write_plan if is_write_plan_request else _delegate_to_existing_product_preparation
        _log_event(EVENT_PRODUCT_PREPARATION_DELEGATION_STARTED, tenant_id=tenant_id)
        try:
            delegated, reason = await delegate(
                raw_fields,
                tenant_id=tenant_id,
                tool_gateway=tool_gateway,
                bitrix_bridge=bitrix_bridge,
                media_fetcher=media_fetcher,
                enrichment_cache=enrichment_cache,
                timeout_s=DEFAULT_DELEGATION_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 -- delegation must never crash a turn
            _log_event(
                EVENT_PRODUCT_PREPARATION_REQUIRED_BUT_NOT_REACHED,
                tenant_id=tenant_id,
                level=logging.WARNING,
                stage="delegation_call",
                error=type(exc).__name__,
            )
            delegated, reason = None, f"unexpected:{type(exc).__name__}"

        if delegated is not None:
            _log_event(EVENT_PRODUCT_PREPARATION_DELEGATION_SUCCEEDED, tenant_id=tenant_id)
            response_text = str(delegated.get("text") or "")
            metadata["delegated_to"] = (
                "product_enrichment_bridge.format_write_plan_text"
                if is_write_plan_request
                else "product_enrichment_bridge.prepare_complete_card"
            )
            metadata["preparation_status"] = "PREPARED"
            write_preview = delegated.get("write_preview") or {}
            if write_preview:
                metadata["bitrix_write_preview"] = write_preview
        else:
            _log_event(
                EVENT_PRODUCT_PREPARATION_DELEGATION_FAILED,
                tenant_id=tenant_id,
                level=logging.WARNING,
                reason=reason,
            )
            # Step 2 contract fix (the major error #79's production run
            # exposed): a resolved product REQUIRES complete
            # deterministic preparation. Never silently fall back to the
            # raw, un-enriched Managed Agent tool/model projection as if
            # it were the finished card -- report an explicit, honest
            # controlled preparation failure instead.
            response_text = _controlled_preparation_failure_text(raw_fields, reason=reason)
            metadata["preparation_status"] = "FAILED"
            metadata["preparation_failure_reason"] = reason

    return {"text": response_text, "metadata": metadata}
