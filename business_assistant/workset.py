"""PANDA — Canonical Workset (single business-data ownership).

The ONE authoritative business-data context for a conversation's Excel/
product work: "which dataset are we working on, and which scope of it
(a single product, a filtered subset, or the whole thing) is currently
in focus." Introduced to close the split-ownership defect where an
uploaded spreadsheet was tracked independently by:

- ``business_assistant.action_continuation.ActiveTask.parameters
  ["dataset_id"]`` (the legacy conversational path's own continuation
  key, overwritten unconditionally by every successful FAMILY_EXCEL tool
  call — see ``conversation_gateway.WorkflowPandaConversationGateway.
  _invoke_tool``'s pre-Workset history);
- the sanctioned managed-agent integration boundary's own private,
  per-conversation SQLite dataset store (see
  ``business_assistant.conversation_gateway``'s own module docstring for
  that integration's exact durable-store paths) — a *different* physical
  database from the shared ``data_intel`` store the legacy path reads, so
  a dataset_id resolved on that path never existed as far as the legacy
  path/tool was concerned.

Neither of those stores is replaced or synchronized by this module —
that FALSE FIX (bridging a generated/derived workbook between two
private stores) was explicitly rejected; see the PR history this module
closes. Instead, this module gives ``ActiveTask`` exactly ONE typed,
serializable business-data context that both the legacy conversational
path and the managed-agent integration boundary update through the SAME
two functions (``start_new_source``/``apply_tool_result``, and
``select_single``), so there is only ever one place a dataset_id/scope
transition is decided.

PERSISTENCE: a plain dict under the EXISTING, already-durable
``ActiveTask.parameters["workset"]`` key — no new store, no new SQLite
table, no new schema. ``ActiveTask`` already round-trips through
``business_assistant.action_continuation.SqliteActiveTaskStore`` as a
JSON blob (``parameters_json``), so a Workset embedded in ``parameters``
survives a process restart exactly like every other FAMILY_EXCEL
continuation field (``bitrix_product_fields``, ...) already does.

COMPATIBILITY MIRROR: ``task.parameters["dataset_id"]`` — the flat key
``business_assistant.action_continuation.resolve_action_turn``'s EXISTING
FAMILY_EXCEL branch already reads/writes for its own ``has_dataset``/
continuation gate — is kept as a MIRROR of ``workset.current_dataset_id``
by ``apply_to_task`` below, the ONE function that ever writes either of
them. This lets that existing resolver (a large, delicate, heavily
tested function) keep working completely unchanged while the Workset
itself becomes the single authoritative source of truth underneath it.

SCOPE VALUES:
    NONE          — no dataset/product currently in focus.
    SINGLE        — exactly one product/row is the current focus
                     (``selected_identifiers`` has exactly one entry).
    FILTERED_SET  — a proper subset of the dataset's rows is in focus
                     (e.g. a filter reduced the row count).
    FULL_DATASET  — the whole current dataset is in focus (a fresh
                     upload, a whole-table analysis, or a transform that
                     touched every row without removing any).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Mapping

SCOPE_NONE = "NONE"
SCOPE_SINGLE = "SINGLE"
SCOPE_FILTERED_SET = "FILTERED_SET"
SCOPE_FULL_DATASET = "FULL_DATASET"

VALID_SCOPES = (SCOPE_NONE, SCOPE_SINGLE, SCOPE_FILTERED_SET, SCOPE_FULL_DATASET)

# Tool-response ``status`` codes that ``apply_tool_result`` already knows
# how to interpret — all of them EXISTING ``data_intel.service.
# DataIntelligenceService.execute_nl_request``/``_row_lookup_result``/
# ``_analyze_only_summary`` return values, never a new status invented by
# this module.
_STATUS_ROW_FOUND = "ROW_FOUND"
_STATUS_OK = "OK"
_STATUS_ANALYZED = "ANALYZED"


@dataclass(frozen=True)
class Workset:
    workset_id: str
    tenant_id: str
    owner_id: str
    conversation_id: str
    source_dataset_id: str = ""
    current_dataset_id: str = ""
    scope: str = SCOPE_NONE
    selected_identifiers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "workset_id": self.workset_id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "conversation_id": self.conversation_id,
            "source_dataset_id": self.source_dataset_id,
            "current_dataset_id": self.current_dataset_id,
            "scope": self.scope,
            "selected_identifiers": list(self.selected_identifiers),
        }

    @staticmethod
    def from_dict(data: Mapping) -> "Workset":
        scope = str(data.get("scope") or SCOPE_NONE)
        if scope not in VALID_SCOPES:
            scope = SCOPE_NONE
        return Workset(
            workset_id=str(data.get("workset_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            owner_id=str(data.get("owner_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            source_dataset_id=str(data.get("source_dataset_id") or ""),
            current_dataset_id=str(data.get("current_dataset_id") or ""),
            scope=scope,
            selected_identifiers=tuple(str(x) for x in (data.get("selected_identifiers") or ())),
        )


def get_workset(task) -> Workset | None:
    """Reads the canonical Workset off an ``ActiveTask``, or ``None`` when
    this task has never had one (a brand-new task, or a task predating
    this module — self-healing: callers must treat that exactly like "no
    workset yet", never crash)."""
    if task is None:
        return None
    raw = task.parameters.get("workset") if isinstance(task.parameters, Mapping) else None
    if not isinstance(raw, Mapping) or not raw.get("workset_id"):
        return None
    return Workset.from_dict(raw)


def apply_to_task(task, workset: Workset) -> None:
    """The ONE function that ever writes ``parameters['workset']`` AND its
    compatibility mirror ``parameters['dataset_id']`` — together, so the
    two can never disagree."""
    task.parameters["workset"] = workset.to_dict()
    task.parameters["dataset_id"] = workset.current_dataset_id


def start_new_source(
    existing: Workset | None,
    *,
    tenant_id: str,
    owner_id: str,
    conversation_id: str,
    dataset_id: str,
) -> Workset:
    """A fresh spreadsheet attachment always (re)starts the canonical
    SOURCE — the CRITICAL invariant this module exists to enforce: from
    this point on, ``source_dataset_id`` is immutable until the NEXT
    fresh attachment. Keeps the SAME ``workset_id`` when one already
    exists for this conversation (a new attachment continues the SAME
    business task, it is not a brand-new one) — mints a fresh id only the
    very first time."""
    workset_id = existing.workset_id if existing is not None else str(uuid.uuid4())
    return Workset(
        workset_id=workset_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
        source_dataset_id=dataset_id,
        current_dataset_id=dataset_id,
        scope=SCOPE_FULL_DATASET,
        selected_identifiers=(),
    )


def select_single(workset: Workset, identifier: str) -> Workset:
    """Selecting one product is ONLY a scope change over the SAME Workset
    — never a new/competing business context (requirement 3). Never
    touches ``source_dataset_id``/``current_dataset_id``."""
    ident = str(identifier or "").strip()
    return replace(workset, scope=SCOPE_SINGLE, selected_identifiers=(ident,) if ident else ())


def select_full_dataset(workset: Workset, *, dataset_id: str | None = None) -> Workset:
    """Returns to the canonical SOURCE (or an explicitly given dataset
    version) with the whole table in focus — never re-uploads, never
    mints a new ``workset_id``."""
    return replace(
        workset,
        current_dataset_id=str(dataset_id or workset.source_dataset_id),
        scope=SCOPE_FULL_DATASET,
        selected_identifiers=(),
    )


def select_filtered_set(workset: Workset, *, dataset_id: str, identifiers: tuple = ()) -> Workset:
    return replace(
        workset,
        current_dataset_id=str(dataset_id),
        scope=SCOPE_FILTERED_SET,
        selected_identifiers=tuple(str(x) for x in identifiers),
    )


def advance_dataset_version(
    workset: Workset,
    *,
    dataset_id: str,
    scope: str = SCOPE_FULL_DATASET,
    identifiers: tuple = (),
) -> Workset:
    """A deterministic transformation always produces a NEW derived
    dataset version — ``source_dataset_id`` NEVER changes here (the
    critical invariant); only ``current_dataset_id``/``scope`` advance."""
    return replace(
        workset,
        current_dataset_id=str(dataset_id),
        scope=scope if scope in VALID_SCOPES else SCOPE_FULL_DATASET,
        selected_identifiers=tuple(str(x) for x in identifiers),
    )


def apply_tool_result(workset: Workset, data: Mapping) -> Workset:
    """Interprets an EXISTING ``data.excel_assistant``/``assist`` (i.e.
    ``data_intel.service.DataIntelligenceService.execute_nl_request``)
    tool response and advances the Workset accordingly — purely from
    ALREADY-EXISTING structured ``status``/``row_count_*`` fields that
    tool already returns for other reasons, never from the request text
    itself (no phrase/value/product-specific routing).

    - ``ROW_FOUND``: exactly one product resolved -> scope narrows to
      SINGLE, keyed by that product's canonical sku (falls back to the
      matched value when no sku role is present in this table).
    - ``OK``: a deterministic transform ran, producing a new derived
      dataset. Row count unchanged -> the whole (possibly already-
      filtered) current dataset was touched (FULL_DATASET); row count
      reduced -> a subset was kept (FILTERED_SET).
    - ``ANALYZED``: a whole-dataset analysis ran over the CURRENT
      dataset -> scope becomes/stays FULL_DATASET.
    - Anything else (``AMBIGUOUS``/``BATCH_QUEUED``/``NEEDS_USER_MAPPING``/
      unrecognized): nothing conclusive happened this turn — the dataset
      id is still tracked (mirrors the pre-Workset flat-key behavior) but
      scope/selection are left exactly as they were.
    """
    new_dataset_id = str(data.get("dataset_id") or "")
    if not new_dataset_id:
        return workset
    status = str(data.get("status") or "")
    if status == _STATUS_ROW_FOUND:
        product_fields = data.get("product_fields")
        identifier = ""
        if isinstance(product_fields, Mapping):
            identifier = str(product_fields.get("sku") or "")
        if not identifier:
            identifier = str(data.get("matched_value") or "")
        return select_single(replace(workset, current_dataset_id=new_dataset_id), identifier)
    if status == _STATUS_OK:
        before = data.get("row_count_before")
        after = data.get("row_count_after")
        scope = SCOPE_FULL_DATASET
        try:
            if before is not None and after is not None and int(after) < int(before):
                scope = SCOPE_FILTERED_SET
        except (TypeError, ValueError):
            pass
        return advance_dataset_version(workset, dataset_id=new_dataset_id, scope=scope)
    if status == _STATUS_ANALYZED:
        return select_full_dataset(workset, dataset_id=new_dataset_id)
    return replace(workset, current_dataset_id=new_dataset_id)
