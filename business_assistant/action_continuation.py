"""Deterministic multi-turn action continuation for conversational Panda.

Composes with existing follow-up resolution. Does not call models.
Does not replace Router, Pipeline, ToolGateway, or conversation history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from agents.routing_requirements import FRESHNESS_CURRENT, derive_task_requirements
from autonomy.capabilities import (
    CAP_FILESYSTEM_WRITE,
    CAP_IMAGE_EDIT,
    CAP_IMAGE_GENERATE,
    CAP_SCRAPE,
)
from business_assistant.follow_up import (
    KIND_NEW_TOPIC,
    KIND_REFERENT,
    KIND_TRANSFORM,
    FollowUpResolution,
)
from business_assistant.intent import requires_business_integration
from business_assistant import workset as workset_lib
from data_intel.cleaning import normalize_decimal_string
from security.tenant import require_tenant_id

# Block 5.1: FAMILY_EXCEL now routes to the real, active data_intel capability
# (chat-driven Excel/CSV analysis/transform/compare) instead of the disabled
# excel.inspect stub. TOOL_EXCEL_INSPECT is kept only as a historical alias --
# nothing still routes to it.
TOOL_EXCEL_INSPECT = "excel.inspect"
TOOL_DATA_EXCEL_ASSISTANT = "data.excel_assistant"
TOOL_DATA_COMPARE_WORKBOOKS = "data.compare_workbooks"
TOOL_IMAGE_EDIT = "image.edit"
TOOL_IMAGE_GENERATE = "image.generate"
# Block 5.2: single chat-facing entry point for the Data Acquisition &
# Parsing Platform (see ``acquisition/tools.py``'s ``AcquisitionToolAdapter``).
# Panda -- not the user -- decides fetch vs. crawl inside this one tool.
TOOL_SCRAPE_EXTRACT = "scrape.extract"
# Block 5.3: single chat-facing entry point for the activated Content
# Intelligence pipeline (see ``content_intel/tools.py``'s ``"create"`` op /
# ``ContentIntelligenceService.create_content_from_request``). One call
# composes Search/Acquisition evidence -> Research -> Content generation ->
# Review -> Artifact -- Panda decides internally whether/what to fetch.
TOOL_CONTENT_CREATE = "content.create"
# Block 5.5: single chat-facing entry point for the activated Product
# Intelligence platform (see ``product_intel/tools.py``'s ``"assist"`` op /
# ``ProductIntelligenceService.execute_nl_request``). Mirrors
# ``TOOL_DATA_EXCEL_ASSISTANT``: Panda -- not the user -- decides internally
# which governed ``product.*`` operation (import/match/dedupe/validate/
# reconcile/enrich/export) the instruction maps to.
TOOL_PRODUCT_CATALOG_ASSIST = "product.catalog_assist"


# --- Public decision / lifecycle labels (internal only) -------------------

CONTINUE_ACTIVE_TASK = "CONTINUE_ACTIVE_TASK"
NEW_TASK = "NEW_TASK"
AMBIGUOUS = "AMBIGUOUS"

READY_TO_EXECUTE = "READY_TO_EXECUTE"
NEEDS_REQUIRED_INPUT = "NEEDS_REQUIRED_INPUT"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
NOT_EXECUTABLE = "NOT_EXECUTABLE"
CONVERSATIONAL_ONLY = "CONVERSATIONAL_ONLY"

ANSWER_TEXT = "ANSWER_TEXT"
CALL_TOOL = "CALL_TOOL"
ASK_CLARIFICATION = "ASK_CLARIFICATION"
REQUEST_APPROVAL = "REQUEST_APPROVAL"
FAIL_UNAVAILABLE = "FAIL_UNAVAILABLE"
# PANDA -- first controlled production Bitrix product write (PR #43):
# an explicit, already-confirmed instruction to create a specific,
# previously-previewed product in Bitrix. Deliberately NOT ``CALL_TOOL``:
# ``business_assistant.controlled_bitrix_write.execute_single_product_write``
# is not a ToolGateway-registered tool -- it is invoked directly (still
# through its own unchanged IntegrationActivationService approval/
# idempotency gate), so the caller (WorkflowPandaConversationGateway) must
# dispatch it through a dedicated, unambiguous decision instead of the
# generic tool-invocation path.
CALL_CONTROLLED_BITRIX_WRITE = "CALL_CONTROLLED_BITRIX_WRITE"
# Product enrichment pipeline follow-up: an explicit instruction to prepare
# a COMPLETE product card (research/characteristics/content/media/SEO --
# ``product_enrichment`` package) for the SAME single product a prior
# ``data.excel_assistant`` ROW_FOUND preview identified -- e.g. "Подготовь
# полную карточку товара". Deliberately NOT ``CALL_CONTROLLED_BITRIX_WRITE``
# (enrichment never mutates Bitrix -- see ``is_explicit_product_enrichment_
# request`` below, which never matches an explicit Bitrix-write
# confirmation) and NOT ``CALL_TOOL`` (enrichment is not a ToolGateway-
# registered tool either -- it composes ToolGateway calls internally, but
# is invoked directly by ``WorkflowPandaConversationGateway``, exactly like
# ``CALL_CONTROLLED_BITRIX_WRITE``).
CALL_PRODUCT_ENRICHMENT = "CALL_PRODUCT_ENRICHMENT"
# Production defect closure: read-only follow-up question about an ALREADY
# prepared complete card -- "Покажи точно, какие данные будут записаны в
# Bitrix/Aspro, если я подтвержу запись ... Ничего не записывай". Answers
# from the enrichment state the prior CALL_PRODUCT_ENRICHMENT turn already
# persisted on the active task, through the EXISTING read-only
# ``prepare_single_product_write`` preview. Never a write (no approval
# marker -- see ``is_bitrix_write_plan_question``) and never a re-run of the
# enrichment pipeline.
EXPLAIN_BITRIX_WRITE_PLAN = "EXPLAIN_BITRIX_WRITE_PLAN"
# Batch Bitrix existence-check defect closure: a read-only "which rows of
# the WHOLE uploaded price list already exist in Bitrix, which are new, and
# which are ambiguous" classification over the active FAMILY_EXCEL task's
# dataset -- e.g. "Проверь весь прайс перед загрузкой на сайт. Покажи,
# какие товары уже есть в Bitrix". Reuses the EXISTING single-product
# duplicate/read logic (``BitrixProductBridge.plan_sync``) once per row --
# never a new Bitrix client, never a write (``plan_sync`` never mutates),
# never one conversational turn per row.
CHECK_BITRIX_EXISTENCE_BATCH = "CHECK_BITRIX_EXISTENCE_BATCH"

STATUS_DRAFT = "DRAFT"
STATUS_WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
STATUS_READY = "READY"
STATUS_EXECUTING = "EXECUTING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED_RETRYABLE = "FAILED_RETRYABLE"
STATUS_CANCELLED = "CANCELLED"
STATUS_SUPERSEDED = "SUPERSEDED"

FAMILY_IMAGE_GENERATE = "image_generate"
FAMILY_IMAGE_EDIT = "image_edit"
FAMILY_SEARCH = "search"
FAMILY_EXCEL = "excel"
FAMILY_DOCUMENT = "document"
FAMILY_WRITE = "write_governed"
# Block 5.2: acquisition (scrape/crawl/extract a public web page) -- distinct
# from FAMILY_EXCEL (analyze/transform already-acquired structured data) and
# FAMILY_SEARCH (find pages, never fetch/parse their content). See section 21
# of the Block 5.2 spec: SEARCH != ACQUISITION != CRAWL != DATA INTELLIGENCE.
FAMILY_ACQUISITION = "acquisition"
# Block 5.3: content creation (research a topic -- optionally grounded in
# Block 5.2 acquired pages -- generate copy, review, export as an artifact).
# Distinct from FAMILY_ACQUISITION (which only collects/parses structured
# records, never drafts prose) and FAMILY_WRITE (which mutates live business
# state and always requires separate approval).
FAMILY_CONTENT = "content"
# Block 5.5: canonical internal product/catalog intelligence (import,
# field mapping, normalization, matching/dedupe, validation, reconciliation,
# content/media enrichment, vendor-neutral export). Distinct from
# FAMILY_EXCEL (generic spreadsheet analysis/transform with no product
# domain model) -- a plain "attach a spreadsheet" turn still routes to
# FAMILY_EXCEL; only an explicit product-catalog verb routes here.
FAMILY_PRODUCT = "product"
# PANDA -- first controlled production Bitrix product write (PR #43): an
# explicit, single-turn confirmation of a Bitrix product create -- e.g.
# "Подтверждаю: создай этот товар в Bitrix. Розничная цена 29990 ₽." Never
# entered via generic continuation heuristics: only ``detect_family``'s
# ``is_explicit_bitrix_write_confirmation`` check below routes here, and it
# requires an explicit confirmation marker + an explicit Bitrix target +
# an explicit create/write verb all in the SAME message (spec requirement:
# never infer approval from vague continuation phrases).
FAMILY_BITRIX_PRODUCT_WRITE = "bitrix_product_write"

RISK_GENERATE = "generate"
RISK_READ = "read"
RISK_WRITE = "write_governed"

CAPABILITY_AVAILABLE_AND_AUTHORIZED = "CAPABILITY_AVAILABLE_AND_AUTHORIZED"
CAPABILITY_AVAILABLE_REQUIRES_APPROVAL = "CAPABILITY_AVAILABLE_REQUIRES_APPROVAL"
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
CAPABILITY_MISCONFIGURED = "CAPABILITY_MISCONFIGURED"

PARAM_SCENE = "scene_description"
PARAM_QUANTITY = "variant_count"
PARAM_ASPECT = "aspect_ratio"
PARAM_STYLE = "style"
PARAM_FILE = "file_ref"


@dataclass(frozen=True)
class CapabilityContract:
    family: str
    tool_id: str
    operation: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    defaults: Mapping[str, Any]
    risk: str
    required_capabilities: tuple[str, ...]
    artifact_type: str = ""


IMAGE_GENERATE_CONTRACT = CapabilityContract(
    family=FAMILY_IMAGE_GENERATE,
    tool_id=TOOL_IMAGE_GENERATE,
    operation="generate",
    required=(PARAM_SCENE,),
    optional=(PARAM_ASPECT, PARAM_QUANTITY, PARAM_STYLE),
    defaults={PARAM_ASPECT: "1:1", PARAM_QUANTITY: 1},
    risk=RISK_GENERATE,
    required_capabilities=(CAP_IMAGE_GENERATE,),
    artifact_type="image",
)

IMAGE_EDIT_CONTRACT = CapabilityContract(
    family=FAMILY_IMAGE_EDIT,
    tool_id=TOOL_IMAGE_EDIT,
    operation="edit",
    required=("source_version_id", "instruction"),
    optional=(),
    defaults={},
    risk=RISK_GENERATE,
    required_capabilities=(CAP_IMAGE_EDIT,),
    artifact_type="image",
)

EXCEL_CONTRACT = CapabilityContract(
    family=FAMILY_EXCEL,
    tool_id=TOOL_DATA_EXCEL_ASSISTANT,
    operation="assist",
    # No statically-required parameter: a fresh spreadsheet attachment OR an
    # inherited ``dataset_id`` from a prior turn each independently satisfy
    # "do we have data to operate on" -- checked explicitly in the FAMILY_EXCEL
    # branch of resolve_action_turn() below, not through the generic
    # required-params gate (which cannot express an OR of two sources).
    required=(),
    optional=("text", "dataset_id"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_FILESYSTEM_WRITE,),
    artifact_type="workbook",
)

COMPARE_WORKBOOKS_CONTRACT = CapabilityContract(
    family=FAMILY_EXCEL,
    tool_id=TOOL_DATA_COMPARE_WORKBOOKS,
    operation="compare_workbooks",
    required=(),
    optional=("text",),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_FILESYSTEM_WRITE,),
    artifact_type="workbook",
)

ACQUISITION_CONTRACT = CapabilityContract(
    family=FAMILY_ACQUISITION,
    tool_id=TOOL_SCRAPE_EXTRACT,
    operation="extract",
    # The URL is deterministically extracted from the user's message BEFORE
    # the task is created (see ``_extract_url`` / the FAMILY_ACQUISITION
    # branch of ``resolve_action_turn``) and stamped into ``task.parameters``
    # up front, so this "required" field is satisfied at creation time rather
    # than through a generic missing-params gate -- mirrors how FAMILY_EXCEL's
    # dataset source (attachment OR inherited dataset_id) is resolved.
    required=("url",),
    optional=("max_pages", "extraction_plan"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_SCRAPE,),
    artifact_type="dataset",
)

CONTENT_CONTRACT = CapabilityContract(
    family=FAMILY_CONTENT,
    tool_id=TOOL_CONTENT_CREATE,
    operation="create",
    # The objective/topic is deterministically extracted from the user's
    # message BEFORE task creation (see ``_extract_content_objective`` /
    # the FAMILY_CONTENT branch of ``resolve_action_turn``), mirroring how
    # FAMILY_ACQUISITION resolves ``url`` up front rather than through the
    # generic missing-params gate.
    required=("objective",),
    optional=("urls", "channel", "content_type"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_SCRAPE, CAP_FILESYSTEM_WRITE),
    artifact_type="content",
)

PRODUCT_CONTRACT = CapabilityContract(
    family=FAMILY_PRODUCT,
    tool_id=TOOL_PRODUCT_CATALOG_ASSIST,
    operation="assist",
    # No statically-required parameter: an attachment/inherited dataset_id
    # OR an already-existing catalog (from a prior product-family turn)
    # each independently satisfy "do we have data to operate on" -- checked
    # explicitly in the FAMILY_PRODUCT branch below, mirroring FAMILY_EXCEL.
    required=(),
    optional=("text", "dataset_id", "catalog_id"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_FILESYSTEM_WRITE,),
    artifact_type="catalog",
)

CONTRACTS: dict[str, CapabilityContract] = {
    FAMILY_IMAGE_GENERATE: IMAGE_GENERATE_CONTRACT,
    FAMILY_IMAGE_EDIT: IMAGE_EDIT_CONTRACT,
    FAMILY_EXCEL: EXCEL_CONTRACT,
    FAMILY_ACQUISITION: ACQUISITION_CONTRACT,
    FAMILY_CONTENT: CONTENT_CONTRACT,
    FAMILY_PRODUCT: PRODUCT_CONTRACT,
}


@dataclass
class ActiveTask:
    task_id: str
    tenant_id: str
    owner_id: str
    conversation_id: str
    family: str
    tool_id: str
    operation: str
    goal: str
    parameters: dict[str, Any] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    quantity: int | None = None
    artifact_type: str = ""
    status: str = STATUS_DRAFT
    execute_requested: bool = False
    execution_count: int = 0
    last_idempotency_key: str = ""
    last_artifact_ids: tuple[str, ...] = ()
    awaiting_quantity: bool = False
    risk: str = RISK_GENERATE

    def snapshot(self) -> "ActiveTask":
        return ActiveTask(
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            owner_id=self.owner_id,
            conversation_id=self.conversation_id,
            family=self.family,
            tool_id=self.tool_id,
            operation=self.operation,
            goal=self.goal,
            parameters=dict(self.parameters),
            missing_required=tuple(self.missing_required),
            quantity=self.quantity,
            artifact_type=self.artifact_type,
            status=self.status,
            execute_requested=self.execute_requested,
            execution_count=self.execution_count,
            last_idempotency_key=self.last_idempotency_key,
            last_artifact_ids=tuple(self.last_artifact_ids),
            awaiting_quantity=self.awaiting_quantity,
            risk=self.risk,
        )


@dataclass(frozen=True)
class ActionDecision:
    decision: str
    readiness: str
    continuation: str
    task: ActiveTask | None
    arguments: dict[str, Any] = field(default_factory=dict)
    user_message: str = ""
    tool_id: str = ""
    operation: str = ""
    extra_llm: bool = False
    capability_status: str = CAPABILITY_UNAVAILABLE
    idempotency_key: str = ""
    # Production defect closure: non-empty only for the CALL_TOOL fallback
    # ``resolve_product_enrichment_request`` returns when a product-card
    # enrichment turn names a SKU/EAN that was never resolved into
    # ``bitrix_product_fields`` on a PRIOR turn. Tells
    # ``WorkflowPandaConversationGateway.respond()`` to re-attempt
    # ``resolve_product_enrichment_request`` with this text (the original
    # enrichment turn's text, not the CALL_TOOL's own follow-up wording)
    # immediately after the row-lookup tool call, and chain straight into
    # CALL_PRODUCT_ENRICHMENT if it now resolves. Every other decision
    # leaves this at its default "" and is completely unaffected.
    chain_to_enrichment_text: str = ""
    # Production defect closure (single-turn "upload XLSX + take the first
    # product + calculate retail price + resolve exact Bitrix/Aspro
    # category" request): non-empty only for the CALL_TOOL fallback
    # ``resolve_action_turn`` returns when a pricing/category refinement
    # turn ALSO carries its own spreadsheet attachment but no FAMILY_EXCEL
    # context exists yet. Mirrors ``chain_to_enrichment_text`` exactly --
    # tells ``WorkflowPandaConversationGateway.respond()`` to re-attempt
    # ``resolve_product_pricing_category_refinement_request`` with this
    # text immediately after the row-lookup tool call, and chain straight
    # into EXPLAIN_BITRIX_WRITE_PLAN if it now resolves. Every other
    # decision leaves this at its default "" and is completely unaffected.
    chain_to_pricing_category_refinement_text: str = ""


class ActiveTaskStore:
    """In-process active-task frame keyed by existing conversation identity.

    Process-lifetime only -- lost on restart/redeploy/crash-recycle. Safe
    for tests and any caller that does not need the active task to survive
    past this process (see ``SqliteActiveTaskStore`` below for the durable
    production variant, which implements the exact same ``get``/``put``/
    ``clear`` contract)."""

    def __init__(self):
        self._tasks: dict[tuple[str, str, str], ActiveTask] = {}

    def _key(self, tenant_id: str, owner_id: str, conversation_id: str) -> tuple[str, str, str]:
        return (require_tenant_id(tenant_id), str(owner_id or ""), str(conversation_id or ""))

    def get(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> ActiveTask | None:
        if not conversation_id:
            return None
        task = self._tasks.get(self._key(tenant_id, owner_id, conversation_id))
        return task.snapshot() if task is not None else None

    def put(self, task: ActiveTask) -> None:
        if not task.conversation_id:
            return
        self._tasks[self._key(task.tenant_id, task.owner_id, task.conversation_id)] = task.snapshot()

    def clear(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        self._tasks.pop(self._key(tenant_id, owner_id, conversation_id), None)


_ACTIVE_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_assistant_active_tasks (
  tenant_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  family TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  parameters_json TEXT NOT NULL DEFAULT '{}',
  missing_required_json TEXT NOT NULL DEFAULT '[]',
  quantity INTEGER,
  artifact_type TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  execute_requested INTEGER NOT NULL DEFAULT 0,
  execution_count INTEGER NOT NULL DEFAULT 0,
  last_idempotency_key TEXT NOT NULL DEFAULT '',
  last_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
  awaiting_quantity INTEGER NOT NULL DEFAULT 0,
  risk TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (tenant_id, owner_id, conversation_id)
);
"""


class SqliteActiveTaskStore:
    """Durable production variant of ``ActiveTaskStore`` -- same ``get``/
    ``put``/``clear`` contract, backed by a dedicated SQLite file instead of
    a process-local dict.

    Production defect closure (request-scoped/process-lifetime state loss):
    ``WorkflowPandaConversationGateway`` previously always defaulted to the
    plain in-memory ``ActiveTaskStore`` in production too (nothing in
    ``main.py``/``business_assistant_api.runtime.wire_panda_conversation_
    gateway`` ever passed a durable ``action_store=``), unlike every other
    piece of state this same multi-turn continuation depends on (the parsed
    XLSX dataset in ``data_intel`` defaults to a SQLite-backed store, the
    conversation/message/request history in ``business_assistant_api`` is
    already SQLite-backed, and file attachments in ``ArtifactService`` are
    already SQLite-backed). A conversation's active product task is the ONE
    piece of state in that chain that did not survive a process
    restart/redeploy/crash-recycle -- so a conversation whose turn 1
    (attach + select a product) completed successfully before a restart
    would, on its very next attachment-less follow-up turn after the
    restart, find NO active task at all (a correctly-empty, but wrong,
    lookup against a brand-new process's empty in-memory dict) and get
    routed right back to the legacy attachment-blind business-workflow path
    -- reproducing the exact ``BA_CAPABILITY_UNAVAILABLE``/
    ``dependency_not_ready`` symptom the routing fix was supposed to have
    already closed. Mirrors the SAME "dedicated SQLite file, shared across
    API replicas/restarts" pattern ``finops.budget_store.SqliteBudgetStore``
    and ``providers.governor.SqliteProviderGovernorStore`` already
    established in this codebase for the identical class of problem
    (in-memory router/budget state needing to survive a restart) -- not a
    second, parallel state system."""

    def __init__(self, db_path: str):
        self.path = str(db_path)
        if self.path != ":memory:":
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_ACTIVE_TASK_SCHEMA)

    def _key(self, tenant_id: str, owner_id: str, conversation_id: str) -> tuple[str, str, str]:
        return (require_tenant_id(tenant_id), str(owner_id or ""), str(conversation_id or ""))

    def get(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> ActiveTask | None:
        if not conversation_id:
            return None
        tenant, owner, conv = self._key(tenant_id, owner_id, conversation_id)
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM business_assistant_active_tasks "
                "WHERE tenant_id = ? AND owner_id = ? AND conversation_id = ?",
                (tenant, owner, conv),
            ).fetchone()
        if row is None:
            return None
        return ActiveTask(
            task_id=str(row["task_id"] or ""),
            tenant_id=str(row["tenant_id"] or ""),
            owner_id=str(row["owner_id"] or ""),
            conversation_id=str(row["conversation_id"] or ""),
            family=str(row["family"] or ""),
            tool_id=str(row["tool_id"] or ""),
            operation=str(row["operation"] or ""),
            goal=str(row["goal"] or ""),
            parameters=json.loads(row["parameters_json"] or "{}"),
            missing_required=tuple(json.loads(row["missing_required_json"] or "[]")),
            quantity=row["quantity"],
            artifact_type=str(row["artifact_type"] or ""),
            status=str(row["status"] or ""),
            execute_requested=bool(row["execute_requested"]),
            execution_count=int(row["execution_count"] or 0),
            last_idempotency_key=str(row["last_idempotency_key"] or ""),
            last_artifact_ids=tuple(json.loads(row["last_artifact_ids_json"] or "[]")),
            awaiting_quantity=bool(row["awaiting_quantity"]),
            risk=str(row["risk"] or ""),
        )

    def put(self, task: ActiveTask) -> None:
        if not task.conversation_id:
            return
        snap = task.snapshot()
        tenant, owner, conv = self._key(snap.tenant_id, snap.owner_id, snap.conversation_id)
        with self._lock:
            self._connect().execute(
                """
                INSERT INTO business_assistant_active_tasks (
                    tenant_id, owner_id, conversation_id, task_id, family, tool_id,
                    operation, goal, parameters_json, missing_required_json, quantity,
                    artifact_type, status, execute_requested, execution_count,
                    last_idempotency_key, last_artifact_ids_json, awaiting_quantity, risk,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, owner_id, conversation_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    family = excluded.family,
                    tool_id = excluded.tool_id,
                    operation = excluded.operation,
                    goal = excluded.goal,
                    parameters_json = excluded.parameters_json,
                    missing_required_json = excluded.missing_required_json,
                    quantity = excluded.quantity,
                    artifact_type = excluded.artifact_type,
                    status = excluded.status,
                    execute_requested = excluded.execute_requested,
                    execution_count = excluded.execution_count,
                    last_idempotency_key = excluded.last_idempotency_key,
                    last_artifact_ids_json = excluded.last_artifact_ids_json,
                    awaiting_quantity = excluded.awaiting_quantity,
                    risk = excluded.risk,
                    updated_at = excluded.updated_at
                """,
                (
                    tenant,
                    owner,
                    conv,
                    snap.task_id,
                    snap.family,
                    snap.tool_id,
                    snap.operation,
                    snap.goal,
                    json.dumps(snap.parameters),
                    json.dumps(list(snap.missing_required)),
                    snap.quantity,
                    snap.artifact_type,
                    snap.status,
                    1 if snap.execute_requested else 0,
                    snap.execution_count,
                    snap.last_idempotency_key,
                    json.dumps(list(snap.last_artifact_ids)),
                    1 if snap.awaiting_quantity else 0,
                    snap.risk,
                    utc_now_iso(),
                ),
            )

    def clear(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        tenant, owner, conv = self._key(tenant_id, owner_id, conversation_id)
        with self._lock:
            self._connect().execute(
                "DELETE FROM business_assistant_active_tasks "
                "WHERE tenant_id = ? AND owner_id = ? AND conversation_id = ?",
                (tenant, owner, conv),
            )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    raw = (text or "").strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", raw)


def _has_stem(text: str, stems: tuple[str, ...]) -> bool:
    blob = _norm(text)
    if not blob:
        return False
    return any(_norm(stem) in blob for stem in stems if stem)


def _token_count(text: str) -> int:
    return len(_norm(text).split())


_IMAGE_ARTIFACT_STEMS = (
    "image",
    "picture",
    "photo",
    "illustrat",
    "artwork",
    "avatar",
    "logo",
    "картин",
    "изобр",
    "фото",
    "логотип",
    "рисун",
    "иллюстрац",
)
_IMAGE_VERB_STEMS = (
    "generat",
    "draw",
    "paint",
    "create",
    "сгенер",
    "сгенен",
    "нарис",
    "создай",
    "создать",
)
_MAKE_STEMS = ("make", "сделай", "сделать", "сделаем")
_EXECUTE_STEMS = _IMAGE_VERB_STEMS + ("сделай", "сделать", "make it", "do it")
_CANCEL_STEMS = ("cancel", "отмен", "стоп", "stop that")
_SEVERAL_STEMS = ("several", "multiple", "a few", "нескольк", "пару", "несколько")
_LOGO_STEMS = ("logo", "логотип")
_SQUARE_STEMS = ("square", "квадрат")
_REALISM_STEMS = ("realism", "realistic", "реализм", "реалистич")
_NIGHT_STEMS = ("night", "ноч")
_DAY_STEMS = ("daytime", "day", "днем", "днём", "день")
_FOREST_STEMS = ("forest", "лес")
_WOLF_STEMS = ("wolf", "волк")
_CORRECTION_STEMS = ("нет,", "no,", "не то", "instead")
_YES_STEMS = ("да", "yes", "ок", "ok", "угу")
_EXCEL_STEMS = ("excel", "xlsx", "csv", "spreadsheet", "таблиц")
_DOC_STEMS = ("document", "pdf", "docx", "документ")
_SEARCH_STEMS = ("search", "найди", "find ", "google")
_WEATHER_STEMS = ("weather", "погод")
_QUESTION_NEW_STEMS = (
    "что такое",
    "what is",
    "расскажи",
    "tell me",
    "how does",
    "как работает",
    "для ип",
    "налог",
)
_WRITE_STEMS = (
    "удали заказ",
    "измени цен",
    "опубликуй",
    "отправь письмо",
    "измени остат",
    "delete order",
    "change price",
    "publish product",
    "send email",
)

# PANDA -- first controlled production Bitrix product write (PR #43): three
# independent signals, ALL required in the SAME message, so an explicit
# Bitrix create can never be confused with a vague continuation
# ("продолжай", "давай", "ок", "делай дальше" match none of these).
_BITRIX_APPROVAL_MARKER_STEMS = ("подтвержда", "подтверд", "confirm", "i confirm")
_BITRIX_TARGET_MARKER_STEMS = ("bitrix", "битрикс", "аспро", "aspro")
# Write-plan confirmation-semantics defect closure: an owner confirming an
# ALREADY-SHOWN write plan (``EXPLAIN_BITRIX_WRITE_PLAN``/enrichment preview)
# routinely refers to "the shown plan" -- e.g. "Да, подтверждаю запись по
# показанному плану." -- instead of re-naming Bitrix/Aspro a second time in
# the confirmation message itself. This is an equally unambiguous target
# signal IN THIS SPECIFIC FLOW (a write PLAN is only ever shown by this
# same Bitrix-write-confirmation feature -- see ``EXPLAIN_BITRIX_WRITE_
# PLAN``/``format_write_plan_text``), so it stands alongside the literal
# Bitrix/Aspro mention rather than replacing it. This does NOT loosen the
# actual write itself: ``resolve_bitrix_write_confirmation`` still fails
# closed (asks for a fresh plan) whenever no matching prepared-product
# task context exists, exactly as before -- this only widens which
# context-bound confirmations are even recognized as approval.
_BITRIX_SHOWN_PLAN_TARGET_STEMS = (
    "показанному плану",
    "показанной карточ",
    "по показанному",
    "shown plan",
    "shown write plan",
    "the plan shown",
    "the shown plan",
)
_BITRIX_CREATE_VERB_STEMS = (
    "созда",
    "запиши",
    "добавь",
    "опубликуй",
    "create",
    "publish",
    "write it",
    # Production defect closure: an owner confirming an ALREADY prepared
    # card most often names the ACTION as a noun ("подтверждаю запись",
    # "выполни запись") rather than using an imperative create verb. Without
    # these, such a turn matched no Bitrix branch at all and fell through to
    # the FAMILY_EXCEL continuation, which simply re-printed the prepared
    # product-card preview instead of performing the governed write.
    # Multi-word phrases (never a bare "запис" stem) so an unrelated mention
    # of "запись" cannot satisfy this on its own.
    "подтверждаю запись",
    "подтверждаю эту запись",
    "выполни запись",
    "выполнить запись",
    "выполняй запись",
    "сделай запись",
    "записать",
    "записывай",
    "запишите",
    "perform the write",
    "do the write",
    "write this product",
    "write the product",
    "write this prepared product",
)
# Production defect closure: the phrases above only match when the action
# verb and the write noun are ADJACENT, but the real production
# confirmation qualifies the noun -- "Подтверждаю. Выполни РЕАЛЬНУЮ запись
# этого подготовленного товара в Bitrix/Aspro." -- so none of them matched
# and the turn fell through to the FAMILY_EXCEL preview again. Allow a
# couple of qualifier words between an explicit perform/do verb and the
# write noun. Still requires a real action verb (never a bare "запись"),
# and the approval marker + Bitrix/Aspro target are checked separately.
_BITRIX_WRITE_ACTION_RE = re.compile(
    r"\b(выполн\w+|сделай|сделать|произвед\w+|запусти)\s+(?:[\w-]+\s+){0,2}запис"
    r"|\b(perform|execute|do|make)\s+(?:[\w-]+\s+){0,2}write"
    r"|\bwrite\s+(?:[\w-]+\s+){0,2}(product|card|item)\b",
    re.I,
)
# Fail closed: an explicit "do NOT write" in the same message always wins
# over a confirmation phrase above (the enrichment/preview replies this
# conversation flow produces routinely end with "Ничего в Bitrix не
# записывай"-style wording).
_BITRIX_NO_WRITE_RE = re.compile(
    r"(не\s+запис|без\s+записи|don'?t\s+write|do\s+not\s+write|nothing\s+to\s+bitrix)",
    re.I,
)
_CONFIRMED_RETAIL_PRICE_RE = re.compile(
    r"(розничн\w*|продажн\w*|retail|selling)\D{0,20}?(\d[\d\s]*(?:[.,]\d+)?)", re.I
)

# Product enrichment pipeline follow-up: two independent stem groups, BOTH
# required in the same message -- an enrichment "prepare/enrich" verb AND
# an explicit "full/complete card" (or "characteristics"/"specification")
# target noun. Deliberately narrower than ``_PRODUCT_INTENT_STEMS``'
# "подготовь карточки"/"карточки товар" (Block 5.5's batch-catalog
# family) -- those are plural/different phrasing and never match here; the
# task's own literal example ("Подготовь полную карточку товара") is the
# calibration point for this pair.
_ENRICHMENT_VERB_STEMS = ("подготов", "обогат", "enrich", "prepare")
_ENRICHMENT_TARGET_STEMS = (
    "полную карточ",
    "полная карточ",
    "complete card",
    "complete product card",
    "full card",
    "full product card",
)

# Production defect closure ("enrich this SAME prepared product" follow-up
# on an ALREADY-selected product, e.g. "...выполни полную подготовку
# карточки перед записью... Выполни обогащение товара: характеристики,
# краткое и полное описание, SEO, основное изображение и галерею..."):
# the enrichment TARGET here is expressed as an explicit shopping list of
# the enrichment pipeline's own components (characteristics/description/
# SEO/image/gallery) rather than the literal adjacent phrase "полную
# карточку" -- "полную" instead modifies "подготовку"
# ("полную ПОДГОТОВКУ карточки"), so ``_ENRICHMENT_TARGET_STEMS`` above
# never matches even though the intent is identical. Two or more of these
# component nouns alongside the SAME enrichment verb (see
# ``is_explicit_product_enrichment_request``) is at least as unambiguous a
# signal as the literal "full card" phrase, and is never satisfied by a
# bare "подготовь"/"обогати" alone -- e.g. a plain read-only "покажи
# окончательный план ... характеристик ..." (no enrichment verb at all)
# never matches this, so PR #67's write-plan question keeps routing to
# ``EXPLAIN_BITRIX_WRITE_PLAN`` exactly as before.
_ENRICHMENT_COMPONENT_STEMS = ("характеристик", "описан", "seo", "изображен", "галере")

# Production defect closure (single-product Bitrix preparation follow-up,
# no attachment on this turn -- e.g. "Do not analyze the whole spreadsheet.
# Choose ONE first product from LG_TV.xlsx and prepare it for Bitrix/
# Aspro: exact model, EAN, brand, category, purchase price, retail price,
# characteristics, description, images, and show the Bitrix write plan.
# Do not write/publish yet."). This mentions a Bitrix/Aspro target and a
# preparation verb, so ``requires_business_integration`` below matches it
# (it also carries the spreadsheet filename/"xlsx", one of that function's
# own business-task keywords) whenever the follow-up turn itself carries
# no ``artifact_refs`` (the workbook was uploaded on an earlier turn) --
# routing it into the same degraded fixture business-workflow engine as
# the enrichment/write-plan defects above, instead of the conversational
# pipeline that can actually resolve the already-uploaded dataset and
# preview one row. Deliberately narrow and additive, mirroring
# ``is_explicit_product_enrichment_request``/``is_bitrix_write_plan_
# question`` exactly: requires an explicit single-item SELECTION signal
# (an explicit "choose/select/pick the first/one product" instruction, or
# an explicit "don't analyze the whole spreadsheet" qualifier) together
# with a Bitrix/Aspro preparation target in the SAME message. Never
# matches a bare "подготовь"/"prepare" alone, and an explicit write
# confirmation (``is_explicit_bitrix_write_confirmation``) always wins.
_SINGLE_PRODUCT_SELECT_RE = re.compile(
    r"\b(?:choose|select|pick|выбери|выберите|возьми|возьмите)\b(?:\s+\w+){0,3}\s+"
    r"\b(?:one|first|один|одна|одну|первый|первую|первое)\b"
    r"|\b(?:first|1|один|одна|первый|первую|первое)\s+(?:product|item|товар\w*|позици\w*)\b",
    re.I,
)
_WHOLE_SPREADSHEET_NEGATION_RE = re.compile(
    r"(?:do\s+not|don.t|не)\s+анализ\w*\s+всю\s+таблиц\w*"
    r"|(?:do\s+not|don.t)\s+analy[sz]e\s+the\s+whole\s+spreadsheet",
    re.I,
)
_SINGLE_PRODUCT_PREP_VERB_STEMS = ("подготов", "prepare", "оформи", "show", "покаж")


def is_explicit_single_product_bitrix_prep_request(text: str) -> bool:
    """True only for an explicit instruction to select exactly ONE product
    from an already-uploaded price list and prepare/preview it for
    Bitrix/Aspro (never a "analyze the whole spreadsheet" style request).
    See the constants above for the exact production defect this closes."""
    blob = _norm(text)
    if not blob:
        return False
    if is_explicit_bitrix_write_confirmation(blob):
        return False
    if not _has_stem(blob, _BITRIX_TARGET_MARKER_STEMS):
        return False
    if not _has_stem(blob, _SINGLE_PRODUCT_PREP_VERB_STEMS):
        return False
    return bool(_SINGLE_PRODUCT_SELECT_RE.search(blob) or _WHOLE_SPREADSHEET_NEGATION_RE.search(blob))


# Production defect closure (Turn-3 pricing/category refinement follow-up,
# reproduced AFTER the single-product Bitrix prep fix above): "Рассчитай
# розничную цену для этого товара и определи точную категорию Bitrix/Aspro
# для телевизора. Покажи обновлённую карточку и план записи. Ничего в
# Bitrix пока не записывай." refers back to the product ALREADY selected/
# prepared by a prior turn (no fresh selection wording, no attachment on
# this turn) and asks Panda to (re)surface the retail price and the exact
# Bitrix/Aspro category for it. It mentions a Bitrix/Aspro target plus a
# "покажи"/"show" action verb, so ``requires_business_integration`` below
# matches it and routes it to the attachment-blind legacy business-
# workflow recipe engine, whose reply is the generic "Задача выполнена."
# diagnostic summary -- never the updated card/write plan. Deliberately
# narrow and additive, mirroring ``is_explicit_single_product_bitrix_prep_
# request``/``is_bitrix_write_plan_question`` exactly: requires a Bitrix/
# Aspro target PLUS an explicit pricing-calculation verb OR an explicit
# category-determination verb+noun pair, in the SAME message, and an
# explicit write confirmation always wins.
_PRICING_CALC_VERB_STEMS = ("рассчита", "расчита", "вычисли", "calculate", "compute")
_CATEGORY_DETERMINE_VERB_STEMS = ("определи", "уточни", "resolve", "determine")
_CATEGORY_NOUN_STEMS = ("категор", "category", "раздел каталог")


def is_explicit_product_pricing_or_category_refinement_request(text: str) -> bool:
    """True only for an explicit follow-up asking to (re)calculate the
    retail price and/or resolve the exact Bitrix/Aspro category for the
    product a PRIOR turn already selected/prepared. See the constants
    above for the exact production defect this closes. Never matches an
    actual write confirmation."""
    blob = _norm(text)
    if not blob:
        return False
    if is_explicit_bitrix_write_confirmation(blob):
        return False
    if not _has_stem(blob, _BITRIX_TARGET_MARKER_STEMS):
        return False
    has_pricing_calc = _has_stem(blob, _PRICING_CALC_VERB_STEMS)
    has_category_calc = _has_stem(blob, _CATEGORY_DETERMINE_VERB_STEMS) and _has_stem(blob, _CATEGORY_NOUN_STEMS)
    return has_pricing_calc or has_category_calc

# Block 5.2: a bare http(s) URL in the message is the strongest, fully
# deterministic acquisition signal -- mirrors how a spreadsheet attachment is
# the strongest Excel signal (spec section 20: "no technical mode picker").
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.I)
_PAGES_COUNT_RE = re.compile(r"(\d{1,3})\s*(?:страниц\w*|pages?)", re.I)
MAX_ACQUISITION_PAGES = 200

# Weaker fallback signal than a URL: explicit "go acquire/parse" verbs with no
# URL yet (e.g. referencing "this page" from earlier context) still route to
# FAMILY_ACQUISITION so the turn resolver asks for the missing URL instead of
# silently answering conversationally (spec section 39: "ask one useful
# clarification").
_ACQUISITION_INTENT_STEMS = (
    "собери",
    "вытащи",
    "извлеки",
    "спарси",
    "спарсь",
    "парсинг",
    "scrape",
    "crawl",
)

# Block 5.3: explicit content-creation verbs -- checked BEFORE the bare-URL
# acquisition signal (spec chain: "write an article about <url>" must route
# to content creation with the URL as research input, not to a bare scrape).
_CONTENT_INTENT_STEMS = (
    "напиши статью",
    "напиши пост",
    "напиши текст",
    "напиши копирайт",
    "сгенерируй статью",
    "сгенерируй текст",
    "сгенерируй пост",
    "создай контент",
    "создай статью",
    "создай пост",
    "write an article",
    "write a post",
    "write copy",
    "generate content",
    "generate an article",
    "generate a post",
)


# Block 5.5: explicit product-catalog intents (spec section 20 examples).
# Checked BEFORE the generic spreadsheet-attachment -> FAMILY_EXCEL signal so
# "Загрузи этот прайс и собери каталог товаров" (an attached spreadsheet
# PLUS an explicit "build a product catalog" verb) routes to Product
# Intelligence instead of generic Excel analysis.
_PRODUCT_CATALOG_STEMS = (
    "каталог товар",
    "собери каталог",
    "собрать каталог",
    "product catalog",
    "build a catalog",
)
_PRODUCT_INTENT_STEMS = (
    "сопоставь товар",
    "сопоставить товар",
    "найди дубли",
    "дубли по артикул",
    "дубли по штрихкод",
    "нормализуй характеристик",
    "обнови остатк",
    "подготовь карточки",
    "карточки товар",
    "сделай описани",
    "не удалось однозначно сопоставить",
    "match products",
    "find duplicate",
    "normalize attributes",
    "update stock",
    "product cards",
) + _PRODUCT_CATALOG_STEMS

_CONTENT_TRIGGER_RE = re.compile(
    r"^\s*(?:please|пожалуйста)?\s*"
    r"(?:напиши(?:те)?|сгенерируй|создай|write|generate)\s+"
    r"(?:статью|пост|текст|копирайт|copy|content|an?\s+article|a\s+post)\s*"
    r"(?:про|о|на\s+тему|about|on|for)?\s*",
    re.I,
)


def _extract_content_objective(text: str) -> str:
    blob = (text or "").strip()
    match = _CONTENT_TRIGGER_RE.match(blob)
    if not match:
        return blob
    # A fully-consumed trigger phrase with nothing left ("напиши статью") is a
    # deliberate signal that no topic was given yet -- returns "" so the
    # missing-required-param gate asks a clarification instead of treating
    # the bare verb phrase itself as the topic.
    return blob[match.end():].strip(" .,:;-\u2014")


def _extract_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}\u00bb\"'")


def _extract_max_pages(text: str) -> int:
    match = _PAGES_COUNT_RE.search(text or "")
    if not match:
        return 1
    try:
        value = int(match.group(1))
    except ValueError:
        return 1
    return max(1, min(value, MAX_ACQUISITION_PAGES))


def _is_image_artifact_request(text: str) -> bool:
    return _has_stem(text, _IMAGE_ARTIFACT_STEMS)


def _is_image_execute_verb(text: str) -> bool:
    return _has_stem(text, _IMAGE_VERB_STEMS) or (
        _has_stem(text, _MAKE_STEMS) and _is_image_artifact_request(text)
    )


def _is_cancel(text: str) -> bool:
    blob = _norm(text)
    if blob in {"отмена", "cancel", "стоп", "stop"}:
        return True
    return _token_count(text) <= 3 and _has_stem(text, _CANCEL_STEMS)


def _is_yes(text: str) -> bool:
    blob = _norm(text).strip(" !.?,")
    return blob in {"да", "yes", "ок", "ok", "угу", "ага"}


def is_explicit_bitrix_write_confirmation(text: str) -> bool:
    """True only for an explicit, unambiguous, single-message instruction to
    create a specific product in Bitrix that ALSO explicitly confirms/
    approves it -- e.g. 'Подтверждаю: создай этот товар в Bitrix. Розничная
    цена 29990 ₽.'. Requires all three signals (confirmation marker +
    Bitrix target + create/write verb); a bare 'да'/'ок'/'давай'/'продолжай'/
    'делай дальше' -- with or without an active task -- can never satisfy
    this, so approval is never inferred from a vague continuation phrase.
    An explicit "do not write" or a read-only "what would be written?"
    question (see ``is_bitrix_write_plan_question``) always wins over the
    confirmation phrases -- approval must be unambiguous."""
    blob = _norm(text)
    if not blob:
        return False
    if _BITRIX_NO_WRITE_RE.search(blob):
        return False
    if _is_write_plan_ask(blob):
        return False
    if not _has_stem(blob, _BITRIX_APPROVAL_MARKER_STEMS):
        return False
    if not _has_stem(blob, _BITRIX_TARGET_MARKER_STEMS) and not _has_stem(blob, _BITRIX_SHOWN_PLAN_TARGET_STEMS):
        return False
    return _has_stem(blob, _BITRIX_CREATE_VERB_STEMS) or bool(_BITRIX_WRITE_ACTION_RE.search(blob))


def is_explicit_product_enrichment_request(text: str) -> bool:
    """True only for an explicit instruction to prepare/enrich a COMPLETE
    product card -- e.g. 'Подготовь полную карточку товара'. Deliberately
    distinct from ``is_explicit_bitrix_write_confirmation`` (that one
    additionally requires an explicit Bitrix target + confirm marker) --
    this NEVER writes anything to Bitrix by itself; it only runs the
    ``product_enrichment`` pipeline and shows a complete preview
    (requirement 12: "enrichment is NOT approval to write"). Requires
    BOTH an enrichment verb and an explicit "full/complete card" target
    -- OR (production defect closure) the SAME enrichment verb plus two or
    more of the enrichment pipeline's own component nouns
    (characteristics/description/SEO/image/gallery, see
    ``_ENRICHMENT_COMPONENT_STEMS``), which covers "...выполни полную
    подготовку карточки... Выполни обогащение товара: характеристики,
    описание, SEO, изображение и галерею..." (the target noun and the verb
    are no longer adjacent, so the literal "full card" phrase does not
    match, but the instruction is just as explicit an ENRICHMENT ACTION
    request) -- in the SAME message, so it can never be confused with
    Block 5.5's own "Подготовь карточки товаров" (batch product-catalog
    family, plural, different phrasing) or a bare "подготовь"/"обогати"
    alone."""
    blob = _norm(text)
    if not blob:
        return False
    if not _has_stem(blob, _ENRICHMENT_VERB_STEMS):
        return False
    if _has_stem(blob, _ENRICHMENT_TARGET_STEMS):
        return True
    component_hits = sum(1 for stem in _ENRICHMENT_COMPONENT_STEMS if stem in blob)
    return component_hits >= 2


# Production defect closure (read-only "what exactly would be written?"
# follow-up): three independent signals, ALL required in the same message --
# a "show/explain" ask, a Bitrix/Aspro target and an explicit "будет
# записано"/"would be written" phrase. "если я подтвержу запись" alone can
# never turn this into a write: the caller checks
# ``is_explicit_bitrix_write_confirmation`` first and this predicate
# additionally refuses any message that satisfies it.
_WRITE_PLAN_ASK_STEMS = ("покаж", "показать", "объясн", "перечисл", "show", "list", "explain")
# Deliberately a phrase, not a bare "запис" stem: "...покажи подготовленную
# карточку и план действий перед записью" (Block 5.5's own row-preview
# request) must keep routing exactly as it does today.
#
# Production defect closure (Task 6: "покажи ... окончательный план для
# этого же товара ... Ничего пока не записывай и не публикуй." immediately
# after a Turn-1 enrichment): the SAME read-only "show me the write plan"
# shape as the "будет записан" phrasing above, just worded as "окончательный
# план"/"финальный план"/"итоговый план"/"final plan" instead of an explicit
# "will be written" clause. Deliberately an ADJECTIVE immediately before
# "план" (not a bare "план\s+запис" match): "...план записи в Bitrix"
# (already used by ``is_explicit_product_pricing_or_category_refinement_
# request``'s and the enrichment request's own turns, e.g. "...покажи EAN
# ... и план записи в Bitrix.") must keep routing exactly as it does today
# -- only "показать/объяснить the FINAL/CONCLUSIVE plan" is this predicate's
# own, distinct shape.
_WRITE_PLAN_RE = re.compile(
    r"(буд(ет|ут)\s+записан|что\s+именно\s+(будет\s+)?запис|(would|will)\s+be\s+written"
    r"|(оконч|финальн|итогов)\w*\s+план|final\s+(write\s+)?plan)",
    re.I,
)


def _is_write_plan_ask(text: str) -> bool:
    """The "show me what WOULD be written" shape shared by
    ``is_bitrix_write_plan_question`` (which routes it) and
    ``is_explicit_bitrix_write_confirmation`` (which must never mistake it
    for approval)."""
    blob = _norm(text)
    return _has_stem(blob, _WRITE_PLAN_ASK_STEMS) and bool(_WRITE_PLAN_RE.search(blob))


def is_bitrix_write_plan_question(text: str) -> bool:
    """True only for a read-only question about WHAT the already prepared
    product card would write to Bitrix/Aspro -- e.g. "Покажи точно, какие
    данные из этой карточки товара будут записаны в Bitrix/Aspro, если я
    подтвержу запись ... Ничего в Bitrix не записывай." or "Покажи перед
    подтверждением записи окончательный план для этого же товара: ...
    Ничего пока не записывай и не публикуй.". Requires a show/explain ask +
    a Bitrix/Aspro target + either an explicit "будет записано"/"would be
    written" phrase or a "окончательный/финальный/итоговый план"/"final
    plan" phrase in the SAME message, and never matches an actual write
    confirmation."""
    blob = _norm(text)
    if not blob:
        return False
    if is_explicit_bitrix_write_confirmation(blob):
        return False
    # An enrichment request ("Подготовь полную карточку ... Ничего в Bitrix
    # не записывай. Покажи полный предпросмотр...") satisfies all three
    # groups below but must keep running the enrichment pipeline -- there is
    # no prepared card to explain yet.
    if is_explicit_product_enrichment_request(blob):
        return False
    if not _has_stem(blob, _WRITE_PLAN_ASK_STEMS):
        return False
    if not _has_stem(blob, _BITRIX_TARGET_MARKER_STEMS):
        return False
    return bool(_WRITE_PLAN_RE.search(blob))


# Product-first routing defect closure: "покажи, что будет записано на
# сайт" and "покажи план записи этого товара в Bitrix" express the SAME
# business intent -- a read-only preview of what the ALREADY SELECTED
# product would write/publish/upload -- but only the first one satisfied
# ``_is_write_plan_ask``/``_WRITE_PLAN_RE`` (that regex only recognises the
# narrow "будет записан"/"final plan" phrasing). Rather than growing
# ``_WRITE_PLAN_RE`` into an ever-longer phrase list -- which would also
# risk newly matching ``is_bitrix_write_plan_question``'s OWN Bitrix-target
# branch for a brand-new Turn-1 message that attaches a fresh spreadsheet
# AND asks to "покажи ... карточку и план действий перед записью" in the
# SAME breath (Block 5.5's own row-preview request, which has no active
# task yet and must keep routing to plain ingestion) -- this is a
# deliberately SEPARATE, broader predicate used at exactly ONE call site:
# the generic "already active, already-selected product" continuation
# branch below. That branch already requires an active FAMILY_EXCEL task
# with a resolved product (``bitrix_product_fields['sku']``) from an
# EARLIER turn, so it structurally can never fire on a fresh Turn-1
# attachment message -- broadening the vocabulary here is safe precisely
# because the call site's own guard already carries the weight
# ``is_bitrix_write_plan_question``'s Bitrix-target requirement carries for
# its own, narrower use.
#
# Deliberately EXCLUDES a bare "карточка"/"card" concept stem: an existing,
# unrelated feature already answers a plain "Покажи подготовленную
# карточку полностью, включая закупочную цену..." follow-up with the raw
# spreadsheet-row echo (see ``test_panda_xlsx_followup_context_hotfix.py``/
# ``test_panda_xlsx_product_preview_response_hotfix.py``), and that
# behaviour must keep working unchanged -- "карточка" alone never implies
# "the WRITE plan" the way "план"/"запис"/"публикац"/"загруз" do. The
# task's own "покажи карточку перед записью" example still matches via
# "записью" (the "запис" stem), never via "карточку" itself.
_WRITE_PREVIEW_ASK_STEMS = _WRITE_PLAN_ASK_STEMS + (
    "что будет",
    "что собира",
    "что ты собира",
    "что именно будет",
    "what will",
    "what are you going",
    "what're you going",
)
# The write/publish CONCEPT itself, deliberately broad (bare "план",
# "итог"/"результат" included, but NOT "карточка"/"card" -- see the module
# note above) -- safe only in combination with the show-ish gate above AND
# the caller's own active-task guard; see the module note above for why a
# bare "план" mention cannot leak into a fresh Turn-1 attachment turn.
_WRITE_PREVIEW_CONCEPT_STEMS = (
    "запис",
    "отправ",
    "публикац",
    "опубл",
    "план",
    "итог",
    "результат",
    "written",
    "publish",
    "send",
    "sent",
    "plan",
    "result",
)
# "загруз"/"загруж"/"upload" is deliberately NOT in the unconditional set
# above: "загруженный прайс"/"из загруженного файла" (the SOURCE
# spreadsheet was uploaded) is common, ordinary phrasing in this domain
# that has nothing to do with writing the PRODUCT to the site -- see
# ``test_panda_xlsx_product_preview_response_hotfix.py``'s "...из
# загруженного файла" follow-up, which must keep its own, unrelated
# behaviour. "Что будет загружено в Bitrix?" only means the site-write
# concept because it ALSO names the Bitrix/site target in the same
# breath, so this stem only counts combined with that target mention.
_WRITE_PREVIEW_UPLOAD_STEMS = ("загруз", "загруж", "выгруз", "выгруж", "upload")
_WRITE_PREVIEW_SITE_TARGET_STEMS = _BITRIX_TARGET_MARKER_STEMS + ("сайт", "site")


def is_read_only_write_preview_ask(text: str) -> bool:
    """Generic "show me what will be written/published/uploaded" shape --
    the SAME business intent as ``is_bitrix_write_plan_question`` but
    without requiring an explicit "Bitrix"/"Aspro" mention or the narrow
    "будет записан"/"final plan" phrasing, e.g. "покажи план записи
    товара", "что будет загружено в Bitrix", "покажи карточку перед
    записью" (matches via "записью", not the bare "карточку"), "что ты
    собираешься отправить на сайт", "покажи итог перед публикацией", "what
    will be uploaded to the site". Deliberately only
    used at the ONE call site below that already requires an active,
    already-selected product task -- see the module note above for why
    that guard is what makes this safe to broaden this far."""
    blob = _norm(text)
    if not blob:
        return False
    if not _has_stem(blob, _WRITE_PREVIEW_ASK_STEMS):
        return False
    if _has_stem(blob, _WRITE_PREVIEW_CONCEPT_STEMS):
        return True
    return _has_stem(blob, _WRITE_PREVIEW_UPLOAD_STEMS) and _has_stem(blob, _WRITE_PREVIEW_SITE_TARGET_STEMS)


# Batch Bitrix existence-check defect closure: ordinary business phrasing
# for "check the WHOLE uploaded price list against Bitrix -- which rows
# already exist, which are new, which are ambiguous" (e.g. "проверь весь
# прайс перед загрузкой на сайт", "какие из этих товаров уже существуют на
# сайте", "покажи, что будет создано, а что уже есть") -- deliberately a
# SEPARATE predicate from ``is_read_only_write_preview_ask`` (that one is
# scoped to a SINGLE already-selected product; this one is scoped to the
# WHOLE dataset and never requires one to be selected at all). Composable
# semantic stems, not a growing phrase list: an ask/query verb, plus EITHER
# an explicit "already exists/will be created" outcome phrase OR a
# "whole dataset" scope phrase combined with a Bitrix/site/upload target.
_BATCH_CHECK_ASK_STEMS = (
    "проверь",
    "проверить",
    "узнай",
    "узнать",
    "покаж",
    "показать",
    "определи",
    "определить",
    "убедись",
    "какие",
    "что",
    "check",
    "show",
    "which",
)
# Deliberately NOT "дубл"/"duplicate": that root belongs to the EXISTING,
# unrelated in-spreadsheet duplicate finder ("какие товары дублируются в
# файле?" -- ``DataIntelligenceService.duplicates``/``FAMILY_PRODUCT``
# catalog_assist), which must keep its own, unrelated behaviour.
_BATCH_EXISTENCE_STATUS_STEMS = (
    "существ",
    "уже есть",
    "созда",
    "exist",
    "new product",
)
_BATCH_WHOLE_SCOPE_RE = re.compile(
    r"весь\s+прайс|весь\s+файл|всю\s+таблицу"
    r"|все\s+(?:\d+\s+)?товар|все\s+(?:\d+\s+)?позици"
    r"|каждый\s+товар|каждую\s+позицию"
    r"|whole\s+price\s+list|all\s+products|every\s+row|entire\s+file",
    re.I,
)


def is_batch_bitrix_existence_check_request(text: str) -> bool:
    """True for a read-only ask about which rows of the WHOLE uploaded
    price list already exist in Bitrix, which are new, and which are
    ambiguous -- e.g. "Проверь весь прайс перед загрузкой на сайт. Покажи,
    какие товары уже есть в Bitrix". Never a write confirmation (the
    caller checks ``is_explicit_bitrix_write_confirmation`` first and this
    predicate additionally refuses any message that satisfies it)."""
    blob = _norm(text)
    if not blob:
        return False
    if is_explicit_bitrix_write_confirmation(blob):
        return False
    if not _has_stem(blob, _BATCH_CHECK_ASK_STEMS):
        return False
    if _has_stem(blob, _BATCH_EXISTENCE_STATUS_STEMS):
        return True
    if not _BATCH_WHOLE_SCOPE_RE.search(blob):
        return False
    return _has_stem(blob, _WRITE_PREVIEW_SITE_TARGET_STEMS) or _has_stem(blob, _WRITE_PREVIEW_UPLOAD_STEMS)


# Production defect closure (business-process ownership: a retail-price
# FORMULA instruction, e.g. "установи розничную цену как закупочная + 7%",
# misread as an immediate write/publish command): ``business_assistant.
# intent.is_conversational``'s attachment/active-task continuation gates
# each carry their own small "explicit immediate-write/publish verb"
# word list ("измени"/"установ"/"опубликуй"/"publish all") so a genuine
# out-of-finger write command (e.g. "Измени цену и опубликуй все товары
# ... на сайт") still overrides continuation ownership and keeps routing
# to the legacy governed business-workflow engine unchanged. That list is
# a crude proxy: the stem "установ" ALSO matches an ordinary "set/
# establish this pricing INPUT" instruction for the SAME already-active
# (or just-starting) Bitrix/Aspro product task, which is never a write
# command at all -- especially when the SAME message already says so
# explicitly ("Ничего в Bitrix пока не записывай."). Rather than adding
# yet another phrase-specific predicate to the growing ``is_explicit_*``
# family above, expose the ONE existing, canonical Bitrix-target +
# no-write-negation signal ``is_explicit_bitrix_write_confirmation``/
# ``is_bitrix_write_plan_question`` already rely on (``_BITRIX_NO_WRITE_RE``
# + ``_BITRIX_TARGET_MARKER_STEMS``) as a small public override those two
# gates can reuse directly. Scoped to an explicit Bitrix/Aspro target so a
# DIFFERENT-domain write command (no "Bitrix"/"Aspro" mention at all, e.g.
# the marketplace-publish example above) is entirely unaffected.
def has_explicit_bitrix_no_write_qualifier(text: str) -> bool:
    """True when the message explicitly targets Bitrix/Aspro AND explicitly
    says not to write/publish there yet (e.g. "...подготовь для Bitrix/
    Aspro... Ничего в Bitrix пока не записывай."). Used to override the
    coarse "explicit write/publish verb" exclusion in ``business_assistant.
    intent.is_conversational``'s continuation-ownership gates -- never to
    grant write approval itself (see ``is_explicit_bitrix_write_confirmation``
    for that, separate, decision)."""
    blob = _norm(text)
    if not blob:
        return False
    if not _has_stem(blob, _BITRIX_TARGET_MARKER_STEMS):
        return False
    return bool(_BITRIX_NO_WRITE_RE.search(blob))


# Product-first defect closure: a "site-ready product card" is the default
# outcome of ANY Bitrix write-plan preview/write for an already-selected
# single product (see ``WorkflowPandaConversationGateway.
# _auto_prepare_site_ready_card_if_needed``) -- the user never has to say
# "enrichment"/"обогащение"/"SEO"/"характеристики"/"галерея" for the
# existing ``product_enrichment_bridge`` pipeline to run. These three
# stem groups are the ONLY way the user can narrow that default, by
# EXPLICITLY naming the stage(s) to skip in the SAME message that asks for
# the preview/write -- never inferred from silence, never phrase-specific
# to any one "show the plan" wording.
_PRICE_LIST_ONLY_STEMS = (
    "только данные из прайса",
    "только из прайса",
    "только цену и артикул",
    "только цена и артикул",
    "только цену, артикул",
    "ничего не ищи",
    "не ищи ничего",
    "price list only",
    "only the price list",
    "only price and sku",
    "only the price and sku",
)
# A compound negation ("без картинок и описания") shares ONE "без" across
# both nouns -- a plain stem substring check ("без описан" as a literal
# phrase) never matches the second noun in that shape, so this allows up
# to two words (and an optional "и"/"and" conjunction) between "без"/
# "without" and the target noun itself. Still anchored on an explicit
# negation marker immediately in front -- never a bare "картинки"/
# "описание" mention alone (e.g. describing what the CARD contains, not
# what to omit).
_NO_MEDIA_RE = re.compile(
    r"без\s+(?:\w+[,]?\s+){0,2}(?:и\s+)?(?:картин\w*|фото\w*|изображен\w*)"
    r"|without\s+(?:\w+[,]?\s+){0,2}(?:and\s+)?(?:images?|pictures?|photos?)"
    r"|no\s+images?|no\s+pictures?|no\s+photos?",
    re.I,
)
_NO_DESCRIPTION_RE = re.compile(
    r"без\s+(?:\w+[,]?\s+){0,2}(?:и\s+)?описан\w*"
    r"|without\s+(?:\w+[,]?\s+){0,2}(?:and\s+)?descriptions?"
    r"|no\s+description",
    re.I,
)


def has_explicit_price_list_only_constraint(text: str) -> bool:
    """True when the user explicitly limits the card to raw price-list
    data only (e.g. "только данные из прайса", "ничего не ищи") -- the
    whole auto-preparation stage is skipped outright and the card stays
    exactly what the spreadsheet row itself supplied (identity + price),
    same as before this defect closure."""
    return _has_stem(text, _PRICE_LIST_ONLY_STEMS)


def has_explicit_no_media_constraint(text: str) -> bool:
    """True when the user explicitly excludes images (e.g. "без картинок",
    "без картинок и описания") -- media acquisition is skipped, every
    other stage still runs."""
    blob = _norm(text)
    return bool(blob) and bool(_NO_MEDIA_RE.search(blob))


def has_explicit_no_description_constraint(text: str) -> bool:
    """True when the user explicitly excludes descriptions (e.g. "без
    описания", "без картинок и описания") -- the short/detailed
    description text is stripped from the prepared card, every other
    stage still runs."""
    blob = _norm(text)
    return bool(blob) and bool(_NO_DESCRIPTION_RE.search(blob))


def _extract_confirmed_retail_price(text: str) -> str:
    match = _CONFIRMED_RETAIL_PRICE_RE.search(text or "")
    if not match:
        return ""
    return normalize_decimal_string(match.group(2)) or ""


def _is_unrelated_new_task(text: str) -> bool:
    if requires_business_integration(text):
        return True
    if _has_stem(text, _WEATHER_STEMS):
        return True
    if _has_stem(text, _QUESTION_NEW_STEMS) and not _is_image_artifact_request(text):
        return True
    req = derive_task_requirements(category="general", text=text)
    if str(getattr(req, "freshness", "") or "") == FRESHNESS_CURRENT:
        return True
    return False


def _extract_quantity(text: str) -> int | None:
    blob = _norm(text)
    if _has_stem(blob, _SEVERAL_STEMS):
        return None
    match = re.search(r"\b(\d{1,2})\b", blob)
    if match:
        value = int(match.group(1))
        if 1 <= value <= 16:
            return value
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "один": 1,
        "одна": 1,
        "два": 2,
        "две": 2,
        "три": 3,
    }
    for token in blob.split():
        if token in words:
            return words[token]
    return None


def _is_quantity_only(text: str) -> bool:
    blob = _norm(text).strip(" !.?,")
    if re.fullmatch(r"\d{1,2}", blob):
        return True
    if blob in {"один", "одна", "два", "две", "три", "one", "two", "three"}:
        return True
    return bool(re.fullmatch(r"(сделай|make)\s+\d{1,2}", blob))


def _strip_scene_wrappers(text: str) -> str:
    blob = (text or "").strip()
    blob = re.sub(
        r"^\s*(please|пожалуйста)\s+",
        "",
        blob,
        flags=re.I,
    )
    blob = re.sub(
        r"\b(сделай(те)?|сделать|create|make|draw|generate|сгенерируй|сгененрировать|"
        r"сгенерировать|нарисуй|создай|создать)\b",
        " ",
        blob,
        flags=re.I,
    )
    blob = re.sub(
        r"\b(мне|для\s+меня|please|a|an|the|with|с|картинк\w*|изображени\w*|"
        r"изоброжени\w*|фото\w*|image\w*|picture\w*|photo\w*|logo|логотип)\b",
        " ",
        blob,
        flags=re.I,
    )
    blob = re.sub(r"\s+", " ", blob).strip(" ,.-")
    return blob


def _merge_scene(existing: str, addition: str, *, replace_time: bool = False) -> str:
    base = (existing or "").strip()
    extra = (addition or "").strip()
    if not extra:
        return base
    extra_norm = _norm(extra)
    if replace_time:
        parts = [
            tok
            for tok in re.split(r"\s+", base)
            if tok and not _has_stem(tok, _NIGHT_STEMS + _DAY_STEMS)
        ]
        base = " ".join(parts)
    if extra_norm and extra_norm in _norm(base):
        return base
    return f"{base} {extra}".strip()


def _apply_style(parameters: dict[str, Any], text: str) -> None:
    if _has_stem(text, _LOGO_STEMS):
        parameters[PARAM_STYLE] = "logo"
    if _has_stem(text, _REALISM_STEMS):
        parameters[PARAM_STYLE] = "realistic"
    if _has_stem(text, _SQUARE_STEMS):
        parameters[PARAM_ASPECT] = "1:1"


def _apply_time_setting(parameters: dict[str, Any], text: str, *, correction: bool) -> None:
    scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _NIGHT_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "night", replace_time=correction or _has_stem(scene, _DAY_STEMS))
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _DAY_STEMS) and not _has_stem(text, _NIGHT_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "day", replace_time=True)
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _FOREST_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "forest")
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _WOLF_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "wolf")


def detect_family(
    text: str, active: ActiveTask | None, *, has_spreadsheet_attachment: bool = False
) -> str | None:
    raw = text or ""
    # PANDA -- first controlled production Bitrix product write (PR #43):
    # checked before every other signal (including the generic FAMILY_WRITE
    # stems below) -- an explicit, self-confirming Bitrix create instruction
    # must always win, never be reclassified as an unrelated write stub or
    # folded into an active FAMILY_EXCEL continuation.
    if is_explicit_bitrix_write_confirmation(raw):
        return FAMILY_BITRIX_PRODUCT_WRITE
    if _has_stem(raw, _WRITE_STEMS):
        return FAMILY_WRITE
    # Block 5.5: an explicit "build a product catalog" verb wins over the
    # bare spreadsheet-attachment signal below -- the user wants canonical
    # product/catalog intelligence (import + field mapping + normalization),
    # not a generic Excel analysis/transform turn.
    if _has_stem(raw, _PRODUCT_CATALOG_STEMS):
        return FAMILY_PRODUCT
    # Block 5.1 chat integration (spec section 12): the user never selects an
    # "Excel mode" -- attaching a spreadsheet is itself the strongest, fully
    # deterministic signal, regardless of the accompanying wording.
    if has_spreadsheet_attachment:
        return FAMILY_EXCEL
    # Block 5.3: an explicit content-creation verb ("write an article about
    # <url>") wins over the bare-URL acquisition signal below -- the user is
    # asking for drafted content, not a raw record extraction, even when a
    # source URL is included as research input.
    if _has_stem(raw, _CONTENT_INTENT_STEMS):
        return FAMILY_CONTENT
    # Block 5.2: an explicit URL always wins over any active task -- pasting
    # a new link is a deliberate "acquire this" signal (spec section 20),
    # stronger than a generic active-family continuation heuristic.
    if _extract_url(raw):
        return FAMILY_ACQUISITION
    if _has_stem(raw, _ACQUISITION_INTENT_STEMS):
        return FAMILY_ACQUISITION
    if _has_stem(raw, _EXCEL_STEMS) and (
        _has_stem(raw, ("анализ", "analyze", "inspect", "проанализ")) or _has_stem(raw, _MAKE_STEMS)
    ):
        return FAMILY_EXCEL
    if _has_stem(raw, _PRODUCT_INTENT_STEMS):
        return FAMILY_PRODUCT
    if active is not None and active.family == FAMILY_PRODUCT:
        # Block 5.5 multi-turn continuation: short deterministic follow-ups
        # ("Только по артикулам", "Сохрани каталог") keep the active product
        # task alive, mirroring FAMILY_EXCEL's continuation heuristic.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_PRODUCT
    if active is not None and active.family == FAMILY_EXCEL:
        # Block 5.1 multi-turn continuation (spec section 13): legitimate
        # follow-ups ("Оставь Samsung", "Только дешевле 50000", "Минус 12%",
        # "Сохрани Excel") are short deterministic data commands that often
        # ARE business/data-domain keywords ("прайс", "samsung", "excel")
        # themselves -- unlike image continuation, domain-keyword presence
        # must not be treated as evidence of an unrelated new task here.
        # Only a genuinely different top-level ask (weather/general trivia)
        # breaks continuation.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_EXCEL
    if active is not None and active.family == FAMILY_ACQUISITION:
        # Continuation while still waiting for the URL (clarification asked
        # last turn) -- keep the frame alive for a plain follow-up reply.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_ACQUISITION
    if active is not None and active.family == FAMILY_CONTENT:
        # Continuation while still waiting for the objective/topic, or a
        # short follow-up refining the same content request.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_CONTENT
    if _is_image_artifact_request(raw) or (
        _is_image_execute_verb(raw) and (active is None or active.family == FAMILY_IMAGE_GENERATE)
    ):
        return FAMILY_IMAGE_GENERATE
    if active is not None and active.family == FAMILY_IMAGE_GENERATE:
        if _is_image_execute_verb(raw) or _is_quantity_only(raw) or _has_stem(raw, _SEVERAL_STEMS):
            return FAMILY_IMAGE_GENERATE
        if _token_count(raw) <= 8 and not _is_unrelated_new_task(raw):
            return FAMILY_IMAGE_GENERATE
    if _has_stem(raw, _SEARCH_STEMS) and _is_unrelated_new_task(raw):
        return FAMILY_SEARCH
    if _has_stem(raw, _DOC_STEMS) and _has_stem(raw, _MAKE_STEMS + _IMAGE_VERB_STEMS):
        return FAMILY_DOCUMENT
    return None


def continuation_decision(
    text: str,
    *,
    active: ActiveTask | None,
    follow_up: FollowUpResolution | None = None,
    has_spreadsheet_attachment: bool = False,
) -> str:
    if active is None:
        return NEW_TASK
    family = detect_family(text, active, has_spreadsheet_attachment=has_spreadsheet_attachment)
    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        if not family:
            return NEW_TASK
    # Block 5.1: an Excel-continuation turn commonly contains business/data
    # keywords ("прайс", "samsung", "excel") that would otherwise look like
    # an unrelated new top-level business-integration task (see
    # requires_business_integration._BUSINESS_TASK_KEYWORDS) -- once
    # detect_family has already deterministically resolved this turn to the
    # active Excel task, that heuristic must not override it.
    if (
        family not in {FAMILY_EXCEL, FAMILY_ACQUISITION, FAMILY_CONTENT, FAMILY_PRODUCT}
        and _is_unrelated_new_task(text)
        and not _is_quantity_only(text)
    ):
        return NEW_TASK
    if family == FAMILY_WRITE:
        return NEW_TASK
    if family and family != active.family and family not in {FAMILY_IMAGE_EDIT, FAMILY_IMAGE_GENERATE}:
        if family == FAMILY_IMAGE_GENERATE and active.family == FAMILY_IMAGE_EDIT:
            return CONTINUE_ACTIVE_TASK
        if family == FAMILY_IMAGE_EDIT and active.family == FAMILY_IMAGE_GENERATE:
            return CONTINUE_ACTIVE_TASK
        return NEW_TASK
    if family == active.family or family is None:
        if _token_count(text) <= 10 or family == active.family:
            return CONTINUE_ACTIVE_TASK
    if _token_count(text) <= 2:
        return AMBIGUOUS
    return NEW_TASK


def _contract_for(family: str | None) -> CapabilityContract | None:
    if not family:
        return None
    return CONTRACTS.get(family)


def compute_readiness(task: ActiveTask) -> str:
    if task.status == STATUS_CANCELLED:
        return NOT_EXECUTABLE
    if task.risk == RISK_WRITE:
        return NEEDS_APPROVAL
    contract = _contract_for(task.family)
    if contract is None:
        return CONVERSATIONAL_ONLY
    missing: list[str] = []
    params = dict(task.parameters)
    for key in contract.required:
        value = params.get(key)
        if value in (None, "", [], ()):
            missing.append(key)
    if task.awaiting_quantity and not task.quantity:
        missing.append(PARAM_QUANTITY)
    task.missing_required = tuple(missing)
    if missing:
        return NEEDS_REQUIRED_INPUT
    return READY_TO_EXECUTE


def _apply_defaults(task: ActiveTask) -> dict[str, Any]:
    contract = _contract_for(task.family)
    args = dict(task.parameters)
    if contract is None:
        return args
    for key, value in dict(contract.defaults).items():
        args.setdefault(key, value)
    if task.quantity:
        args[PARAM_QUANTITY] = int(task.quantity)
    else:
        args.setdefault(PARAM_QUANTITY, int(contract.defaults.get(PARAM_QUANTITY, 1) or 1))
    scene = str(args.get(PARAM_SCENE) or "").strip()
    style = str(args.get(PARAM_STYLE) or "").strip()
    if style and style not in scene.casefold():
        args[PARAM_SCENE] = f"{scene} {style}".strip()
    args["prompt"] = str(args.get(PARAM_SCENE) or "")
    return args


def _clarification_for(task: ActiveTask) -> str:
    missing = task.missing_required
    if PARAM_QUANTITY in missing:
        return "Сколько вариантов?"
    if PARAM_SCENE in missing or "instruction" in missing:
        return "Что нужно сделать?"
    if PARAM_FILE in missing:
        return "Приложите файл."
    if missing:
        return "Нужен ещё один обязательный параметр."
    return "Уточните, пожалуйста."


def _bind_image_params(task: ActiveTask, text: str, *, correction: bool) -> None:
    _apply_style(task.parameters, text)
    qty = _extract_quantity(text)
    if _has_stem(text, _SEVERAL_STEMS) and qty is None:
        task.awaiting_quantity = True
        task.quantity = None
        task.parameters.pop(PARAM_QUANTITY, None)
    elif qty is not None:
        task.quantity = qty
        task.parameters[PARAM_QUANTITY] = qty
        task.awaiting_quantity = False
    _apply_time_setting(task.parameters, text, correction=correction)
    remainder = _strip_scene_wrappers(text)
    skip_remainder = (
        _is_quantity_only(text)
        or _has_stem(text, _SEVERAL_STEMS)
        or _has_stem(text, _CORRECTION_STEMS)
    )
    if remainder and not skip_remainder:
        if _has_stem(remainder, _LOGO_STEMS) and _token_count(remainder) <= 2:
            pass
        else:
            task.parameters[PARAM_SCENE] = _merge_scene(
                str(task.parameters.get(PARAM_SCENE) or ""),
                remainder,
                replace_time=correction,
            )
    if _is_image_execute_verb(text) or _has_stem(text, _MAKE_STEMS):
        task.execute_requested = True
    task.goal = str(task.parameters.get(PARAM_SCENE) or task.goal)


def _new_image_task(*, tenant_id: str, owner_id: str, conversation_id: str, text: str) -> ActiveTask:
    task = ActiveTask(
        task_id=str(uuid.uuid4()),
        tenant_id=require_tenant_id(tenant_id),
        owner_id=str(owner_id or ""),
        conversation_id=str(conversation_id or ""),
        family=FAMILY_IMAGE_GENERATE,
        tool_id=IMAGE_GENERATE_CONTRACT.tool_id,
        operation=IMAGE_GENERATE_CONTRACT.operation,
        goal=_strip_scene_wrappers(text) or text,
        artifact_type="image",
        status=STATUS_DRAFT,
        risk=RISK_GENERATE,
        execute_requested=_is_image_execute_verb(text) or _has_stem(text, _MAKE_STEMS),
    )
    _bind_image_params(task, text, correction=False)
    return task


def user_unavailable_message(family: str) -> str:
    if family == FAMILY_IMAGE_GENERATE:
        return "Сейчас не могу создать изображение — возможность генерации недоступна."
    if family == FAMILY_EXCEL:
        return "Сейчас не могу обработать таблицу — возможность недоступна."
    if family == FAMILY_DOCUMENT:
        return "Сейчас не могу создать документ — возможность недоступна."
    if family == FAMILY_ACQUISITION:
        return "Сейчас не могу собрать данные со страницы — возможность недоступна."
    if family == FAMILY_CONTENT:
        return "Сейчас не могу сгенерировать текст — возможность недоступна."
    if family == FAMILY_PRODUCT:
        return "Сейчас не могу обработать каталог товаров — возможность недоступна."
    return "Эта возможность сейчас недоступна."


def _user_tool_error() -> str:
    return "Не получилось выполнить действие. Можно повторить запрос."


def _user_success_image() -> str:
    return "Готово."


def inspect_capability(gateway, tool_id: str) -> str:
    if gateway is None:
        return CAPABILITY_UNAVAILABLE
    try:
        descriptor = gateway.get_tool(tool_id)
    except Exception:
        return CAPABILITY_UNAVAILABLE
    if descriptor is None:
        return CAPABILITY_UNAVAILABLE
    if not bool(getattr(descriptor, "enabled", False)):
        return CAPABILITY_UNAVAILABLE
    return CAPABILITY_AVAILABLE_AND_AUTHORIZED


def _bitrix_missing_context_decision(active: ActiveTask | None) -> ActionDecision:
    return ActionDecision(
        decision=ANSWER_TEXT,
        readiness=NOT_EXECUTABLE,
        continuation=NEW_TASK,
        task=active,
        user_message=(
            "Не вижу подготовленной карточки товара для записи в Bitrix. "
            "Сначала приложите файл и попросите подготовить карточку конкретного "
            "товара, а затем подтвердите его создание."
        ),
        extra_llm=False,
    )


def _batch_bitrix_missing_dataset_decision(active: ActiveTask | None) -> ActionDecision:
    return ActionDecision(
        decision=ANSWER_TEXT,
        readiness=NOT_EXECUTABLE,
        continuation=NEW_TASK,
        task=active,
        user_message=(
            "Не вижу загруженного прайс-листа для проверки по Bitrix. "
            "Сначала приложите файл, затем попросите проверить товары по Bitrix."
        ),
        extra_llm=False,
    )


def _enrichment_missing_context_decision(active: ActiveTask | None) -> ActionDecision:
    return ActionDecision(
        decision=ANSWER_TEXT,
        readiness=NOT_EXECUTABLE,
        continuation=NEW_TASK,
        task=active,
        user_message=(
            "Не вижу товара для подготовки полной карточки. Сначала приложите "
            "файл и уточните конкретный товар (например, его артикул), затем "
            "попросите подготовить полную карточку."
        ),
        extra_llm=False,
    )


def _enrichment_needs_excel_ingestion_first(
    active: ActiveTask | None, has_spreadsheet_attachment: bool
) -> bool:
    """True when an explicit "Подготовь полную карточку товара <SKU>..."
    request carries its OWN spreadsheet on THIS turn but has no
    FAMILY_EXCEL context yet -- i.e. the attachment and the enrichment
    instruction arrived in the SAME message, so nothing has been ingested
    and ``resolve_product_enrichment_request`` would fail closed on its
    ``active is None`` guard without ever looking at the attachment. Such a
    turn must first go through the existing FAMILY_EXCEL ingestion/row
    lookup (exactly what a "Найди товар <SKU>..." turn does) before
    enrichment can resolve a product."""
    if not has_spreadsheet_attachment:
        return False
    return active is None or active.family != FAMILY_EXCEL


def _pricing_category_refinement_needs_excel_ingestion_first(
    active: ActiveTask | None, has_spreadsheet_attachment: bool
) -> bool:
    """True when an explicit "...возьми первый товар из <файл> и подготовь
    его для Bitrix/Aspro: ... рассчитанную розничную цену, точную
    категорию Bitrix/Aspro..." request carries its OWN spreadsheet on
    THIS turn but has no FAMILY_EXCEL context yet -- i.e. the attachment
    and the pricing/category refinement instruction arrived in the SAME
    (first) message of a brand-new conversation, so nothing has been
    ingested yet and ``resolve_product_pricing_category_refinement_
    request`` would fail closed on its ``active is None`` guard without
    ever looking at the attachment. Mirrors ``_enrichment_needs_excel_
    ingestion_first`` exactly. Such a turn must first go through the
    existing FAMILY_EXCEL ingestion/row lookup (exactly what a bare
    "Возьми первый товар из <файл>..." turn does) before the pricing/
    category refinement can resolve a product."""
    if not has_spreadsheet_attachment:
        return False
    return active is None or active.family != FAMILY_EXCEL


def resolve_product_enrichment_request(
    text: str,
    *,
    active: ActiveTask | None,
    store: ActiveTaskStore,
    request_id: str = "",
) -> ActionDecision:
    """Deterministic routing for "Подготовь полную карточку товара..."
    (product enrichment pipeline follow-up). Resolves the SAME
    previously-previewed product from the active FAMILY_EXCEL task's
    ``parameters['bitrix_product_fields']`` -- mirrors
    ``resolve_bitrix_write_confirmation`` exactly, but dispatches
    ``CALL_PRODUCT_ENRICHMENT`` (never mutates Bitrix) instead of
    ``CALL_CONTROLLED_BITRIX_WRITE``."""
    if active is None or active.family != FAMILY_EXCEL:
        return _enrichment_missing_context_decision(active)

    fields = dict(active.parameters.get("bitrix_product_fields") or {})
    if not fields.get("title") or not fields.get("sku"):
        # Production defect closure: the prior upload turn may have only
        # parsed/analyzed the spreadsheet (e.g. "В таблице 13 строк и 8
        # столбцов...") WITHOUT naming a specific row -- no ROW_FOUND yet,
        # so bitrix_product_fields was never persisted -- while THIS
        # enrichment turn names the exact SKU/EAN itself ("...карточку
        # товара LG 55MRGB86B6A.ARUG из загруженного прайса..."). Rather
        # than giving up, reuse the EXISTING, unchanged FAMILY_EXCEL
        # row-lookup tool call (data.excel_assistant/assist) against the
        # already-parsed dataset_id -- never re-parsing/re-uploading.
        # ``chain_to_enrichment_text`` tells the gateway to re-resolve this
        # SAME enrichment request immediately after, so the previously
        # parsed product context is restored and CALL_PRODUCT_ENRICHMENT
        # still fires within this one turn when the lookup succeeds.
        dataset_id = str(active.parameters.get("dataset_id") or "")
        if not dataset_id:
            return _enrichment_missing_context_decision(active)
        lookup_args = {"text": text, "dataset_id": dataset_id}
        lookup_idem = _idempotency_key(request_id, "data.excel_assistant.assist", lookup_args)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            arguments=lookup_args,
            tool_id=EXCEL_CONTRACT.tool_id,
            operation=EXCEL_CONTRACT.operation,
            extra_llm=False,
            capability_status=CAPABILITY_AVAILABLE_AND_AUTHORIZED,
            idempotency_key=lookup_idem,
            chain_to_enrichment_text=text,
        )

    retail_price = str(active.parameters.get("bitrix_retail_price_preview") or "")
    args = {
        "product_fields": fields,
        "retail_price": retail_price,
        "dataset_id": str(active.parameters.get("dataset_id") or ""),
    }
    idem = _idempotency_key(request_id, "product_enrichment.prepare_complete_card", args)
    active.status = STATUS_READY
    store.put(active)
    return ActionDecision(
        decision=CALL_PRODUCT_ENRICHMENT,
        readiness=READY_TO_EXECUTE,
        continuation=CONTINUE_ACTIVE_TASK,
        task=active,
        arguments=args,
        tool_id="product_enrichment.prepare_complete_card",
        operation="prepare_complete_card",
        extra_llm=False,
        capability_status=CAPABILITY_AVAILABLE_AND_AUTHORIZED,
        idempotency_key=idem,
    )


def resolve_bitrix_write_confirmation(
    text: str,
    *,
    active: ActiveTask | None,
    store: ActiveTaskStore,
    request_id: str = "",
) -> ActionDecision:
    """Deterministic routing for an already-confirmed Bitrix product create
    (PANDA -- first controlled production Bitrix product write, PR #43).

    Resolves the SAME previously-previewed product from the active
    FAMILY_EXCEL task's ``parameters['bitrix_product_fields']`` -- persisted
    by ``WorkflowPandaConversationGateway._invoke_tool`` right after a
    ``data.excel_assistant`` ROW_FOUND preview -- never from the confirmation
    text alone. If that context is missing, this NEVER writes; it asks the
    user to prepare/select the product first (spec requirement 8)."""
    if active is None or active.family != FAMILY_EXCEL:
        return _bitrix_missing_context_decision(active)

    fields = dict(active.parameters.get("bitrix_product_fields") or {})
    if not fields.get("title") or not fields.get("sku"):
        return _bitrix_missing_context_decision(active)

    retail_price = _extract_confirmed_retail_price(text) or str(
        active.parameters.get("bitrix_retail_price_preview") or ""
    )
    if not retail_price:
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=NEEDS_REQUIRED_INPUT,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            user_message="Укажите розничную цену для подтверждения записи в Bitrix.",
            extra_llm=False,
        )

    args = {
        "product_fields": fields,
        "retail_price": retail_price,
        "dataset_id": str(active.parameters.get("dataset_id") or ""),
    }
    idem = _idempotency_key(request_id, "bitrix.controlled_product_write", args)
    active.status = STATUS_READY
    store.put(active)
    return ActionDecision(
        decision=CALL_CONTROLLED_BITRIX_WRITE,
        readiness=READY_TO_EXECUTE,
        continuation=CONTINUE_ACTIVE_TASK,
        task=active,
        arguments=args,
        tool_id="bitrix.controlled_product_write",
        operation="execute_single_product_write",
        extra_llm=False,
        capability_status=CAPABILITY_AVAILABLE_REQUIRES_APPROVAL,
        idempotency_key=idem,
    )


def resolve_bitrix_write_plan_question(
    text: str,
    *,
    active: ActiveTask | None,
    store: ActiveTaskStore,
    request_id: str = "",
) -> ActionDecision:
    """Deterministic routing for the read-only "покажи, что именно будет
    записано в Bitrix" follow-up (production defect closure). Answers from
    whichever prepared-product state the active task already carries:
    preferably the richer enrichment state a prior ``CALL_PRODUCT_
    ENRICHMENT`` turn persisted (``bitrix_enrichment_write_request``), but
    -- production defect closure (PR #67 follow-up) -- Turn 1 does not
    always run enrichment first; a plain row-preview/single-product
    Bitrix-prep turn (``data_intel.service._row_lookup_result``'s
    ``ROW_FOUND``) only ever persists the flatter ``bitrix_product_fields``/
    ``bitrix_retail_price_preview`` pair. Falls back to building the
    canonical write request from THAT state instead -- the SAME existing,
    already-proven fallback ``resolve_product_pricing_category_refinement_
    request`` already uses via ``build_write_request_from_fields`` -- so
    this question is answered from whatever was actually prepared, never
    forcing a re-attachment/re-enrichment the user never asked for. Never
    re-runs ingestion or enrichment, never writes. Without EITHER state
    there is nothing to explain, so this asks for the card to be prepared
    first (same fail-closed shape as ``resolve_bitrix_write_confirmation``).
    """
    enrichment_write_request = dict(active.parameters.get("bitrix_enrichment_write_request") or {}) if active else {}
    if enrichment_write_request:
        args = {
            "write_request": enrichment_write_request,
            "characteristic_status": dict(active.parameters.get("bitrix_enrichment_characteristic_status") or {}),
            "enrichment_preview": dict(active.parameters.get("bitrix_enrichment_preview") or {}),
            "retail_price": str(active.parameters.get("bitrix_retail_price_preview") or ""),
        }
    else:
        if active is None or active.family != FAMILY_EXCEL:
            return _bitrix_missing_context_decision(active)
        fields = dict(active.parameters.get("bitrix_product_fields") or {})
        if not fields.get("title") or not fields.get("sku"):
            return _bitrix_missing_context_decision(active)

        from business_assistant.controlled_bitrix_write import build_write_request_from_fields
        from business_assistant.product_enrichment_bridge import serialize_write_request

        retail_price = str(active.parameters.get("bitrix_retail_price_preview") or "")
        write_request = build_write_request_from_fields(fields, tenant_id=active.tenant_id, retail_price=retail_price)
        args = {
            "write_request": serialize_write_request(write_request),
            "characteristic_status": {},
            "enrichment_preview": {},
            "retail_price": retail_price,
        }
    return ActionDecision(
        decision=EXPLAIN_BITRIX_WRITE_PLAN,
        readiness=READY_TO_EXECUTE,
        continuation=CONTINUE_ACTIVE_TASK,
        task=active,
        arguments=args,
        operation="prepare_single_product_write",
        extra_llm=False,
        capability_status=CAPABILITY_AVAILABLE_AND_AUTHORIZED,
        idempotency_key=_idempotency_key(request_id, "bitrix.write_plan_explain", args),
    )


def resolve_batch_bitrix_existence_check(
    text: str,
    *,
    active: ActiveTask | None,
    store: ActiveTaskStore,
    request_id: str = "",
) -> ActionDecision:
    """Deterministic routing for the read-only "check the WHOLE uploaded
    price list against Bitrix" ask (batch existence-check defect closure).
    Requires only that a spreadsheet dataset already exists on the active
    FAMILY_EXCEL task -- never a single already-selected product (unlike
    ``resolve_bitrix_write_plan_question``). The actual per-row
    ``BitrixProductBridge.plan_sync`` classification runs in
    ``WorkflowPandaConversationGateway._check_bitrix_existence_batch``,
    which alone holds both the ``ToolGateway`` (to read dataset rows) and
    the ``BitrixProductBridge`` (to check them) -- this resolver only
    decides that the turn IS this batch check and carries the dataset id
    forward; it never touches Bitrix or the dataset itself."""
    if active is None or active.family != FAMILY_EXCEL:
        return _batch_bitrix_missing_dataset_decision(active)
    dataset_id = str(active.parameters.get("dataset_id") or "")
    if not dataset_id:
        return _batch_bitrix_missing_dataset_decision(active)
    args = {"dataset_id": dataset_id}
    return ActionDecision(
        decision=CHECK_BITRIX_EXISTENCE_BATCH,
        readiness=READY_TO_EXECUTE,
        continuation=CONTINUE_ACTIVE_TASK,
        task=active,
        arguments=args,
        operation="check_bitrix_existence_batch",
        extra_llm=False,
        capability_status=CAPABILITY_AVAILABLE_AND_AUTHORIZED,
        idempotency_key=_idempotency_key(request_id, "bitrix.existence_check_batch", args),
    )


def resolve_product_pricing_category_refinement_request(
    text: str,
    *,
    active: ActiveTask | None,
    store: ActiveTaskStore,
    request_id: str = "",
) -> ActionDecision:
    """Deterministic routing for "Рассчитай розничную цену... и определи
    точную категорию Bitrix/Aspro..." (Turn-3 pricing/category refinement
    production defect closure). Resolves the SAME previously-previewed
    product from the active FAMILY_EXCEL task's ``parameters[
    'bitrix_product_fields']``/``bitrix_retail_price_preview`` -- exactly
    like ``resolve_bitrix_write_confirmation`` -- reuses ONLY an already
    known/persisted retail price (never derives a new one; no such
    calculator exists in this codebase), builds the canonical write
    request through the EXISTING ``build_write_request_from_fields``, and
    dispatches the EXISTING ``EXPLAIN_BITRIX_WRITE_PLAN`` decision so the
    EXISTING, unchanged ``_explain_bitrix_write_plan`` handler -- which
    already calls the EXISTING, read-only category resolver
    (``prepare_single_product_write`` -> ``schema.resolve_section_id``) --
    renders the updated card + write plan. Never writes to Bitrix."""
    if active is None or active.family != FAMILY_EXCEL:
        return _bitrix_missing_context_decision(active)

    fields = dict(active.parameters.get("bitrix_product_fields") or {})
    if not fields.get("title") or not fields.get("sku"):
        return _bitrix_missing_context_decision(active)

    retail_price = _extract_confirmed_retail_price(text) or str(
        active.parameters.get("bitrix_retail_price_preview") or ""
    )

    from business_assistant.controlled_bitrix_write import build_write_request_from_fields
    from business_assistant.product_enrichment_bridge import serialize_write_request

    write_request = build_write_request_from_fields(fields, tenant_id=active.tenant_id, retail_price=retail_price)
    args = {
        "write_request": serialize_write_request(write_request),
        "characteristic_status": {},
        "enrichment_preview": {},
        "retail_price": retail_price,
    }
    return ActionDecision(
        decision=EXPLAIN_BITRIX_WRITE_PLAN,
        readiness=READY_TO_EXECUTE,
        continuation=CONTINUE_ACTIVE_TASK,
        task=active,
        arguments=args,
        operation="prepare_single_product_write",
        extra_llm=False,
        capability_status=CAPABILITY_AVAILABLE_AND_AUTHORIZED,
        idempotency_key=_idempotency_key(request_id, "bitrix.pricing_category_refinement_explain", args),
    )


def resolve_action_turn(
    text: str,
    *,
    tenant_id: str,
    owner_id: str,
    conversation_id: str,
    store: ActiveTaskStore,
    follow_up: FollowUpResolution | None = None,
    gateway=None,
    request_id: str = "",
    # Block 5.1: deterministic count of THIS turn's attachments already
    # resolved (trusted, tenant/conversation-verified) to kind=="spreadsheet"
    # -- never a raw/unresolved ref count. 0 for every pre-5.1 caller/test
    # (default), so existing image/document/search routing is unaffected.
    spreadsheet_attachment_count: int = 0,
    # Private recursion guard for the one-turn "spreadsheet attachment +
    # explicit enrichment request in the SAME message" dispatch below.
    # Never set by external callers.
    _skip_enrichment_dispatch: bool = False,
    # Private recursion guard for the one-turn "spreadsheet attachment +
    # explicit pricing/category refinement request in the SAME message"
    # dispatch below (mirrors ``_skip_enrichment_dispatch`` exactly).
    # Never set by external callers.
    _skip_pricing_category_dispatch: bool = False,
) -> ActionDecision:
    """Pure-ish turn resolver. At most one extra LLM call: never (extra_llm=False)."""
    current = (text or "").strip()
    tenant = require_tenant_id(tenant_id)
    owner = str(owner_id or "")
    conv = str(conversation_id or "")
    active = store.get(tenant_id=tenant, owner_id=owner, conversation_id=conv)
    has_spreadsheet_attachment = spreadsheet_attachment_count > 0

    # PANDA -- first controlled production Bitrix product write (PR #43):
    # checked unconditionally, before follow-up/continuation heuristics --
    # an explicit, self-confirming Bitrix create instruction must never be
    # reclassified by e.g. a REFERENT/TRANSFORM follow-up guess. This ONLY
    # matches when the message itself carries all three explicit signals
    # (see ``is_explicit_bitrix_write_confirmation``); it never fires for a
    # bare "да"/"ок"/"давай"/"продолжай"/"делай дальше".
    if is_explicit_bitrix_write_confirmation(current):
        return resolve_bitrix_write_confirmation(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    # Product enrichment pipeline follow-up: same unconditional, up-front
    # placement as the Bitrix write confirmation above -- an explicit
    # "prepare the complete card" instruction must never be reclassified by
    # a follow-up/continuation guess either. Checked AFTER the Bitrix write
    # confirmation (so a message that happens to satisfy both -- it never
    # can, by construction of the two stem sets -- would still prefer the
    # write path), never fires for a bare verb alone (see the function's
    # own docstring).
    if is_explicit_product_enrichment_request(current) and not _skip_enrichment_dispatch:
        if _enrichment_needs_excel_ingestion_first(active, has_spreadsheet_attachment):
            # Production defect closure (one-turn case): the XLSX and the
            # "Подготовь полную карточку товара <SKU>..." instruction were
            # submitted in the SAME message, so there is no FAMILY_EXCEL
            # task/dataset yet and resolve_product_enrichment_request would
            # fail closed on its ``active is None`` guard without the
            # attachment ever being parsed. Resolve THIS turn as an ordinary
            # FAMILY_EXCEL turn instead -- the branch below creates the task
            # and the tool call ingests the workbook and performs the SAME
            # row lookup a "Найди товар <SKU>..." turn performs -- then reuse
            # the existing chaining marker so the gateway continues into
            # CALL_PRODUCT_ENRICHMENT within this same turn once the row
            # resolves (unresolved/ambiguous rows keep the Excel reply).
            #
            # Production defect closure (recursion/500-log-storm): a single
            # message can satisfy BOTH this predicate AND
            # ``is_explicit_product_pricing_or_category_refinement_request``
            # below (e.g. one instruction that asks for a full enrichment
            # card AND explicitly names the retail price/category). ``active``
            # is never mutated by this synchronous resolver -- ingestion only
            # happens later via the dispatched tool call -- so on the
            # recursive call below it is still ``None``/non-FAMILY_EXCEL,
            # meaning the pricing/category branch's own excel-ingestion-first
            # check would ALSO fire and recurse back in here with only ITS
            # OWN guard set, alternating forever. This recursive call must
            # therefore skip EVERY explicit-instruction dispatch branch, not
            # just this one, so it always resolves to the plain FAMILY_EXCEL
            # ingestion decision in exactly one hop.
            excel_first = resolve_action_turn(
                current,
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                store=store,
                follow_up=follow_up,
                gateway=gateway,
                request_id=request_id,
                spreadsheet_attachment_count=spreadsheet_attachment_count,
                _skip_enrichment_dispatch=True,
                _skip_pricing_category_dispatch=True,
            )
            return replace(excel_first, chain_to_enrichment_text=current)
        return resolve_product_enrichment_request(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    # Production defect closure: read-only "Покажи точно, какие данные будут
    # записаны в Bitrix/Aspro, если я подтвержу запись ... Ничего не
    # записывай" follow-up on an ALREADY prepared card. Checked after both
    # Bitrix branches above (an actual confirmation always wins, and this
    # predicate refuses a confirmation anyway) and before the generic
    # follow-up/continuation heuristics, which would otherwise hand this
    # question to the model/business-workflow path that merely echoed the
    # instruction back instead of answering from the prepared state.
    if is_bitrix_write_plan_question(current):
        return resolve_bitrix_write_plan_question(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    # Batch Bitrix existence-check defect closure: "Проверь весь прайс
    # перед загрузкой на сайт. Покажи, какие товары уже есть в Bitrix,
    # каких нет и где есть неоднозначность." names no single product at
    # all (unlike every branch above), so it must be checked before any
    # single-product-scoped predicate would otherwise swallow it or the
    # generic follow-up/continuation heuristics below hand it to the
    # model, which has no batch Bitrix tool and invents "no access to
    # Bitrix / need an export" instead of using the EXISTING
    # ``BitrixProductBridge.plan_sync`` read path this dispatches to.
    if is_batch_bitrix_existence_check_request(current):
        return resolve_batch_bitrix_existence_check(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    # Production defect closure (Turn-3 pricing/category refinement
    # follow-up, reproduced AFTER the single-product Bitrix prep fix
    # above): "Рассчитай розничную цену для этого товара и определи точную
    # категорию Bitrix/Aspro для телевизора..." mentions "Bitrix" plus a
    # "покажи"/action verb, so it would otherwise fall through to the
    # generic follow-up/continuation heuristics below and, once ``mode ==
    # NEW_TASK`` and ``_is_unrelated_new_task`` (which itself calls
    # ``requires_business_integration``) fires, degrade into the generic
    # business-workflow's diagnostic summary instead of re-surfacing the
    # SAME prepared product/Bitrix plan with the resolved category. Checked
    # after both Bitrix branches and the write-plan question above (an
    # actual confirmation always wins, and this predicate refuses one
    # anyway) and before the generic heuristics, exactly like ``is_bitrix_
    # write_plan_question`` immediately above it.
    if is_explicit_product_pricing_or_category_refinement_request(current) and not _skip_pricing_category_dispatch:
        if _pricing_category_refinement_needs_excel_ingestion_first(active, has_spreadsheet_attachment):
            # Production defect closure (one-turn case): the XLSX and the
            # "Возьми первый товар из <файл> и подготовь его для
            # Bitrix/Aspro: ... рассчитанную розничную цену, точную
            # категорию Bitrix/Aspro..." instruction were submitted in the
            # SAME message of a brand-new conversation, so there is no
            # FAMILY_EXCEL task/dataset yet and
            # ``resolve_product_pricing_category_refinement_request``
            # would fail closed on its ``active is None`` guard without the
            # attachment ever being parsed -- exactly mirrors the
            # analogous ``is_explicit_product_enrichment_request`` branch
            # above. Resolve THIS turn as an ordinary FAMILY_EXCEL turn
            # instead -- the branch below creates the task and the tool
            # call ingests the workbook and performs the SAME row lookup a
            # bare "Возьми первый товар из <файл>..." turn performs
            # (including PR #62's own "first product" fallback) -- then
            # reuse the chaining marker so the gateway continues into
            # EXPLAIN_BITRIX_WRITE_PLAN within this same turn once the row
            # resolves (unresolved/ambiguous rows keep the Excel reply).
            #
            # Production defect closure (recursion/500-log-storm): mirrors
            # the analogous comment on the enrichment branch's own
            # recursive call above -- a single message can satisfy BOTH
            # this predicate AND ``is_explicit_product_enrichment_request``,
            # and ``active`` never changes across this synchronous
            # resolution, so this recursive call must also skip the
            # enrichment dispatch branch, not just its own, or the two
            # branches alternate recursively forever (each one only ever
            # guarding against re-entering ITSELF).
            excel_first = resolve_action_turn(
                current,
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                store=store,
                follow_up=follow_up,
                gateway=gateway,
                request_id=request_id,
                spreadsheet_attachment_count=spreadsheet_attachment_count,
                _skip_enrichment_dispatch=True,
                _skip_pricing_category_dispatch=True,
            )
            return replace(excel_first, chain_to_pricing_category_refinement_text=current)
        return resolve_product_pricing_category_refinement_request(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    # Production defect closure (generic product-workflow conversation
    # continuity): ``is_bitrix_write_plan_question`` above only fires when
    # the SAME turn also names "Bitrix"/"Aspro" -- a later "Покажи
    # окончательный план записи." follow-up on an ALREADY active, already-
    # selected product task never repeats that word (the whole
    # conversation is already unambiguously about the ONE governed Bitrix
    # write this task tracks), so it fell through to the generic
    # continuation heuristics below and, once mis-detected as an unrelated
    # new task, degraded into the legacy business workflow's generic
    # summary instead of re-showing the write plan.
    #
    # Product-first routing defect closure: ordinary business phrasing for
    # the SAME "show me what will be written/published" intent -- "покажи
    # план записи товара", "что будет загружено в Bitrix", "покажи
    # карточку перед записью", "что ты собираешься отправить на сайт",
    # "покажи итог перед публикацией" -- never repeats the narrow "будет
    # записан"/"final plan" wording either, so this uses the broader,
    # GENERIC ``is_read_only_write_preview_ask`` here instead of
    # ``_is_write_plan_ask`` (see that predicate's own module note for why
    # it is only safe to broaden this far at THIS call site). Only when an
    # active FAMILY_EXCEL task already carries a resolved product
    # (``bitrix_product_fields``), i.e. never for a brand-new conversation
    # with no established product context at all.
    if (
        active is not None
        and active.family == FAMILY_EXCEL
        and not has_spreadsheet_attachment
        and dict(active.parameters.get("bitrix_product_fields") or {}).get("sku")
        and not is_explicit_bitrix_write_confirmation(current)
        and not is_explicit_product_enrichment_request(current)
        and is_read_only_write_preview_ask(current)
    ):
        return resolve_bitrix_write_plan_question(
            current,
            active=active,
            store=store,
            request_id=request_id,
        )

    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        resolved_family = (
            detect_family(current, active, has_spreadsheet_attachment=has_spreadsheet_attachment)
            if active is not None
            else None
        )
        excel_continuation = (
            active is not None and active.family == FAMILY_EXCEL and resolved_family == FAMILY_EXCEL
        )
        image_continuation = (
            active is not None
            and resolved_family == active.family
            and _is_image_artifact_request(current)
        )
        if not (excel_continuation or image_continuation):
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=NEW_TASK if follow_up.kind == KIND_TRANSFORM else CONTINUE_ACTIVE_TASK,
                task=active,
                extra_llm=False,
            )

    if active is not None and _is_yes(current) and active.risk == RISK_WRITE:
        return ActionDecision(
            decision=REQUEST_APPROVAL,
            readiness=NEEDS_APPROVAL,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            user_message="Это действие требует отдельного подтверждения в запросе на одобрение.",
            extra_llm=False,
            capability_status=CAPABILITY_AVAILABLE_REQUIRES_APPROVAL,
        )

    if active is not None and _is_cancel(current):
        active.status = STATUS_CANCELLED
        store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=NOT_EXECUTABLE,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            user_message="Отменил текущую задачу.",
            extra_llm=False,
        )

    mode = continuation_decision(
        current,
        active=active,
        follow_up=follow_up,
        has_spreadsheet_attachment=has_spreadsheet_attachment,
    )
    family = detect_family(
        current,
        active if mode != NEW_TASK else None,
        has_spreadsheet_attachment=has_spreadsheet_attachment,
    )

    if family == FAMILY_WRITE:
        if active is not None:
            active.status = STATUS_SUPERSEDED
            store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=NEEDS_APPROVAL,
            continuation=NEW_TASK,
            task=None,
            extra_llm=False,
            capability_status=CAPABILITY_AVAILABLE_REQUIRES_APPROVAL,
        )

    if family == FAMILY_SEARCH or (
        mode == NEW_TASK
        and _is_unrelated_new_task(current)
        and family not in {FAMILY_IMAGE_GENERATE, FAMILY_EXCEL, FAMILY_ACQUISITION, FAMILY_CONTENT, FAMILY_PRODUCT}
    ):
        if active is not None:
            active.status = STATUS_SUPERSEDED
            store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=CONVERSATIONAL_ONLY,
            continuation=NEW_TASK,
            task=None,
            extra_llm=False,
        )

    if mode == NEW_TASK and family is None:
        if active is not None and _token_count(current) <= 2 and not _is_unrelated_new_task(current):
            mode = AMBIGUOUS
        else:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=NEW_TASK,
                task=None,
                extra_llm=False,
            )

    if mode == AMBIGUOUS and active is not None and active.family == FAMILY_IMAGE_GENERATE:
        if _is_yes(current) and active.risk == RISK_WRITE:
            return ActionDecision(
                decision=REQUEST_APPROVAL,
                readiness=NEEDS_APPROVAL,
                continuation=CONTINUE_ACTIVE_TASK,
                task=active,
                user_message="Это действие требует отдельного подтверждения в запросе на одобрение.",
                extra_llm=False,
            )
        if _is_yes(current) and compute_readiness(active) == READY_TO_EXECUTE:
            active.execute_requested = True
            mode = CONTINUE_ACTIVE_TASK
        elif not _is_quantity_only(current) and not _has_stem(current, _IMAGE_ARTIFACT_STEMS + _IMAGE_VERB_STEMS):
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=AMBIGUOUS,
                task=active,
                extra_llm=False,
            )

    if family == FAMILY_IMAGE_GENERATE or (
        active is not None and active.family == FAMILY_IMAGE_GENERATE and mode == CONTINUE_ACTIVE_TASK
    ):
        correction = _has_stem(current, _CORRECTION_STEMS)
        if mode == NEW_TASK or active is None or active.family != FAMILY_IMAGE_GENERATE:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            task = _new_image_task(
                tenant_id=tenant, owner_id=owner, conversation_id=conv, text=current
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
            _bind_image_params(task, current, correction=correction)
            if _is_image_execute_verb(current) or _has_stem(current, ("сгенер", "сгенен")):
                task.execute_requested = True

        readiness = compute_readiness(task)
        cap_status = inspect_capability(gateway, task.tool_id)
        args = _apply_defaults(task)

        if readiness == NEEDS_REQUIRED_INPUT:
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=readiness,
                continuation=CONTINUE_ACTIVE_TASK if active is not None else NEW_TASK,
                task=task,
                arguments=args,
                user_message=_clarification_for(task),
                tool_id=task.tool_id,
                operation=task.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_IMAGE_GENERATE
        should_execute = bool(task.execute_requested)
        if is_new and _is_image_artifact_request(current) and readiness == READY_TO_EXECUTE:
            should_execute = True
        if _is_quantity_only(current) and task.execute_requested and readiness == READY_TO_EXECUTE:
            should_execute = True

        if not should_execute:
            task.status = STATUS_READY if readiness == READY_TO_EXECUTE else STATUS_DRAFT
            store.put(task)
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=readiness,
                continuation=CONTINUE_ACTIVE_TASK,
                task=task,
                arguments=args,
                extra_llm=False,
                capability_status=cap_status,
            )

        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_IMAGE_GENERATE),
                tool_id=task.tool_id,
                operation=task.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, task.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if active is not None else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=task.tool_id,
            operation=task.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_ACQUISITION:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_ACQUISITION
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                family=FAMILY_ACQUISITION,
                tool_id=ACQUISITION_CONTRACT.tool_id,
                operation=ACQUISITION_CONTRACT.operation,
                goal=current,
                parameters={
                    "url": _extract_url(current),
                    "max_pages": _extract_max_pages(current),
                },
                artifact_type=ACQUISITION_CONTRACT.artifact_type,
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
            new_url = _extract_url(current)
            if new_url:
                task.parameters["url"] = new_url
                task.parameters["max_pages"] = _extract_max_pages(current)
        task.goal = current

        url = str(task.parameters.get("url") or "")
        if not url:
            task.missing_required = ("url",)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="Пришлите ссылку на страницу, которую нужно собрать/разобрать.",
                extra_llm=False,
            )
        task.missing_required = ()

        args: dict[str, Any] = {
            "url": url,
            "max_pages": int(task.parameters.get("max_pages") or 1),
            "conversation_id": conv,
        }

        cap_status = inspect_capability(gateway, ACQUISITION_CONTRACT.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_ACQUISITION),
                tool_id=ACQUISITION_CONTRACT.tool_id,
                operation=ACQUISITION_CONTRACT.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, ACQUISITION_CONTRACT.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=ACQUISITION_CONTRACT.tool_id,
            operation=ACQUISITION_CONTRACT.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_CONTENT:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_CONTENT
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            first_url = _extract_url(current)
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                family=FAMILY_CONTENT,
                tool_id=CONTENT_CONTRACT.tool_id,
                operation=CONTENT_CONTRACT.operation,
                goal=current,
                parameters={
                    "objective": _extract_content_objective(current),
                    "urls": [first_url] if first_url else [],
                },
                artifact_type=CONTENT_CONTRACT.artifact_type,
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
            new_url = _extract_url(current)
            if new_url:
                urls = list(task.parameters.get("urls") or [])
                if new_url not in urls:
                    urls.append(new_url)
                task.parameters["urls"] = urls
            if not str(task.parameters.get("objective") or "").strip():
                task.parameters["objective"] = _extract_content_objective(current)
        task.goal = current

        objective = str(task.parameters.get("objective") or "").strip()
        if not objective:
            task.missing_required = ("objective",)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="О чём написать?",
                extra_llm=False,
            )
        task.missing_required = ()

        args = {
            "objective": objective,
            "urls": list(task.parameters.get("urls") or []),
            "conversation_id": conv,
        }

        cap_status = inspect_capability(gateway, CONTENT_CONTRACT.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_CONTENT),
                tool_id=CONTENT_CONTRACT.tool_id,
                operation=CONTENT_CONTRACT.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, CONTENT_CONTRACT.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=CONTENT_CONTRACT.tool_id,
            operation=CONTENT_CONTRACT.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_EXCEL:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_EXCEL
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                family=FAMILY_EXCEL,
                tool_id=EXCEL_CONTRACT.tool_id,
                operation=EXCEL_CONTRACT.operation,
                goal=current,
                artifact_type="workbook",
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
        # Production defect closure (XLSX attachment -> failed response,
        # follow-up phase): a continuation turn ("Продолжай и выполни мой
        # предыдущий запрос полностью", "Покажи подготовленную карточку
        # полностью, включая закупочную цену...") often carries no
        # identifying content of its own. The gate here is deliberately just
        # "this turn is already an established FAMILY_EXCEL continuation
        # (``not is_new``) AND a real prior user turn exists in history" --
        # NOT ``follow_up.kind``/``inject_context``, which only recognizes a
        # narrow, hand-picked set of continuation phrasings (deictic /
        # "продолжай" / short "главное" follow-ups) and silently excluded
        # every other legitimate rephrasing, losing the original instruction
        # to the current turn's near-empty text. ``prior_turns()`` always
        # populates ``previous_user`` (see business_assistant/follow_up.py)
        # regardless of ``kind``, so this is safe and never invents/guesses
        # text -- taken verbatim from real conversation history. Existing
        # short deterministic data continuations ("Оставь Samsung", "Минус
        # 12%") are unaffected: their own trigger phrase is still present
        # in the merged text (nl_ops matches via unanchored ``.search()``),
        # and callers that never pass ``history=`` (previous_user == "")
        # keep the exact prior behaviour.
        effective_text = current
        if not is_new and follow_up is not None and follow_up.previous_user:
            effective_text = f"{follow_up.previous_user} {current}".strip()
        task.goal = effective_text

        # Section 13 (multi-turn continuation): a new attachment always wins
        # over any inherited dataset_id (the user is deliberately switching
        # data). Otherwise resolve the prior dataset so the user never has to
        # re-upload/re-state which file to operate on.
        inherited_dataset_id = str(task.parameters.get("dataset_id") or "")
        has_dataset = bool(inherited_dataset_id) and spreadsheet_attachment_count <= 0
        if spreadsheet_attachment_count <= 0 and not inherited_dataset_id:
            task.missing_required = (PARAM_FILE,)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="Приложите файл Excel/CSV, чтобы я мог его обработать.",
                extra_llm=False,
            )

        # Section 9/12 (reconciliation, chat integration): two spreadsheet
        # attachments in the same turn is the deterministic signal for the
        # two-workbook comparison capability -- the user never picks a
        # separate "compare mode".
        if spreadsheet_attachment_count >= 2:
            contract = COMPARE_WORKBOOKS_CONTRACT
        else:
            contract = EXCEL_CONTRACT
        task.tool_id = contract.tool_id
        task.operation = contract.operation
        task.missing_required = ()

        args: dict[str, Any] = {"text": effective_text}
        if has_dataset:
            args["dataset_id"] = inherited_dataset_id
            # Production defect closure (generic product-workflow
            # conversation continuity): forward the currently selected
            # product's row identity so ``execute_nl_request`` can resolve
            # "another/previous/refine the current product" navigation
            # against the SAME dataset (see ``data_intel.service``'s
            # ``_next_distinct_row_hit``/``_row_hit_by_source_row``) --
            # never included for a brand-new dataset (a fresh attachment
            # always starts a clean selection).
            #
            # CANONICAL WORKSET (single business-data ownership): this
            # stored ``bitrix_row_selection`` is exactly the "selected
            # product exists in separate state" duplicate-ownership shape
            # the canonical Workset (see ``business_assistant.workset``)
            # replaces -- it must never keep dominating a turn on its own
            # once the ONE authoritative Workset has already moved this
            # task's focus away from a single product (e.g. a deterministic
            # bulk transform reset scope to FULL_DATASET/FILTERED_SET). Gated
            # on the EXISTING Workset scope this task already carries --
            # never a new store, never a phrase/stem check -- so a stale
            # single-product selection can no longer override a later
            # multi-row request ("stale single-product override" defect).
            # A task with no Workset yet (pre-existing/legacy task shape)
            # keeps the exact previous behaviour, unaffected.
            workset = workset_lib.get_workset(task)
            if workset is None or workset.scope == workset_lib.SCOPE_SINGLE:
                row_selection = dict(task.parameters.get("bitrix_row_selection") or {})
                if row_selection:
                    args["current_selection"] = row_selection

        cap_status = inspect_capability(gateway, contract.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_EXCEL),
                tool_id=contract.tool_id,
                operation=contract.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, contract.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=contract.tool_id,
            operation=contract.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_PRODUCT:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_PRODUCT
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            # Block 5.5 (spec section 20): a prior FAMILY_EXCEL task's
            # dataset_id is a legitimate existing-context source too -- e.g.
            # "Загрузи прайс" (Excel) followed by "Собери из него каталог"
            # (Product) must not force a re-upload.
            inherited = str((active.parameters.get("dataset_id") if active else "") or "")
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                family=FAMILY_PRODUCT,
                tool_id=PRODUCT_CONTRACT.tool_id,
                operation=PRODUCT_CONTRACT.operation,
                goal=current,
                parameters={"dataset_id": inherited, "catalog_id": ""},
                artifact_type=PRODUCT_CONTRACT.artifact_type,
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
        task.goal = current

        inherited_dataset_id = str(task.parameters.get("dataset_id") or "")
        inherited_catalog_id = str(task.parameters.get("catalog_id") or "")
        has_dataset = bool(inherited_dataset_id) and spreadsheet_attachment_count <= 0
        if spreadsheet_attachment_count <= 0 and not inherited_dataset_id and not inherited_catalog_id:
            task.missing_required = (PARAM_FILE,)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="Приложите файл с товарами (Excel/CSV), чтобы я мог собрать каталог.",
                extra_llm=False,
            )
        task.missing_required = ()

        args = {"text": current}
        if has_dataset:
            args["dataset_id"] = inherited_dataset_id
        elif inherited_catalog_id:
            args["catalog_id"] = inherited_catalog_id

        cap_status = inspect_capability(gateway, PRODUCT_CONTRACT.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_PRODUCT),
                tool_id=PRODUCT_CONTRACT.tool_id,
                operation=PRODUCT_CONTRACT.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, PRODUCT_CONTRACT.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=PRODUCT_CONTRACT.tool_id,
            operation=PRODUCT_CONTRACT.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    return ActionDecision(
        decision=ANSWER_TEXT,
        readiness=CONVERSATIONAL_ONLY,
        continuation=mode,
        task=active,
        extra_llm=False,
    )


def _idempotency_key(request_id: str, tool_id: str, arguments: Mapping[str, Any]) -> str:
    req = str(request_id or "").strip()
    if req:
        return f"{req}:{tool_id}"
    payload = hashlib.sha256(repr(sorted(dict(arguments).items())).encode("utf-8")).hexdigest()[:16]
    return f"{tool_id}:{payload}"


def mark_executed(store: ActiveTaskStore, task: ActiveTask, *, artifact_ids: tuple[str, ...] = (), failed: bool = False) -> None:
    if task.conversation_id:
        current = store.get(
            tenant_id=task.tenant_id, owner_id=task.owner_id, conversation_id=task.conversation_id
        ) or task
    else:
        current = task
    current.execution_count += 1
    current.execute_requested = False
    current.awaiting_quantity = False
    if failed:
        current.status = STATUS_FAILED_RETRYABLE
    else:
        current.status = STATUS_COMPLETED
        current.last_artifact_ids = tuple(artifact_ids)
    store.put(current)


def format_tool_user_text(
    *,
    family: str,
    data: Mapping[str, Any] | None,
    success: bool,
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    if not success:
        return _user_tool_error()
    payload = dict(data or {})
    if family == FAMILY_EXCEL:
        status = str(payload.get("status") or "OK")
        if status == "AMBIGUOUS":
            msg = str(payload.get("message_safe") or "Уточните запрос.")
            candidates = list(payload.get("candidates") or [])
            if candidates:
                msg += " Варианты: " + ", ".join(str(c) for c in candidates) + "."
            return msg
        if status == "NEEDS_USER_MAPPING":
            return str(payload.get("message_safe") or "Не удалось сопоставить столбцы для сравнения.")
        if status == "BATCH_QUEUED":
            return str(payload.get("summary_text") or "Файл большой — обрабатываю в фоне.")
        lines: list[str] = []
        summary_text = str(payload.get("summary_text") or "").strip()
        if summary_text:
            lines.append(summary_text)
        workbook = payload.get("workbook")
        if isinstance(workbook, dict) and workbook.get("view_url"):
            lines.append(f"[Скачать Excel]({workbook['view_url']})")
        if not lines:
            lines.append("Готово.")
        return "\n".join(lines)
    if family == FAMILY_ACQUISITION:
        status = str(payload.get("status") or "OK")
        if status == "BATCH_QUEUED":
            return str(
                payload.get("summary_text")
                or "Собираю данные — задача большая, выполняю в фоне."
            )
        record_count = int(payload.get("record_count") or 0)
        if record_count:
            return f"Собрал {record_count} записей со страницы."
        return "Не нашёл структурированных данных на странице."
    if family == FAMILY_CONTENT:
        status = str(payload.get("status") or "")
        errors = list(payload.get("validation_errors") or [])
        if status == "NEEDS_REVIEW" or errors:
            msg = "Текст сгенерирован, но требует ручной проверки перед публикацией."
            if errors:
                msg += " Замечания: " + ", ".join(str(e) for e in errors) + "."
            return msg
        lines: list[str] = []
        body_preview = str(payload.get("body_preview") or "").strip()
        if body_preview:
            lines.append(body_preview[:800])
        view_url = str(payload.get("view_url") or "")
        if view_url:
            lines.append(f"[Скачать текст]({view_url})")
        if not lines:
            lines.append("Готово.")
        return "\n".join(lines)
    if family == FAMILY_PRODUCT:
        operation = str(payload.get("operation") or "")
        if operation == "import":
            return (
                f"Каталог обновлён: добавлено {int(payload.get('created') or 0)}, "
                f"обновлено {int(payload.get('updated') or 0)}, "
                f"не удалось однозначно сопоставить {int(payload.get('ambiguous') or 0)}."
            )
        if operation == "duplicates":
            groups = list(payload.get("groups") or [])
            return f"Найдено групп дублей: {len(groups)}." if groups else "Дублей не найдено."
        if operation == "match_summary":
            unresolved = list(payload.get("unresolved_product_ids") or [])
            return (
                f"Не удалось однозначно сопоставить {len(unresolved)} товаров."
                if unresolved
                else "Все товары сопоставлены однозначно."
            )
        if operation == "validate":
            summary = dict(payload.get("summary") or {})
            return (
                f"Проверка каталога: valid={summary.get('valid', 0)}, "
                f"warning={summary.get('warning', 0)}, invalid={summary.get('invalid', 0)}."
            )
        if operation == "reconcile":
            return f"Остатки обновлены у {int(payload.get('updated_count') or 0)} товаров."
        if operation == "enrich":
            enriched = list(payload.get("enriched_product_ids") or [])
            return f"Подготовлены карточки для {len(enriched)} товаров."
        if operation == "export" and payload.get("view_url"):
            return f"Каталог выгружен: [скачать]({payload.get('view_url')})."
        return "Готово."
    if family in (FAMILY_IMAGE_GENERATE, FAMILY_IMAGE_EDIT):
        urls: list[str] = []
        seen: set[str] = set()

        def _add(url: str) -> None:
            if (url.startswith("/") or url.startswith("https://")) and url not in seen:
                seen.add(url)
                urls.append(url)

        # Production acceptance defect closure: prefer the caller-supplied,
        # already-registered ``artifacts`` list (each item's view_url has
        # been rewritten to the canonical /artifacts/{id}/view route once
        # register_external_image() runs) so the embedded markdown link and
        # the canonical artifact_id the UI needs for direct Edit/Download
        # actions always point at the exact same resource. Falls back to the
        # raw tool payload for callers that never registered artifacts
        # (existing tests/behavior unchanged).
        if artifacts:
            for item in artifacts:
                if isinstance(item, dict) and str(item.get("artifact_type") or item.get("type") or "") == "image":
                    _add(str(item.get("view_url") or ""))
        else:
            # Prefer the per-item "assets" list (one dict per generated variant,
            # each with its OWN view_url); it is a superset of the top-level
            # "view_url"/"url" convenience mirror (== the first asset's URL).
            # Consulting BOTH sources unconditionally previously double-added the
            # first image's URL -- rendering the same generated image twice in a
            # single-variant response. Bare "version_ids" (plain id strings, no
            # per-item URL) fall back to the single top-level view_url/url.
            assets = payload.get("assets")
            if isinstance(assets, list) and assets:
                for item in assets:
                    if isinstance(item, dict):
                        _add(str(item.get("view_url") or item.get("url") or ""))
            else:
                for key in ("view_url", "url"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        _add(value)
        lines = [_user_success_image()]
        for url in urls[:8]:
            lines.append(f"![изображение]({url})")
        return "\n".join(lines)
    return "Готово."


def artifacts_from_tool_data(data: Mapping[str, Any] | None, *, tool_id: str) -> list[dict[str, Any]]:
    payload = dict(data or {})
    out: list[dict[str, Any]] = []
    # Block 5.1: a generated workbook (data.excel_assistant/data.compare_workbooks,
    # only present when execute_nl_request()'s plan wanted an export, or a
    # workbook comparison succeeded) -- registered through the canonical
    # ArtifactService by data_intel.service, so artifact_id/view_url here are
    # already the real, authorized ones (never a raw internal blob ref).
    workbook = payload.get("workbook")
    if isinstance(workbook, dict) and workbook.get("artifact_id"):
        return [
            {
                "type": "workbook",
                "artifact_type": "workbook",
                "ref": str(workbook.get("artifact_id")),
                "artifact_id": str(workbook.get("artifact_id")),
                "mime_type": str(workbook.get("mime_type") or ""),
                "view_url": str(workbook.get("view_url") or ""),
            }
        ]
    # Block 5.3: content.create's Review -> Artifact step (see
    # ContentIntelligenceService.export_asset_artifact) -- already registered
    # through the canonical ArtifactService, so artifact_id/view_url here are
    # the real, authorized ones (never a raw internal blob ref).
    if tool_id == TOOL_CONTENT_CREATE and payload.get("exported") and payload.get("artifact_id"):
        return [
            {
                "type": "content",
                "artifact_type": "content",
                "ref": str(payload.get("artifact_id")),
                "artifact_id": str(payload.get("artifact_id")),
                "mime_type": str(payload.get("mime_type") or ""),
                "view_url": str(payload.get("view_url") or ""),
            }
        ]
    # Prefer the per-item "assets" list (ProductMediaToolAdapter always provides one) so
    # each artifact gets its OWN view_url/mime_type. The version_ids-only fallback below
    # previously reused the single top-level "view_url" (the first generated image) for
    # every version_id -- correct for variant_count=1, but silently wrong for >1 variants
    # (every additional generated image pointed at the first image's URL).
    assets = payload.get("assets")
    if isinstance(assets, list) and assets:
        for item in assets:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("version_id") or item.get("ref") or "")
            if not vid:
                continue
            out.append(
                {
                    "type": "image",
                    "artifact_type": str(item.get("artifact_type") or "image"),
                    "ref": vid,
                    "artifact_id": vid,
                    "mime_type": str(item.get("mime_type") or payload.get("mime_type") or "image/png"),
                    "view_url": str(item.get("view_url") or ""),
                }
            )
        if out:
            return out
    version_ids = list(payload.get("version_ids") or [])
    if not version_ids and payload.get("version_id"):
        version_ids = [payload.get("version_id")]
    if version_ids:
        for vid in version_ids:
            out.append(
                {
                    "type": "image",
                    "artifact_type": "image",
                    "ref": str(vid),
                    "artifact_id": str(vid),
                    "mime_type": str(payload.get("mime_type") or "image/png"),
                    "view_url": payload.get("view_url") or "",
                }
            )
        return out
    # Block 5.1: data_intel tools (analyze/filter/ambiguous/batch-queued
    # results) legitimately have NO artifact at all -- the generic
    # "tool_result" pseudo-artifact fallback below exists for other
    # capabilities' UI needs and must not manufacture a fake, non-downloadable
    # "artifact" here (would wrongly look like a generated file, and would
    # get persisted into ActiveTask.last_artifact_ids).
    if tool_id in {TOOL_DATA_EXCEL_ASSISTANT, TOOL_DATA_COMPARE_WORKBOOKS, TOOL_SCRAPE_EXTRACT}:
        return out
    if payload:
        out.append(
            {
                "type": "tool_result",
                "artifact_type": "tool_result",
                "ref": str(payload.get("request_id") or tool_id),
                "artifact_id": str(payload.get("request_id") or tool_id),
            }
        )
    return out


def scene_mentions(task: ActiveTask | None, *stems: str) -> bool:
    if task is None:
        return False
    blob = " ".join(
        str(v)
        for v in (
            task.parameters.get(PARAM_SCENE),
            task.parameters.get(PARAM_STYLE),
            task.goal,
        )
        if v
    )
    return all(_has_stem(blob, (stem,)) for stem in stems)
