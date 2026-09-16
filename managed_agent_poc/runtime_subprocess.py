#!/usr/bin/env python3
"""ISOLATED PROCESS ONLY — never imported by, or run inside, Panda's main
process/tests.

Reads exactly one JSON request object from stdin, runs ONE managed-agent
turn (OpenAI Agents SDK: ``Agent`` + ``Runner`` + ``function_tool`` +
``SQLiteSession``) against the 3 safe, read-only Panda tools defined
below, and writes exactly one JSON response object to stdout. Launched
by ``managed_agent_poc.adapter.ManagedAgentPOC`` as a subprocess; see
``managed_agent_poc/__init__.py`` for exactly why this needs to be a
separate process (the ``agents`` import-name collision with Panda's own
``agents/`` package) rather than an in-process call.

SEMANTIC TOOL SELECTION, NOT A LANGUAGE ROUTER: the 3 tools below are
plain, typed Python functions. The Agents SDK derives each tool's JSON
schema and natural-language description directly from its type hints
and docstring (see the SDK's ``function_schema`` derivation) -- the
model reads those descriptions and decides which tool fits the user's
message and what typed arguments to pass. There is no
``is_explicit_*``/``_wants_*`` predicate, no regex, no stem dictionary,
and no hardcoded verb/noun list anywhere in this file: every
Russian/English paraphrase the model can understand is handled by the
SAME 3 tools without any code change here.

Business rules stay in Panda: the tools below only ever READ an
already-ingested spreadsheet dataset (via the existing, UNMODIFIED
``data_intel`` store/service public API) and return a structured
preview. None of them writes to Bitrix/Aspro, publishes anything, or
mutates any Panda business record -- there is deliberately no 4th tool
that could.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Step 1 (BEFORE any other import): make the isolated OpenAI Agents SDK
# resolve first, so ``import agents`` below can NEVER resolve to Panda's own
# ``agents/`` package even if this script's own directory or an inherited
# PYTHONPATH happens to include the repo root.
# ---------------------------------------------------------------------------
_PKGS_DIR = os.environ.get("PANDA_MANAGED_AGENT_POC_PKGS_DIR") or "/tmp/panda_managed_agent_poc_pkgs"
sys.path.insert(0, _PKGS_DIR)

from agents import Agent, ModelSettings, RunConfig, RunContextWrapper, Runner, SQLiteSession, function_tool  # noqa: E402
from agents.testing import ScriptedModel, assistant_message, function_call  # noqa: E402
from openai.types.shared import Reasoning  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

# ---------------------------------------------------------------------------
# Step 2: only now append the repo root, so the existing, UNMODIFIED
# ``data_intel``/``artifacts`` packages become importable too. Appended, not
# inserted -- ``agents`` above is already resolved/cached by this point, so
# this can never shadow the SDK regardless of append order.
# ---------------------------------------------------------------------------
_REPO_ROOT = os.environ.get("PANDA_MANAGED_AGENT_POC_REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_REPO_ROOT)

from data_intel.contracts import (  # noqa: E402
    ROLE_ARTICLE,
    ROLE_BARCODE,
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_EAN,
    ROLE_PRICE,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_SKU,
)
from data_intel.service import DataIntelligenceService  # noqa: E402
from data_intel.store import SqliteDatasetStore  # noqa: E402

from managed_agent_poc.state_store import ConversationStateStore, PersistedState  # noqa: E402

_IDENTIFIER_ROLES = (ROLE_SKU, ROLE_ARTICLE, ROLE_PRODUCT_NAME, ROLE_EAN, ROLE_BARCODE, ROLE_CATEGORY, ROLE_BRAND)
_PRICE_ROLES = (ROLE_SELLING_PRICE, ROLE_PRICE, ROLE_PURCHASE_PRICE)


# ---------------------------------------------------------------------------
# Structured, code-owned conversation state (Panda's business state stays in
# Panda's code, never in the model's free-text reasoning): which dataset this
# conversation is working with, and which product identifiers have already
# been shown, so a "give me another one" turn deterministically excludes them
# without the model needing to track or repeat SKUs itself.
# ---------------------------------------------------------------------------
@dataclass
class ConversationState:
    tenant_id: str
    dataset_id: str = ""
    shown_identifiers: list = field(default_factory=list)
    current_identifier: str = ""
    svc: object = None
    # Business-task-ownership/workset-continuation defect closure (PR #90
    # correction): set by ``apply_scoped_price_rules`` below on a
    # successful compound scoped-rule execution -- the freshly generated
    # workbook bytes (base64) + filename for the dataset it just produced.
    # Deliberately NEVER included in that tool's own JSON return value
    # (which IS model-visible and would otherwise waste tokens on/expose
    # raw file bytes to the model) -- only ``main()`` reads this AFTER the
    # run, exactly like ``dataset_id``/``shown_identifiers`` above, so
    # ``managed_agent_poc.panda_bridge``/``business_assistant.
    # conversation_gateway`` can bridge the derived dataset into the SAME
    # shared ``data_intel`` store the legacy engine reads.
    scoped_rules_workbook: dict | None = None
    # Not model-visible, not tool-visible -- only ``main()`` reads this after
    # the run to persist the (possibly mutated) fields above. See
    # ``managed_agent_poc.state_store`` for why this exists at all.


def _row_identifying_value(row: dict, table) -> str:
    for col in table.columns:
        if col.semantic_role == ROLE_SKU:
            value = str(row.get(col.source_name) or "").strip()
            if value:
                return value
    for role in _IDENTIFIER_ROLES:
        for col in table.columns:
            if col.semantic_role == role:
                value = str(row.get(col.source_name) or "").strip()
                if value:
                    return value
    return ""


def _row_product_fields(row: dict, table) -> dict:
    fields: dict = {}
    role_to_key = {
        ROLE_PRODUCT_NAME: "name",
        ROLE_SKU: "sku",
        ROLE_EAN: "ean",
        ROLE_BARCODE: "barcode",
        ROLE_CATEGORY: "category",
        ROLE_BRAND: "brand",
        ROLE_PURCHASE_PRICE: "purchase_price",
        ROLE_SELLING_PRICE: "retail_price",
        ROLE_PRICE: "price",
    }
    for col in table.columns:
        key = role_to_key.get(col.semantic_role)
        if key and key not in fields:
            value = str(row.get(col.source_name) or "").strip()
            if value:
                fields[key] = value
    # Production defect closure #3 (real production: MANAGED_PRODUCT_
    # SELECTED logged has_sku=False/has_name=True for a row whose
    # identifier column IS present and non-empty): mirrors the EXISTING,
    # already-proven ``data_intel.service._row_lookup_result`` contract
    # EXACTLY -- ``"sku": _role_value(row, table, ROLE_SKU) or
    # _role_value(row, table, ROLE_ARTICLE)``. ``ROLE_ARTICLE`` (e.g. a
    # column literally header "Артикул" -- see
    # ``data_intel.mapping``'s own role classifier) is a DISTINCT
    # semantic role from ``ROLE_SKU``, and a real supplier price list's
    # identifier column is frequently classified as ``ROLE_ARTICLE``, not
    # ``ROLE_SKU`` -- this tool's own row-projection previously had no
    # mapping for that role at all, so the value was silently dropped
    # before it ever reached ``select_product``'s returned dict. Never
    # overwrites an already-resolved ``sku`` (``ROLE_SKU`` always wins
    # when a table has both roles, exactly like the existing contract).
    if "sku" not in fields:
        for col in table.columns:
            if col.semantic_role == ROLE_ARTICLE:
                value = str(row.get(col.source_name) or "").strip()
                if value:
                    fields["sku"] = value
                    break
    return fields


def _load_table(svc: DataIntelligenceService, dataset_id: str, tenant_id: str):
    desc = svc.store.get_dataset(dataset_id, tenant_id=tenant_id)
    if desc is None or not desc.tables:
        raise ValueError(f"dataset {dataset_id!r} not found for this conversation")
    table = desc.tables[0]
    rows = svc.store.get_rows(dataset_id, tenant_id=tenant_id, table_id=table.table_id)
    return table, rows


# ---------------------------------------------------------------------------
# THE 3 SAFE, READ-ONLY TOOLS.
#
# Each tool takes ONLY structured, typed arguments -- the model is
# responsible for turning free-text (any language, any paraphrase) into
# these arguments; the Python code below never parses free text at all.
# ---------------------------------------------------------------------------


@function_tool
def analyze_spreadsheet(ctx: RunContextWrapper[ConversationState]) -> dict:
    """Analyze the OVERALL structure and pricing of the already-uploaded
    spreadsheet/price-list for this conversation: row count, column
    count, and price range statistics (min/max/average) across ALL rows.

    Use this when the user is asking about the spreadsheet or price list
    as a WHOLE -- e.g. how many rows/products it has, its average/
    minimum/maximum price, or a general summary -- and is NOT asking to
    select, prepare, or review any single specific product.
    """
    state = ctx.context
    table, rows = _load_table(state.svc, state.dataset_id, state.tenant_id)
    result: dict = {
        "row_count": len(rows),
        "column_count": len(table.columns),
    }
    price_col = next((c for c in table.columns if c.semantic_role in _PRICE_ROLES), None)
    if price_col is not None:
        values = []
        for row in rows:
            raw = str(row.get(price_col.source_name) or "").strip()
            try:
                values.append(float(raw))
            except ValueError:
                continue
        if values:
            result["price_column"] = price_col.source_name
            result["price_min"] = min(values)
            result["price_max"] = max(values)
            result["price_avg"] = sum(values) / len(values)
    return result


@function_tool
def select_product(
    ctx: RunContextWrapper[ConversationState],
    identifier: str | None = None,
) -> dict:
    """Select and preview exactly ONE product row from the already-uploaded
    spreadsheet dataset for this conversation. Read-only: this NEVER
    writes or publishes anything to Bitrix/Aspro.

    Args:
        identifier: A SKU, EAN, barcode, brand, category, or product-name
            substring, when the user names or refers back to a SPECIFIC
            product (e.g. "show me the LG one again", "go back to
            TV-A-1001"). Leave this as ``None`` when the user wants ANY
            unspecified single product -- including a first-time "prepare
            one product"/"pick one item" request AND a later "a
            different one"/"another product" request. In both of those
            ``None`` cases this tool automatically avoids re-selecting a
            product already shown earlier in this same conversation.

    Returns:
        The resolved product's fields (name, sku, ean, category, brand,
        purchase_price, retail_price) as a read-only preview.
    """
    state = ctx.context
    table, rows = _load_table(state.svc, state.dataset_id, state.tenant_id)

    chosen = None
    matched_by = ""
    if identifier:
        needle = identifier.strip().casefold()
        for row in rows:
            for role in _IDENTIFIER_ROLES:
                for col in table.columns:
                    if col.semantic_role != role:
                        continue
                    value = str(row.get(col.source_name) or "").strip()
                    if value and (needle == value.casefold() or needle in value.casefold()):
                        chosen = row
                        matched_by = col.source_name
                        break
                if chosen is not None:
                    break
            if chosen is not None:
                break

    if chosen is None:
        for row in rows:
            row_ident = _row_identifying_value(row, table)
            if row_ident and row_ident in state.shown_identifiers:
                continue
            chosen = row
            matched_by = "next_unspecified"
            break

    if chosen is None:
        return {"status": "NOT_FOUND", "reason": "no matching or remaining unselected product row"}

    row_ident = _row_identifying_value(chosen, table)
    if row_ident and row_ident not in state.shown_identifiers:
        state.shown_identifiers.append(row_ident)
    state.current_identifier = row_ident

    fields = _row_product_fields(chosen, table)
    return {"status": "SELECTED", "matched_by": matched_by, **fields}


@function_tool
def explain_bitrix_write_plan(
    ctx: RunContextWrapper[ConversationState],
    identifier: str | None = None,
) -> dict:
    """Explain exactly what WOULD be written to Bitrix/Aspro for a
    product that has already been selected in this conversation, IF the
    user later confirms a write. This is READ-ONLY -- it never performs
    an actual write or publish action itself, and there is no tool in
    this conversation that does.

    Args:
        identifier: The product's SKU/EAN/name, or leave ``None`` to use
            the product most recently selected in this conversation.

    Use this when the user asks to see, confirm, or review the write
    plan / fields / category that would be sent to Bitrix/Aspro before
    approving anything.
    """
    state = ctx.context
    target = identifier or state.current_identifier
    if not target:
        return {"status": "NO_PRODUCT_SELECTED", "reason": "no product has been selected in this conversation yet"}
    table, rows = _load_table(state.svc, state.dataset_id, state.tenant_id)
    for row in rows:
        row_ident = _row_identifying_value(row, table)
        if row_ident == target or target.casefold() in row_ident.casefold():
            fields = _row_product_fields(row, table)
            return {
                "status": "WRITE_PLAN",
                "would_write": fields,
                "note": "not written -- a separate, explicit write confirmation is required (not offered by this POC)",
            }
    return {"status": "NOT_FOUND", "reason": f"no product matching {target!r}"}


# ---------------------------------------------------------------------------
# 4TH TOOL: compound scoped price-adjustment rules (business-task-
# ownership/workset-continuation defect closure, PR #90 correction).
#
# SAME semantic-tool-selection principle as the 3 tools above: the model
# turns free text (ANY language/paraphrase -- "first three rows +7%, the
# rest +15%", "brand X gets +10%, everything else gets -5%", ...) into
# these TYPED, VALIDATED pydantic arguments; this file never parses free
# text. What is NEW here is that a SINGLE call can now describe MULTIPLE
# different row scopes with MULTIPLE different percent changes in ONE
# deterministic operation -- the 3 read-only tools above only ever
# describe the WHOLE sheet or ONE row. The model interprets language; the
# EXISTING deterministic executors below (``data_intel.nl_ops.
# validate_scoped_price_rules`` + ``data_intel.transform.
# execute_scoped_percent_rules``, wired together by
# ``DataIntelligenceService.execute_scoped_price_rules``) validate the
# structure and do 100% of the arithmetic -- there is no model-generated
# code or arbitrary-expression path anywhere in this tool.
# ---------------------------------------------------------------------------


class ScopedRuleScope(BaseModel):
    """WHICH rows of the already-uploaded spreadsheet a rule applies to."""

    kind: Literal["row_position_range", "text_contains", "price_compare", "remainder"] = Field(
        description=(
            "'row_position_range': rows by their 1-based position in the file's ORIGINAL row "
            "order (e.g. 'the first three rows' -> start_position=1, end_position=3). "
            "'text_contains': rows whose brand/category/name field contains a substring. "
            "'price_compare': rows whose purchase/retail/price field is above/below/at a "
            "threshold. 'remainder': every row not already claimed by an EARLIER rule in this "
            "SAME tool call -- use this for 'the rest'/'everything else'/'остальные'."
        )
    )
    start_position: int | None = Field(
        default=None, description="1-based inclusive start row position (row_position_range only)."
    )
    end_position: int | None = Field(
        default=None,
        description="1-based inclusive end row position (row_position_range only); omit for 'to the end'.",
    )
    text_field: Literal["brand", "category", "name"] | None = Field(
        default=None, description="Which text field to match (text_contains only)."
    )
    contains: str | None = Field(default=None, description="Substring to match (text_contains only).")
    price_field: Literal["retail_price", "purchase_price", "price"] | None = Field(
        default=None, description="Which price field to compare (price_compare only)."
    )
    operator: Literal["gt", "gte", "lt", "lte", "eq"] | None = Field(
        default=None, description="Comparison operator (price_compare only)."
    )
    threshold: float | None = Field(default=None, description="Comparison threshold (price_compare only).")


class ScopedPriceRule(BaseModel):
    """ONE rule: a percent price change applied to exactly ``scope``'s
    matched rows."""

    scope: ScopedRuleScope
    price_field: Literal["retail_price", "purchase_price", "price"] = Field(
        default="retail_price", description="Which price field this rule adjusts."
    )
    percent: float = Field(description="Signed percent change, e.g. 7 for +7% or -15 for -15%.")


def _scoped_rule_to_raw(rule: ScopedPriceRule) -> dict:
    scope = rule.scope
    scope_dict: dict = {"kind": scope.kind}
    if scope.kind == "row_position_range":
        scope_dict["start_position"] = scope.start_position
        scope_dict["end_position"] = scope.end_position
    elif scope.kind == "text_contains":
        scope_dict["text_field"] = scope.text_field
        scope_dict["contains"] = scope.contains
    elif scope.kind == "price_compare":
        scope_dict["price_field"] = scope.price_field
        scope_dict["operator"] = scope.operator
        scope_dict["threshold"] = scope.threshold
    return {"scope": scope_dict, "price_field": rule.price_field, "percent": rule.percent}


@function_tool
def apply_scoped_price_rules(
    ctx: RunContextWrapper[ConversationState],
    rules: list[ScopedPriceRule],
) -> dict:
    """Apply TWO OR MORE different percentage price changes to TWO OR MORE
    different, non-overlapping row scopes of the already-uploaded
    spreadsheet, in ONE deterministic operation, and return a precise
    preview of every affected row (identifier, price field, value before,
    value after). Use this ONLY when the user's message assigns MORE THAN
    ONE distinct percentage change to MORE THAN ONE distinct subset of
    rows in the SAME message -- for example "first three rows get +7%,
    the rest get +15%", "brand A gets a 10% increase, everything else
    gets a 5% discount", or any other combination of row scopes (position
    ranges, text filters, price thresholds, or "the remainder") with
    percentage changes.

    Rules are applied IN ORDER; a later rule's scope only ever considers
    rows not already claimed by an earlier rule in this SAME call, so a
    "remainder" scope is always safe and can never double-apply a change
    to the same row.

    For a request with only ONE change applying to ONE scope (or to the
    whole sheet), do NOT use this tool -- that is handled elsewhere in
    this conversation, before this tool is ever offered.

    This NEVER writes or publishes anything to Bitrix/Aspro.
    """
    state = ctx.context
    raw_rules = [_scoped_rule_to_raw(rule) for rule in rules]
    result = state.svc.execute_scoped_price_rules(state.dataset_id, raw_rules, tenant_id=state.tenant_id)

    if result.get("status") == "OK":
        state.dataset_id = str(result["dataset_id"])
        try:
            wb = state.svc.generate_excel(state.dataset_id, tenant_id=state.tenant_id, kind="data")
            state.scoped_rules_workbook = {
                "content_b64": base64.b64encode(wb["content"]).decode("ascii"),
                "filename": str(wb.get("filename") or "dataset.xlsx"),
            }
        except Exception:  # noqa: BLE001 -- the bridging workbook is best-effort, never blocks the preview
            state.scoped_rules_workbook = None

    # Model-visible output only -- raw workbook bytes never reach the
    # model (see ``ConversationState.scoped_rules_workbook`` above).
    return result


_TOOLS = [analyze_spreadsheet, select_product, explain_bitrix_write_plan, apply_scoped_price_rules]

_INSTRUCTIONS = (
    "You are Panda's data/product assistant for an already-uploaded spreadsheet "
    "(a supplier price list). Decide, from the user's message alone -- in any "
    "language or phrasing -- which ONE of your tools fits their request: "
    "overall spreadsheet analysis, selecting/previewing a single product, "
    "explaining what would be written to Bitrix/Aspro for an already-selected "
    "product, or applying two or more different percentage price changes to "
    "two or more different row scopes in the same request (e.g. 'first three "
    "rows +7%, the rest +15%'). Never claim to write, publish, or confirm "
    "anything to Bitrix/Aspro yourself -- you have no tool that does that. "
    "After calling a tool, answer the user in the SAME language they used, "
    "summarizing the tool's structured result."
)


def _build_scripted_model(plan: list[dict]) -> ScriptedModel:
    steps = []
    for idx, item in enumerate(plan):
        if "call_tool" in item:
            steps.append([function_call(item["call_tool"], item.get("arguments") or {}, call_id=f"call_{idx}")])
        elif "final_output" in item:
            steps.append([assistant_message(item["final_output"])])
        else:
            raise ValueError(f"unrecognized scripted plan item: {item!r}")
    return ScriptedModel(steps)


def _extract_tool_calls(result) -> list[dict]:
    """Pairs each ``ToolCallItem`` (carries the tool name + call_id) with
    its matching ``ToolCallOutputItem`` (carries call_id + output) from
    ``RunResult.new_items``, so the structured response can report
    exactly which tool the model chose and what it returned -- proof of
    SEMANTIC tool selection, not just a final text answer."""
    names_by_call_id: dict[str, str] = {}
    for item in getattr(result, "new_items", []) or []:
        if type(item).__name__ == "ToolCallItem":
            raw = getattr(item, "raw_item", None)
            call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None)
            name = getattr(raw, "name", None)
            if call_id and name:
                names_by_call_id[call_id] = name

    calls: list[dict] = []
    for item in getattr(result, "new_items", []) or []:
        if type(item).__name__ == "ToolCallOutputItem":
            raw = getattr(item, "raw_item", None)
            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
            calls.append(
                {
                    "tool": names_by_call_id.get(call_id, ""),
                    "output": getattr(item, "output", None),
                }
            )
    return calls


def main() -> int:
    request = json.loads(sys.stdin.read())

    tenant_id = str(request.get("tenant_id") or "")
    dataset_store_path = str(request["dataset_store_path"])
    session_db_path = str(request["session_db_path"])
    state_store_path = str(request.get("state_store_path") or session_db_path)
    conversation_id = str(request.get("conversation_id") or "")

    store = SqliteDatasetStore(dataset_store_path)
    svc = DataIntelligenceService(store)

    # Durable, Panda-owned business state (dataset_id / shown-product
    # history / current selection) -- NOT the SDK's own Session, which only
    # persists conversation transcript. See ``managed_agent_poc.state_store``.
    state_store = ConversationStateStore(state_store_path)
    persisted = state_store.load(tenant_id=tenant_id, conversation_id=conversation_id)

    dataset_id = str(request.get("dataset_id") or "") or persisted.dataset_id
    artifact_bytes_path = request.get("artifact_bytes_path") or ""
    if artifact_bytes_path:
        # Mirrors production: attachment ingestion is a deterministic,
        # pre-conversational step (ArtifactService + DataIntelligenceService.
        # ingest()) -- never a tool the model itself decides to call.
        with open(artifact_bytes_path, "rb") as fh:
            content = fh.read()
        ingested = svc.ingest(
            content,
            filename=str(request.get("artifact_filename") or "upload.xlsx"),
            tenant_id=tenant_id,
            enqueue_large=False,
        )
        dataset_id = str(ingested["dataset_id"])

    state = ConversationState(
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        shown_identifiers=list(persisted.shown_identifiers),
        current_identifier=persisted.current_identifier,
        svc=svc,
    )

    session = SQLiteSession(conversation_id or "managed-agent-poc", session_db_path)

    scripted_plan = request.get("test_scripted_plan")
    run_config_kwargs: dict = {"tracing_disabled": True}
    if scripted_plan:
        run_config_kwargs["model"] = _build_scripted_model(scripted_plan)
        agent = Agent(name="Panda Data Assistant (POC)", instructions=_INSTRUCTIONS, tools=_TOOLS)
    else:
        # Current, SDK-documented, cost-efficient default for a tool-selection
        # workload (see the SDK's own Models guide -- "gpt-5.6-luna ... for
        # efficient, high-volume agent workloads"), overridable via the SAME
        # OPENAI_MODEL env var Panda's existing adapter
        # (agents/openai_agent.py) already uses. low/none reasoning +
        # low verbosity keeps this live evaluation's spend minimal, per the
        # cost-control requirement -- this is a semantic tool-selection task,
        # not deep multi-step reasoning.
        model_name = os.environ.get("OPENAI_MODEL") or "gpt-5.6-luna"
        agent = Agent(
            name="Panda Data Assistant (POC)",
            instructions=_INSTRUCTIONS,
            tools=_TOOLS,
            model=model_name,
            model_settings=ModelSettings(reasoning=Reasoning(effort="none"), verbosity="low"),
        )

    result = Runner.run_sync(
        agent,
        str(request.get("text") or ""),
        context=state,
        session=session,
        run_config=RunConfig(**run_config_kwargs),
    )

    state_store.save(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        state=PersistedState(
            dataset_id=state.dataset_id,
            shown_identifiers=state.shown_identifiers,
            current_identifier=state.current_identifier,
        ),
    )

    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    usage_payload = None
    if usage is not None:
        usage_payload = {
            "requests": getattr(usage, "requests", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }

    response = {
        "status": "COMPLETED",
        "final_output": result.final_output,
        "tool_calls": _extract_tool_calls(result),
        "dataset_id": state.dataset_id,
        "shown_identifiers": state.shown_identifiers,
        "current_identifier": state.current_identifier,
        "scoped_rules_workbook": state.scoped_rules_workbook,
        "model": model_name if not scripted_plan else "SCRIPTED_MODEL_TEST_DOUBLE",
        "usage": usage_payload,
    }
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 -- boundary process: report, never traceback-crash silently
        sys.stdout.write(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)
