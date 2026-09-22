"""Governed adapter from Business Assistant to existing Panda AI core."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class ConversationUnavailableError(Exception):
    """Raised when Panda conversational intelligence cannot produce a response."""


@dataclass(frozen=True)
class ConversationRequest:
    text: str
    tenant_id: str
    user_id: str
    request_id: str
    conversation_id: str | None = None
    correlation_id: str | None = None
    history: tuple = ()
    # Block 3.5.4: user-attached artifact refs from the current turn's
    # conversation context. These are resolved server-side (tenant/ownership
    # verified) before ever reaching a tool call -- never trusted as-is.
    attachment_refs: tuple[str, ...] = ()
    # Production acceptance defect closure ("ChatGPT-like generated image
    # actions"): the canonical artifact_id of the exact generated image the
    # user selected via the direct "Редактировать" UI action. When set,
    # ``respond()`` dispatches straight to the existing image.edit tool
    # path -- never inferred from text/NLU/"last generated image" guessing.
    # Resolved through the same trusted, tenant/conversation-verified
    # ArtifactService boundary as every other artifact access.
    image_edit_source_ref: str = ""
    # Block 4.28: canonical resolved presentation directive (style/tone/
    # length/language), built once by personalization.resolve_style_profile
    # BEFORE this request is constructed. Applied ONLY to the free-text
    # conversational model prompt below (never to CALL_TOOL/canned tool-result
    # text, never to tool-routing classification) -- see respond(): a style
    # preference can change wording, never authorization/HITL/tool permissions
    # (Block 4.28.7).
    style_directive: str = ""


@dataclass(frozen=True)
class ConversationResult:
    text: str
    workflow_id: str | None = None
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PandaConversationGateway(Protocol):
    async def respond(self, request: ConversationRequest) -> ConversationResult: ...


# Judge / routing / governance strings that must never be shown as the user answer.
_INTERNAL_ASSISTANT_MARKERS = (
    "синтез ответов экспертов без скрытого приоритета",
    "внешняя проверка фактов учитывается только при независимых источниках",
    "финальный анализ успешно сформирован",
    "использовать решение, подтвержденное большинством экспертов",
    "без скрытого приоритета provider",
)

_NO_ANSWER_PLACEHOLDERS = (
    "нет успешных ответов экспертов.",
)

# Business-diagnostic labels only — never a bare "provider" (that drops ProviderResult / expert bodies).
_TECHNICAL_SUMMARY_PREFIXES = (
    "requested:",
    "findings:",
    "artifacts:",
    "published:",
    "fixture_mode:",
    "approved:",
    "waiting_approval:",
    "status:",
    "recipe:",
    "mode:",
    "trace id:",
    "workflow_id:",
    "execution_id:",
    "provider:",
    "providers:",
)

_WRAP_KEYS = ("result", "payload", "decision", "data", "output")
_EXPERT_VALUE_KEYS = ("text", "content", "response", "output", "answer", "message")
_PROVIDER_LINE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)?\s*:\s+(.*)$")


def is_internal_assistant_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    low = raw.casefold()
    return any(marker in low for marker in _INTERNAL_ASSISTANT_MARKERS)


def _is_placeholder_answer(text: str) -> bool:
    low = str(text or "").strip().casefold()
    if not low:
        return True
    return any(low == marker or low.startswith(marker) for marker in _NO_ANSWER_PLACEHOLDERS)


def _usable_user_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw or is_internal_assistant_text(raw) or _is_placeholder_answer(raw):
        return ""
    if raw.casefold().startswith("providerresult("):
        return ""
    return raw


def _strip_technical_summary(text: str) -> str:
    kept: list[str] = []
    for line in str(text or "").replace(" | ", "\n").splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        low = trimmed.casefold()
        if any(low.startswith(prefix) for prefix in _TECHNICAL_SUMMARY_PREFIXES):
            continue
        kept.append(trimmed)
    return "\n".join(kept).strip()


def _clean_analysis_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    cleaned: list[str] = []
    prefixed = 0
    for line in lines:
        match = _PROVIDER_LINE.match(line)
        if match:
            prefixed += 1
            body = match.group(1).strip()
            if body:
                cleaned.append(body)
        else:
            cleaned.append(line)
    if prefixed == len(lines) and cleaned:
        return "\n".join(cleaned).strip()
    if prefixed and cleaned:
        return "\n".join(cleaned).strip()
    return raw


def _expert_item_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _usable_user_text(value)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return _usable_user_text(text)
    if isinstance(value, dict):
        for key in _EXPERT_VALUE_KEYS:
            inner = value.get(key)
            if isinstance(inner, str):
                got = _usable_user_text(inner)
                if got:
                    return got
            if isinstance(inner, dict):
                nested = inner.get("text") or inner.get("content")
                if isinstance(nested, str):
                    got = _usable_user_text(nested)
                    if got:
                        return got
    return ""


def _aggregate_experts(experts: Any) -> str:
    """Match Judge aggregation: every successful expert text, sorted by provider id."""
    if not isinstance(experts, dict) or not experts:
        return ""
    parts: list[str] = []
    for provider_id in sorted(experts):
        text = _expert_item_text(experts[provider_id])
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _merged_payload(result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result)
    for key in _WRAP_KEYS:
        inner = result.get(key)
        if not isinstance(inner, dict):
            continue
        for inner_key, inner_val in inner.items():
            current = merged.get(inner_key)
            if current in (None, "", {}, []):
                merged[inner_key] = inner_val
    return merged


def _candidate_text(result: dict[str, Any], key: str) -> str:
    value = result.get(key)
    if value is None:
        return ""
    if key == "experts":
        return _aggregate_experts(value)
    text = str(value).strip()
    if key == "analysis":
        text = _clean_analysis_text(text)
    return text


def select_canonical_final_answer(result: dict[str, Any] | None) -> str:
    """Authoritative user-facing answer from orchestration output.

    Order (existing architecture):
    1. explicit final_answer (Judge/formatter user-facing field)
    2. experts map aggregated the same way Judge concatenates successful experts
    3. analysis (expert dump with provider labels stripped)
    4. answer / text / reply if they are not governance/metadata
    5. best_solution / summary only if not internal and not business diagnostics
    """
    payload = _merged_payload(result) if isinstance(result, dict) else {}

    for key in ("final_answer", "experts", "analysis"):
        text = _usable_user_text(_candidate_text(payload, key))
        if text:
            return text

    for key in ("answer", "text", "reply"):
        text = _usable_user_text(_candidate_text(payload, key))
        if text:
            stripped = _strip_technical_summary(text)
            stripped = _usable_user_text(stripped)
            if stripped:
                return stripped

    for key in ("best_solution", "summary"):
        text = _usable_user_text(_candidate_text(payload, key))
        if not text:
            continue
        stripped = _usable_user_text(_strip_technical_summary(text))
        if stripped:
            return stripped
    return ""


def extract_assistant_text(result: dict[str, Any]) -> str:
    return select_canonical_final_answer(result if isinstance(result, dict) else {})


def _render_ambiguous_candidates_message(candidates: list[Mapping[str, Any]]) -> str:
    """SAME visual shape ``DataIntelligenceService.
    execute_structured_plan_via_model``'s own ``message_safe`` already
    uses for a fresh AMBIGUOUS result -- kept as one plain function
    (never a template/i18n system of its own) so a reduced clarification
    list (``STILL_AMBIGUOUS``) or a restored original list (a pending-
    ambiguity turn that matched nothing new) reads identically to the
    very first clarification the user already saw."""
    summaries = [str(c.get("summary") or "").strip() for c in candidates]
    summaries = [s for s in summaries if s]
    lines = ["Нашёл несколько подходящих товаров:"]
    lines.extend(f"{i}. {summary}" for i, summary in enumerate(summaries, 1))
    lines.append("Какой выбрать?")
    return "\n".join(lines)


def _table_operation_preview(data: dict[str, Any]) -> dict[str, Any]:
    """Concrete before/after preview for a canonical-table-execution
    result: for each affected row, exposes the affected column, the
    applied operation/parameters/scope, and its source/resulting value.

    Built ENTIRELY from ``row_changes`` -- the EXECUTOR's OWN record
    (``data_intel.transform.TransformResult.row_changes``, produced while
    ``execute_plan`` actually mutates each row, never reconstructed
    afterwards by inverting a specific operation's formula) of exactly
    which row a given rule's ``scope`` touched. This is what makes a
    COMPOUND request (scope A -> operation A, remainder -> operation B)
    preview correctly: each row's own "before"/"after" came from
    whichever rule actually touched it, not a single global percent."""
    changed_rows: list[dict[str, Any]] = []
    for change in list(data.get("row_changes") or []):
        if not isinstance(change, dict):
            continue
        params = dict(change.get("params") or {})
        before_value = change.get("before")
        after_value = change.get("after")
        changed_rows.append(
            {
                "row": dict(change.get("row_after") or {}),
                "column": change.get("column"),
                "operation": change.get("operation"),
                "percent": params.get("percent"),
                "scope": dict(change.get("scope") or {}),
                "source_value": "" if before_value is None else str(before_value),
                "resulting_value": "" if after_value is None else str(after_value),
            }
        )
    return {
        "operations_applied": list(data.get("operations_applied") or []),
        "row_count_before": data.get("row_count_before"),
        "row_count_after": data.get("row_count_after"),
        "changed_rows": changed_rows,
    }


class WorkflowPandaConversationGateway:
    """Routes conversational turns through WorkflowEngine + Router (mode/role=auto)."""

    def __init__(
        self,
        *,
        workflow_engine,
        run_router,
        context_manager,
        mode: str = "auto",
        role: str = "auto",
        tool_gateway=None,
        action_store=None,
        tool_capabilities=None,
        artifact_service=None,
        bitrix_product_bridge=None,
        media_fetcher=None,
    ):
        self._workflow_engine = workflow_engine
        self._run_router = run_router
        self._context_manager = context_manager
        self._mode = mode
        self._role = role
        self._tool_gateway = tool_gateway
        # PANDA -- first controlled production Bitrix product write (PR #43
        # conversational glue): the SAME BitrixProductBridge instance
        # BusinessAssistantService already uses for every other governed
        # Bitrix operation (Block 5.6) -- not a second connector. None is
        # safe (feature is additive; CALL_CONTROLLED_BITRIX_WRITE reports
        # capability-unavailable rather than raising).
        self._bitrix_bridge = bitrix_product_bridge
        # Block 3.5.4/3.5.5: optional canonical artifact layer -- trusted
        # attachment resolution for tool calls, and registration of
        # generated image artifacts. None is safe (feature is additive).
        self._artifact_service = artifact_service
        if action_store is None:
            from business_assistant.action_continuation import ActiveTaskStore

            action_store = ActiveTaskStore()
        self._action_store = action_store
        self._tool_capabilities = tool_capabilities
        self._executed_keys: set[str] = set()
        # Product enrichment pipeline follow-up: process-lifetime cache
        # keyed by (tenant_id, identity_key) -- mirrors ``ActiveTaskStore``'s
        # own simplicity (requirement 13: avoid repeating research/media
        # work for the exact same product; not a new infrastructure stack).
        from product_enrichment.cache import EnrichmentCache

        self._enrichment_cache = EnrichmentCache()
        # Product enrichment "MEDIA GAP" closure: the EXISTING, unchanged
        # product_enrichment.media_fetch.GovernedImageFetcher (SSRF-safe
        # raw binary download -- see its own module docstring) is the one
        # capability this conversational path previously never
        # constructed/passed at all, so image candidates research
        # discovers had nowhere to go. None-safe injection point for
        # tests (a FakeImageFetcher/mocked-transport instance); defaults
        # to a real fetcher in production -- construction itself makes no
        # network call.
        if media_fetcher is None:
            from product_enrichment.media_fetch import GovernedImageFetcher

            media_fetcher = GovernedImageFetcher()
        self._media_fetcher = media_fetcher
        self.last_action_decision = None

    def has_active_product_context(
        self, *, tenant_id: str, owner_id: str, conversation_id: str
    ) -> bool:
        """True when this conversation already carries a durable product/
        XLSX working context (an active FAMILY_EXCEL task with a parsed
        dataset) established on an EARLIER turn -- the "conversation ->
        active product task -> source dataset" boundary a later attachment-
        less follow-up must resolve against instead of re-uploading.

        Production defect closure (generic conversation/task-continuity
        root cause): every new follow-up phrasing on an already active
        product task previously needed its OWN narrow ``is_explicit_*``
        escape hatch in ``business_assistant.intent.is_conversational`` --
        each one individually recognizing that ITS specific wording must
        reach ``WorkflowPandaConversationGateway`` rather than the
        attachment-blind legacy business-workflow engine (see PRs #62-#69).
        This is the single, general, STATE-based (not phrase-based) signal
        those escape hatches were each independently working around: once a
        product/XLSX task is already active for this conversation, ANY
        later wording -- regardless of exact phrasing -- should stay on the
        SAME conversational path that already holds the state needed to
        resolve it. Callers still gate this behind their own explicit
        immediate-write/publish-verb exception, mirroring the existing
        attachment-presence check it sits alongside; this method itself
        never grants write approval -- an actual governed Bitrix write
        still requires ``resolve_action_turn``'s own explicit confirmation
        predicate downstream, completely unaffected by this check."""
        if not conversation_id:
            return False
        from business_assistant.action_continuation import FAMILY_EXCEL

        task = self._action_store.get(
            tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id
        )
        if task is None or task.family != FAMILY_EXCEL:
            return False
        return bool(str(task.parameters.get("dataset_id") or "").strip())

    def _record_latency(self, t0: float, follow_up_ms: int) -> None:
        router_obj = getattr(self._run_router, "__self__", None)
        if router_obj is None:
            return
        from agents.execution_policy import sanitize_latency_ms

        merged = dict(getattr(router_obj, "last_latency_ms", {}) or {})
        merged["follow_up_resolution_ms"] = follow_up_ms
        merged["request_total_ms"] = int((time.monotonic() - t0) * 1000)
        router_obj.last_latency_ms = sanitize_latency_ms(merged)

    async def _invoke_tool(self, request: ConversationRequest, action) -> ConversationResult:
        from business_assistant.action_continuation import (
            CALL_TOOL,
            FAMILY_ACQUISITION,
            FAMILY_EXCEL,
            FAMILY_PRODUCT,
            TOOL_DATA_EXCEL_ASSISTANT,
            TOOL_IMAGE_EDIT,
            artifacts_from_tool_data,
            format_tool_user_text,
            mark_executed,
        )
        from task_queue.lanes import WORKLOAD_INTERACTIVE
        from tools.models import ToolRequest

        task = action.task
        idem = str(action.idempotency_key or request.request_id or "")
        family = getattr(task, "family", "")
        if idem and idem in self._executed_keys:
            return ConversationResult(
                text=format_tool_user_text(family=family, data={}, success=True),
                task_id=task.task_id if task else None,
                metadata={
                    "follow_up_kind": None,
                    "action_decision": CALL_TOOL,
                    "duplicate": True,
                    "artifacts": [],
                },
            )
        provided = ()
        from business_assistant.action_continuation import CONTRACTS, RISK_GENERATE

        contract = CONTRACTS.get(family)
        if contract is not None:
            provided = tuple(contract.required_capabilities)
        if self._tool_capabilities is not None:
            provided = tuple(self._tool_capabilities.capabilities) or provided

        arguments = dict(action.arguments or {})
        # Block 3.5.4: resolve any user-attached refs through the trusted
        # server-side artifact layer and inject the safe, verified
        # descriptors under a reserved key -- always overwritten here (after
        # merging action.arguments) so message text/tool arguments/raw user
        # input can never spoof ownership of another tenant's file.
        if self._artifact_service is not None and request.attachment_refs:
            try:
                resolved = self._artifact_service.resolve_trusted_refs(
                    tenant_id=str(request.tenant_id or ""),
                    conversation_id=str(request.conversation_id or ""),
                    refs=tuple(request.attachment_refs),
                )
            except Exception:
                resolved = []
            arguments["attachment_refs"] = resolved
        # Block 5.1: data_intel's DataIntelToolAdapter registers generated
        # workbooks as conversation-attached artifacts; ToolRequest carries
        # no dedicated conversation field, so it travels through arguments
        # like attachment_refs above (harmless/ignored by every other adapter).
        arguments.setdefault("conversation_id", str(request.conversation_id or ""))

        if action.tool_id == TOOL_IMAGE_EDIT:
            # Production acceptance defect closure: the direct "Редактировать"
            # UI action supplies an explicit source artifact ref (never
            # inferred by NLU/"last generated image" guessing -- see
            # respond()'s deterministic dispatch). Resolve it through the
            # same trusted, tenant/conversation-verified boundary as every
            # other artifact access; a spoofed/foreign/non-image ref must
            # never reach the image.edit tool.
            source_ref = str(getattr(request, "image_edit_source_ref", "") or "").strip()
            version_id = (
                self._artifact_service.resolve_trusted_image_source(
                    tenant_id=str(request.tenant_id or ""),
                    conversation_id=str(request.conversation_id or ""),
                    ref=source_ref,
                )
                if source_ref and self._artifact_service is not None
                else None
            )
            if not version_id:
                if task is not None:
                    mark_executed(self._action_store, task, failed=True)
                return ConversationResult(
                    text="Не удалось найти исходное изображение для редактирования.",
                    task_id=getattr(task, "task_id", None),
                    metadata={"action_decision": CALL_TOOL, "artifacts": []},
                )
            arguments["source_version_id"] = version_id

        tool_request = ToolRequest(
            request_id=str(request.request_id or uuid.uuid4()),
            workflow_id="",
            task_id=str(task.task_id if task else uuid.uuid4()),
            tool_id=action.tool_id,
            operation=action.operation or "generate",
            arguments=arguments,
            requested_capabilities=provided,
            # Server-classified: every Business Assistant tool call is a live user
            # waiting synchronously for a reply. Never sourced from `action.arguments`
            # (user-controlled) so it cannot be spoofed by request content.
            metadata={"workload_class": WORKLOAD_INTERACTIVE},
            tenant_id=str(request.tenant_id or ""),
            user_id=str(request.user_id or ""),
            actor_id=f"{request.tenant_id}:{request.user_id}",
            idempotency_key=idem or None,
        )
        try:
            result = await self._tool_gateway.invoke(
                tool_request,
                capabilities=self._tool_capabilities,
            )
        except Exception:
            if task is not None:
                mark_executed(self._action_store, task, failed=True)
            return ConversationResult(
                text=format_tool_user_text(family=getattr(task, "family", ""), data=None, success=False),
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CALL_TOOL, "artifacts": []},
            )
        success = bool(getattr(result, "success", False))
        data = dict(getattr(result, "data", None) or {})
        error_code = str(getattr(result, "error_code", "") or "")
        if error_code == "tool_approval_required":
            if task is not None:
                task.status = "WAITING_FOR_INPUT"
                task.risk = "write_governed"
                self._action_store.put(task)
            return ConversationResult(
                text="Это действие требует подтверждения.",
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": "REQUEST_APPROVAL", "artifacts": []},
            )
        artifacts = artifacts_from_tool_data(data, tool_id=action.tool_id) if success else []
        # Strict artifact-required success invariant for artifact-generating capabilities
        # (risk=RISK_GENERATE, e.g. image.generate/image.edit -- not generic read/search/write
        # tools). The ToolGateway only reports "no exception raised" as success; adapters such
        # as ProductMediaToolAdapter can legitimately return {"status": "error", ...} without
        # raising when the provider/persistence chain yields no usable image. artifacts_from_
        # tool_data() falls back to a generic "tool_result" pseudo-artifact for ANY non-empty
        # payload (including that error dict), so success must require an artifact of the
        # capability's declared artifact_type specifically -- never say "Готово."/cache
        # idempotent success without at least one real, persisted image artifact.
        if success and contract is not None and contract.risk == RISK_GENERATE:
            required_type = contract.artifact_type
            has_required_artifact = (
                any(str(a.get("artifact_type") or a.get("type") or "") == required_type for a in artifacts)
                if required_type
                else bool(artifacts)
            )
            if not has_required_artifact:
                success = False
                artifacts = []
        if success and artifacts and self._artifact_service is not None:
            # Block 3.5.5: register image artifacts through the canonical
            # artifact layer (thin wrapper -- bytes stay in product_media,
            # existing generation/persistence is untouched). Best-effort: a
            # registration failure must never break the already-working
            # image delivery/view_url path.
            for art in artifacts:
                if str(art.get("artifact_type") or art.get("type") or "") != "image":
                    continue
                version_id = str(art.get("artifact_id") or art.get("ref") or "")
                if not version_id:
                    continue
                try:
                    rec = self._artifact_service.register_external_image(
                        tenant_id=str(request.tenant_id or ""),
                        owner_id=str(request.user_id or ""),
                        version_id=version_id,
                        mime_type=str(art.get("mime_type") or "image/png"),
                        conversation_id=str(request.conversation_id or ""),
                        request_id=str(request.request_id or ""),
                        tool_id=str(action.tool_id or ""),
                    )
                    art["artifact_id"] = rec.artifact_id
                    art["download_url"] = f"/api/v1/business-assistant/artifacts/{rec.artifact_id}/download"
                    # Production acceptance defect closure: point the markdown
                    # image (built from this same ``artifacts`` list below) at
                    # the canonical, authorized view route instead of the raw
                    # product_media media URL -- this is what lets the UI
                    # recover the exact artifact_id for direct Edit/Download
                    # actions straight from the rendered chat message, with no
                    # new message field and no duplicate image.
                    art["view_url"] = f"/api/v1/business-assistant/artifacts/{rec.artifact_id}/view"
                except Exception:
                    pass
        if idem and success:
            self._executed_keys.add(idem)
        if family == FAMILY_EXCEL and success and task is not None:
            # CANONICAL WORKSET (single business-data ownership): the
            # resulting dataset_id -- a fresh source on a new attachment, a
            # derived version after a transform, or the unchanged current
            # dataset after an analyze/ambiguous turn -- is applied onto the
            # ONE authoritative Workset this task owns (see
            # business_assistant.workset), never assigned to the flat
            # ``dataset_id`` key directly. ``workset.apply_to_task`` keeps
            # that flat key mirrored for ``resolve_action_turn``'s own
            # EXISTING ``has_dataset``/continuation gate, so that large,
            # already-tested resolver needs no change of its own. Persisted
            # BEFORE mark_executed() below re-reads the task from the store,
            # so the next turn can resolve "them"/"it" without re-upload.
            from business_assistant import workset as workset_lib

            new_dataset_id = str(data.get("dataset_id") or "")
            changed = False
            if new_dataset_id:
                # Mirrors the EXISTING "a new attachment always wins" rule
                # ``resolve_action_turn`` already applies immediately before
                # dispatching this call (no inherited ``dataset_id`` was
                # forwarded as an argument): that is exactly when THIS
                # dataset must become the new canonical SOURCE, never merely
                # the current one.
                had_inherited_dataset_id = bool(dict(action.arguments or {}).get("dataset_id"))
                current_workset = workset_lib.get_workset(task)
                if not had_inherited_dataset_id or current_workset is None:
                    base_workset = workset_lib.start_new_source(
                        current_workset,
                        tenant_id=task.tenant_id,
                        owner_id=task.owner_id,
                        conversation_id=task.conversation_id,
                        dataset_id=new_dataset_id,
                    )
                else:
                    base_workset = current_workset
                workset_lib.apply_to_task(task, workset_lib.apply_tool_result(base_workset, data))
                changed = True
            # PANDA -- first controlled production Bitrix product write
            # (PR #43 conversational glue): a ROW_FOUND preview identifies
            # the SAME single product a later explicit "Подтверждаю: создай
            # этот товар в Bitrix..." confirmation must resolve -- persist
            # its already role-resolved fields (never the raw row) so that
            # later turn never has to re-parse the workbook or guess which
            # column is which (see resolve_bitrix_write_confirmation).
            if str(data.get("status") or "") == "ROW_FOUND":
                product_fields = data.get("product_fields")
                new_source_row = data.get("row_source_row")
                if isinstance(product_fields, dict) and product_fields:
                    if task.parameters.get("bitrix_product_fields") != dict(product_fields):
                        # Product enrichment pipeline follow-up: a NEW
                        # ROW_FOUND for a DIFFERENT product must never let a
                        # PRIOR product's enrichment (characteristics/
                        # content/media) leak into this one's later write
                        # confirmation.
                        task.parameters.pop("bitrix_enrichment_write_request", None)
                        task.parameters.pop("bitrix_enrichment_characteristic_status", None)
                        task.parameters.pop("bitrix_enrichment_preview", None)
                        # Production defect closure (generic product-
                        # workflow conversation continuity): track the row
                        # navigation history (see ``data_intel.service``'s
                        # ``_row_hit_by_source_row``/``_next_distinct_row_
                        # hit``) so a LATER "вернись к предыдущему товару"
                        # follow-up can resolve deterministically -- taken
                        # verbatim from the PRIOR selection, never guessed.
                        prior_selection = dict(task.parameters.get("bitrix_row_selection") or {})
                        prior_source_row = prior_selection.get("source_row")
                        history = list(prior_selection.get("history") or [])
                        if prior_source_row not in (None, "") and prior_source_row != new_source_row:
                            history.append(prior_source_row)
                        if new_source_row not in (None, ""):
                            task.parameters["bitrix_row_selection"] = {
                                "source_row": new_source_row,
                                "history": history,
                            }
                    elif new_source_row not in (None, ""):
                        # SAME product re-surfaced (e.g. a plain refinement
                        # question about "него") -- keep the existing
                        # selection/history unchanged, just ensure it stays
                        # populated for later navigation.
                        selection = dict(task.parameters.get("bitrix_row_selection") or {})
                        selection.setdefault("source_row", new_source_row)
                        selection.setdefault("history", [])
                        task.parameters["bitrix_row_selection"] = selection
                    task.parameters["bitrix_product_fields"] = dict(product_fields)
                    changed = True
                retail_preview = str(data.get("retail_price_preview") or "")
                if retail_preview:
                    task.parameters["bitrix_retail_price_preview"] = retail_preview
                    changed = True
            if changed:
                self._action_store.put(task)
        if family == FAMILY_ACQUISITION and success and task is not None:
            # Block 5.2 multi-turn continuation (spec section 22): once
            # acquisition/extraction produced a Block 5.1 dataset, hand the
            # conversation frame to FAMILY_EXCEL so "Оставь Samsung"/"Только
            # дешевле 50000"/"Сохрани в Excel" operate on THIS dataset without
            # re-crawling/re-acquiring -- the exact same continuation path an
            # uploaded spreadsheet would already use.
            new_dataset_id = str(data.get("dataset_id") or "")
            if new_dataset_id:
                from business_assistant import workset as workset_lib

                task.family = FAMILY_EXCEL
                task.tool_id = TOOL_DATA_EXCEL_ASSISTANT
                task.operation = "assist"
                task.artifact_type = "workbook"
                # This acquisition result IS a brand-new canonical source for
                # the FAMILY_EXCEL context it just became -- same reasoning
                # as a fresh spreadsheet attachment above.
                workset_lib.apply_to_task(
                    task,
                    workset_lib.start_new_source(
                        workset_lib.get_workset(task),
                        tenant_id=task.tenant_id,
                        owner_id=task.owner_id,
                        conversation_id=task.conversation_id,
                        dataset_id=new_dataset_id,
                    ),
                )
                self._action_store.put(task)
        if family == FAMILY_PRODUCT and success and task is not None:
            # Block 5.5 multi-turn continuation (spec section 20): persist the
            # resolved catalog_id BEFORE mark_executed() re-reads the task, so
            # a follow-up turn ("Найди дубли", "Проверь каталог") resolves the
            # SAME catalog without re-attaching the price list -- mirrors the
            # FAMILY_EXCEL dataset_id persistence above. Without this, the
            # next turn's "no attachment and no inherited source" gate in
            # resolve_action_turn would wrongly ask the user to re-upload.
            new_catalog_id = str(data.get("catalog_id") or "")
            if new_catalog_id:
                task.parameters["catalog_id"] = new_catalog_id
                self._action_store.put(task)
        if task is not None:
            mark_executed(
                self._action_store,
                task,
                artifact_ids=tuple(str(a.get("ref") or "") for a in artifacts if a.get("ref")),
                failed=not success,
            )
        reply = format_tool_user_text(
            family=getattr(task, "family", ""),
            data=data,
            success=success,
            artifacts=artifacts,
        )
        return ConversationResult(
            text=reply,
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": CALL_TOOL,
                "artifacts": artifacts,
                "follow_up_kind": None,
            },
        )

    def _persist_managed_agent_product_context(self, request: ConversationRequest, metadata: dict) -> None:
        """Ownership-model-B defect closure (managed-agent -> governed
        Bitrix write confirmation, PR #87 follow-up), PART 2: this gateway
        -- never ``managed_agent_poc``/``panda_bridge`` (which remain state-
        pure -- see that module's own "palm + fingers" independence
        docstring, and its ``maybe_respond_via_managed_agent`` return
        contract, which only RETURNS this data, never persists it) -- is
        the sole owner of durable ``ActiveTaskStore`` state. Reuses the
        EXISTING get -> mutate ``task.parameters`` -> ``put`` pattern the
        legacy FAMILY_EXCEL ``ROW_FOUND``/``CALL_PRODUCT_ENRICHMENT`` paths
        already use above (``_invoke_tool``/``_invoke_product_enrichment``)
        under the SAME EXISTING ``bitrix_product_fields``/
        ``bitrix_enrichment_write_request``/``bitrix_retail_price_preview``
        keys -- no new state store, no new schema.

        A no-op whenever this turn's managed-agent metadata carries no
        resolved product (e.g. a pure ``analyze_spreadsheet`` turn, or a
        failed/NOT_FOUND resolution) -- nothing about the conversation's
        existing product/write context is touched in that case.

        Called on EVERY successful managed-agent product/write-plan
        resolution (``select_product`` AND ``explain_bitrix_write_plan``),
        never only the first product in the conversation -- so a later
        Product A -> Product B switch through this SAME managed-agent path
        always overwrites both keys together with Product B's identity
        atomically, exactly like the legacy ``ROW_FOUND`` handler already
        does. This is what keeps PR #87's own cross-product SKU guard in
        ``_invoke_controlled_bitrix_write`` meaningful here too: that guard
        fails closed the instant ``bitrix_enrichment_write_request``'s own
        sku ever disagrees with ``bitrix_product_fields['sku']`` -- which
        can only happen if one of the two were left stale while the other
        moved on, something this method never allows since it always
        writes both from the SAME managed-agent turn's resolution."""
        product_fields = metadata.get("bitrix_product_fields")
        if not isinstance(product_fields, dict) or not product_fields.get("title") or not product_fields.get("sku"):
            return

        from business_assistant.action_continuation import (
            EXCEL_CONTRACT,
            FAMILY_EXCEL,
            RISK_READ,
            STATUS_DRAFT,
            ActiveTask,
        )

        tenant_id = str(request.tenant_id or "")
        owner_id = str(request.user_id or "")
        conversation_id = str(request.conversation_id or "")
        if not conversation_id:
            return

        task = self._action_store.get(tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id)
        if task is None or task.family != FAMILY_EXCEL:
            # A managed-agent-driven conversation never reaches
            # resolve_action_turn() at all while eligible (see this
            # method's caller), so no ActiveTask normally exists here yet.
            # FAMILY_EXCEL is the EXISTING family
            # ``resolve_bitrix_write_confirmation`` already requires --
            # reused verbatim (PART 4's compatibility guard: this exact
            # existing check is satisfied by reusing the existing family,
            # never inventing a new one). If some OTHER, unrelated family's
            # task happens to be active for this conversation, it is
            # replaced here -- this managed-agent-resolved product context
            # is the newest, most relevant state for a following write
            # confirmation.
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                family=FAMILY_EXCEL,
                tool_id=EXCEL_CONTRACT.tool_id,
                operation=EXCEL_CONTRACT.operation,
                goal="",
                artifact_type="workbook",
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )

        task.parameters["bitrix_product_fields"] = dict(product_fields)
        enrichment_write_request = metadata.get("bitrix_enrichment_write_request")
        if isinstance(enrichment_write_request, dict) and enrichment_write_request:
            task.parameters["bitrix_enrichment_write_request"] = dict(enrichment_write_request)
        else:
            task.parameters.pop("bitrix_enrichment_write_request", None)
        retail_price_preview = str(metadata.get("bitrix_retail_price_preview") or "")
        if retail_price_preview:
            task.parameters["bitrix_retail_price_preview"] = retail_price_preview

        # CANONICAL WORKSET (single business-data ownership): the managed
        # agent resolved ONE specific product this turn -- narrow the
        # EXISTING canonical Workset's scope to SINGLE (requirement 3:
        # "single product is only a scope", never a second/competing
        # business context). ``managed_agent_poc``'s own private dataset
        # (see ``managed_agent_poc.panda_bridge``'s module docstring) is
        # NEVER read here -- if no canonical Workset exists yet for this
        # conversation (e.g. ``_establish_canonical_workset_from_attachment``
        # was never reached -- no attachment/no tool_gateway this turn),
        # this is a no-op: there is deliberately nothing non-authoritative
        # to promote into authoritative state.
        from business_assistant import workset as workset_lib

        current_workset = workset_lib.get_workset(task)
        if current_workset is not None:
            workset_lib.apply_to_task(
                task, workset_lib.select_single(current_workset, str(product_fields.get("sku") or ""))
            )
        self._action_store.put(task)

    async def _establish_canonical_workset_from_attachment(
        self, request: ConversationRequest, spreadsheet_ref: dict
    ) -> bool:
        """CANONICAL WORKSET (single business-data ownership): a
        spreadsheet attached THIS turn always (re)establishes the ONE
        authoritative business-data context this gateway owns -- BEFORE
        the managed-agent boundary ever runs (see this method's caller in
        ``respond()``).

        Calls the SAME EXISTING ``data.excel_assistant``/``assist`` tool
        (``business_assistant.action_continuation.EXCEL_CONTRACT``) the
        legacy ``resolve_action_turn``/``_invoke_tool`` path already calls
        for a fresh attachment -- reused unchanged, never a second/
        duplicated ingestion implementation. No transformation text is
        sent (``text=""``): this call exists ONLY to ingest the raw
        attachment bytes into the SHARED, canonical ``data_intel`` store
        and obtain its ``dataset_id`` -- never to apply any operation.

        This is deliberately NOT a bridge/synchronization between this
        canonical dataset and ``managed_agent_poc``'s own private dataset
        store: the two are independent ingestions of the SAME source
        bytes for two different purposes (this one is the durable business
        truth; the managed agent's own copy stays a private, disposable
        compatibility cache for its own 3 read-only tools -- see that
        module's docstring). No data ever flows from one to the other, so
        there is nothing to keep synchronized and nothing to delete later.

        Returns ``True`` iff the canonical Workset was actually
        (re)established from THIS attachment, ``False`` on any failure
        (tool unavailable, ingest error, no dataset id produced).
        Final-review correction: a ``False`` return is NOT swallowed by
        this method's caller -- a fresh spreadsheet attachment whose
        canonical (shared ``data_intel``) ingest failed must never let
        the managed-agent boundary run this turn, because that would let
        ``managed_agent_poc``'s own private dataset become the ONLY
        authoritative continuation context for a supposedly-canonical
        attachment (the exact split-ownership condition this module
        exists to remove). See the caller in ``respond()`` for the
        skip-managed-agent-this-turn gate this return value drives."""
        if self._tool_gateway is None:
            return False
        conversation_id = str(request.conversation_id or "")
        if not conversation_id:
            return False

        from business_assistant.action_continuation import (
            ActiveTask,
            EXCEL_CONTRACT,
            FAMILY_EXCEL,
            RISK_READ,
            STATUS_DRAFT,
        )
        from business_assistant import workset as workset_lib
        from tools.models import ToolRequest

        tenant_id = str(request.tenant_id or "")
        owner_id = str(request.user_id or "")
        tool_request = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="",
            task_id=str(uuid.uuid4()),
            tool_id=EXCEL_CONTRACT.tool_id,
            operation=EXCEL_CONTRACT.operation,
            arguments={"text": "", "attachment_refs": [dict(spreadsheet_ref)]},
            requested_capabilities=tuple(EXCEL_CONTRACT.required_capabilities),
            tenant_id=tenant_id,
            user_id=owner_id,
            actor_id=f"{tenant_id}:{owner_id}",
        )
        try:
            result = await self._tool_gateway.invoke(tool_request, capabilities=self._tool_capabilities)
        except Exception:
            return False
        if not getattr(result, "success", False):
            return False
        data = dict(getattr(result, "data", None) or {})
        new_dataset_id = str(data.get("dataset_id") or "")
        if not new_dataset_id:
            return False

        task = self._action_store.get(tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id)
        if task is None or task.family != FAMILY_EXCEL:
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                family=FAMILY_EXCEL,
                tool_id=EXCEL_CONTRACT.tool_id,
                operation=EXCEL_CONTRACT.operation,
                goal="",
                artifact_type="workbook",
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        workset_lib.apply_to_task(
            task,
            workset_lib.start_new_source(
                workset_lib.get_workset(task),
                tenant_id=tenant_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                dataset_id=new_dataset_id,
            ),
        )
        self._action_store.put(task)
        return True

    def _persist_resolved_selection(self, task, workset, resolved: dict) -> None:
        """Production defect closure (Defect B: SINGLE -> MULTI -> SINGLE
        scope recovery): applies an already-computed ``ROW_FOUND``-shaped
        resolution (``DataIntelligenceService._resolve_product_reference``
        via ``execute_structured_plan_via_model``'s ``resolved_selection``)
        onto the CURRENT canonical Workset -- the exact SAME transition
        ``status == "ROW_FOUND"`` already applies for an explicit
        selection turn, just reused here for a selection that was
        recovered IMPLICITLY from this turn's own text because there was
        no current SINGLE selection to answer/preview against. Restores
        ``scope=SINGLE`` and persists the selected product's fields so the
        REST of this SAME turn (a field query / write-plan preview) reads
        the newly-selected product, never a stale/previous one."""
        from business_assistant import workset as workset_lib

        next_workset = workset_lib.apply_tool_result(workset, resolved)
        workset_lib.apply_to_task(task, next_workset)
        fields = resolved.get("product_fields")
        if isinstance(fields, dict):
            task.parameters["bitrix_product_fields"] = dict(fields)
        task.parameters.pop("bitrix_enrichment_write_request", None)
        task.parameters.pop("product_enrichment_result", None)
        task.parameters["bitrix_retail_price_preview"] = str(
            resolved.get("retail_price_preview") or ""
        )
        self._action_store.put(task)

    async def _maybe_execute_canonical_table_operation(
        self, request: ConversationRequest, text: str
    ) -> ConversationResult | None:
        """CANONICAL TABLE EXECUTION -- ONE model semantic call -> validated
        structured plan -> existing deterministic executor (PR #92
        correction): the semantic boundary the managed-agent integration
        boundary above cannot cross on its own -- its 3 read-only tools
        (``analyze_spreadsheet``/``select_product``/
        ``explain_bitrix_write_plan``, see ``runtime_subprocess.py``) have
        no bulk/structural table-transform capability at all, so a real
        production "increase the retail price for ALL products" turn fell
        through to a free-text "I can't recalculate this in bulk" answer
        from the model instead of ever reaching the EXISTING deterministic
        Data Intelligence executor.

        Gives that EXISTING executor the FIRST and ONLY interpretation of
        this turn's text, over THIS conversation's canonical Workset (PR
        #91) current dataset -- the SAME ``data.excel_assistant``/
        ``assist`` tool call the legacy FAMILY_EXCEL path/``_invoke_tool``
        below already uses, but with ``use_model_plan=True`` so the
        adapter routes to ``DataIntelligenceService.
        execute_structured_plan_via_model`` (ONE call to the EXISTING
        one-shot model seam ``agents.openai_agent.OpenAIAgent.run``,
        deterministically validated into an ``OperationPlan`` -- see
        ``data_intel.nl_plan_llm``) instead of ``compile_request``'s
        bounded RU/EN regex/stem grammar. ``compile_request`` itself is
        never consulted here, and ``text`` is never interpreted a second
        time after the model call returns.

        Returns a concrete successful ``ConversationResult`` when the
        model itself judged this text a genuine table-wide (or table-
        subset) operation AND its output passed strict deterministic
        validation AND ``execute_plan`` applied it -- the tool's own
        ``status == "OK"``. Returns ``None`` (falls straight through to
        the existing managed-agent/legacy routing, completely unchanged)
        ONLY for no canonical Workset/dataset yet for this conversation,
        or ``status == "NOT_APPLICABLE"`` -- a genuine, validly-parsed
        model judgment that this is a single-product selection/plain
        analysis/write-plan question/anything else that is not a table-
        wide operation.

        PRODUCTION DEFECT CLOSURE (PR #93): every OTHER outcome --
        ``MODEL_ERROR``/``PARSE_ERROR``/``VALIDATION_ERROR`` (a provider
        failure, malformed model output, an unknown column reference, an
        invalid scope, ...; see ``data_intel.nl_plan_llm.ModelPlanError``)
        -- is a TECHNICAL failure of this boundary itself, NOT a judgment
        that the text isn't a table operation, and returns a fail-closed
        ``ConversationResult`` (a plain "the table operation was not
        executed, nothing changed" message) INSTEAD of ``None``. A real
        production request over a column name containing a comma failed
        exactly this way, and the previous revision's ``return None`` on
        any non-"OK" status let it silently fall through to managed-agent
        product selection/enrichment -- an unrelated workflow the user
        never asked for. Nothing is persisted for any non-"OK" outcome
        (no ``store.save_dataset`` call outside the ``status == "OK"``
        branch either way), so this has zero side effects on the shared
        ``data_intel`` store beyond ``ActiveTaskStore``'s own executed/
        failed bookkeeping.

        Adds no new agent, router, dataset store, or executor: it is the
        SAME tool_id/operation, the SAME ``DataIntelligenceService``, and
        the SAME ``data_intel.transform.execute_plan``. It also adds no
        phrase/stem list of its own -- arbitration between "table
        operation" and "not a table operation" (formerly four separate
        pure predicates checked here as a precedence guard against
        ``compile_request``'s own overly loose grammar) is now the
        model's OWN single judgment call, deterministically validated
        afterwards; changing the request's wording or numeric values, or
        adding a second scoped rule to the SAME request (e.g. "the first 3
        rows +7%, everyone else +15%"), requires zero code change here."""

        if self._tool_gateway is None:
            return None
        tenant_id = str(request.tenant_id or "")
        owner_id = str(request.user_id or "")
        conversation_id = str(request.conversation_id or "")
        if not conversation_id:
            return None

        from business_assistant.action_continuation import (
            EXCEL_CONTRACT,
            EXPLAIN_BITRIX_WRITE_PLAN,
            FAMILY_EXCEL,
            artifacts_from_tool_data,
            format_tool_user_text,
            mark_executed,
            resolve_bitrix_write_plan_question,
        )
        from business_assistant import workset as workset_lib
        from data_intel.service import resolve_ambiguity_clarification
        from tools.models import ToolRequest

        task = self._action_store.get(
            tenant_id=tenant_id, owner_id=owner_id, conversation_id=conversation_id
        )
        if task is None or task.family != FAMILY_EXCEL:
            return None
        workset = workset_lib.get_workset(task)
        if workset is None or not workset.current_dataset_id:
            return None

        # Clarification-continuation (generic, data-driven -- production
        # defect closure): when a PRIOR turn left a pending AMBIGUOUS
        # candidate set on THIS SAME task (see the ``status ==
        # "AMBIGUOUS"`` branch below, which is the only place that ever
        # writes ``pending_product_ambiguity``), THIS turn's text is
        # resolved AGAINST THAT candidate set FIRST -- an ordinal
        # ("второй"), a unique suffix/token, a shortened identifying
        # fragment, or an exact article/SKU/EAN -- never re-searched
        # against the whole Workset unless that resolution finds nothing
        # in the pending set AND this turn's text also fails to name any
        # OTHER concrete product below (see ``restore_pending_message``).
        # Persisted entirely inside the EXISTING ``ActiveTask.parameters``
        # -- no new store, no new agent, no new dataset.
        effective_text = text
        restore_pending_message: str | None = None
        pending = task.parameters.get("pending_product_ambiguity")
        if isinstance(pending, Mapping) and pending.get("dataset_id") == workset.current_dataset_id:
            pending_candidates = [c for c in (pending.get("candidates") or []) if isinstance(c, Mapping)]
            outcome, matched = resolve_ambiguity_clarification(text, pending_candidates)
            if outcome == "SELECTED" and matched:
                # Exactly one candidate from the PRIOR turn's own list
                # resolved -- clear the pending state and re-express this
                # turn as a direct reference to that candidate's own
                # canonical identifier, so the SAME deterministic,
                # exact-match row lookup every other "send the exact
                # SKU/article/EAN" turn already uses picks it up (never a
                # second, competing selection mechanism).
                task.parameters.pop("pending_product_ambiguity", None)
                effective_text = str(matched[0].get("value") or "") or text
            elif outcome == "STILL_AMBIGUOUS" and matched:
                task.parameters["pending_product_ambiguity"] = {
                    "dataset_id": pending.get("dataset_id"),
                    "candidates": matched,
                }
                self._action_store.put(task)
                mark_executed(self._action_store, task, failed=True)
                return ConversationResult(
                    text=_render_ambiguous_candidates_message(matched),
                    task_id=task.task_id,
                    metadata={"action_decision": "AMBIGUOUS_PRODUCT_REFERENCE", "artifacts": []},
                )
            else:
                # Matches none of the pending candidates. Do NOT guess and
                # do NOT drop the original candidate set yet -- only a
                # CONCLUSIVE different outcome below (a fresh ROW_FOUND/
                # OK/FIELD_VALUE/WRITE_PLAN_REQUESTED, i.e. this turn
                # clearly named/started something else) abandons it; a
                # fresh AMBIGUOUS/no-match result is not new information,
                # so this turn re-asks against the SAME original list
                # instead of a re-searched (and possibly empty) one.
                restore_pending_message = _render_ambiguous_candidates_message(pending_candidates)

        args: dict = {
            "text": effective_text,
            "dataset_id": workset.current_dataset_id,
            "use_model_plan": True,
            "conversation_id": conversation_id,
            "workset_scope": workset.scope,
            "selected_identifiers": list(workset.selected_identifiers),
        }

        tool_request = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="",
            task_id=task.task_id,
            tool_id=EXCEL_CONTRACT.tool_id,
            operation=EXCEL_CONTRACT.operation,
            arguments=args,
            requested_capabilities=tuple(EXCEL_CONTRACT.required_capabilities),
            tenant_id=tenant_id,
            user_id=owner_id,
            actor_id=f"{tenant_id}:{owner_id}",
        )
        try:
            result = await self._tool_gateway.invoke(tool_request, capabilities=self._tool_capabilities)
        except Exception:
            return None
        if not getattr(result, "success", False):
            return None
        data = dict(getattr(result, "data", None) or {})
        status = str(data.get("status") or "")

        if restore_pending_message is not None:
            if status in ("ROW_FOUND", "OK"):
                # A CONCLUSIVE outcome -- an outright product selection
                # (``ROW_FOUND``) or a table-wide operation (``OK``) --
                # this turn clearly named/started something else, so the
                # pending ambiguity is explicitly abandoned instead of
                # silently hijacking a later unrelated turn. Persisted
                # immediately (not left for a downstream branch's own
                # ``put``) so the abandonment survives regardless of
                # what that branch does afterward.
                task.parameters.pop("pending_product_ambiguity", None)
                self._action_store.put(task)
            else:
                # AMBIGUOUS / NOT_APPLICABLE / "" / FIELD_VALUE / WRITE_
                # PLAN_REQUESTED / any technical-failure status -- NONE
                # of these conclusively named a NEW, different product
                # (``FIELD_VALUE``/``WRITE_PLAN_REQUESTED`` only ever
                # answer about an ALREADY selected product, never a
                # fresh selection). Neither the pending candidate set nor
                # this whole-Workset attempt distinguished a different
                # product, so keep the ORIGINAL pending candidates alive
                # (already persisted, untouched) and ask again against
                # exactly them instead of guessing or losing them.
                mark_executed(self._action_store, task, failed=True)
                return ConversationResult(
                    text=restore_pending_message,
                    task_id=task.task_id,
                    metadata={"action_decision": "AMBIGUOUS_PRODUCT_REFERENCE", "artifacts": []},
                )

        if status == "ROW_FOUND":
            next_workset = workset_lib.apply_tool_result(workset, data)
            workset_lib.apply_to_task(task, next_workset)
            fields = data.get("product_fields")
            if isinstance(fields, dict):
                task.parameters["bitrix_product_fields"] = dict(fields)
            # Selection is one atomic identity transition: no enrichment or
            # pending-write payload belonging to the previous SKU survives.
            task.parameters.pop("bitrix_enrichment_write_request", None)
            task.parameters.pop("product_enrichment_result", None)
            task.parameters["bitrix_retail_price_preview"] = str(
                data.get("retail_price_preview") or ""
            )
            self._action_store.put(task)
            mark_executed(self._action_store, task, failed=False)
            return ConversationResult(
                text=format_tool_user_text(
                    family=FAMILY_EXCEL, data=data, success=True, artifacts=[]
                ),
                task_id=task.task_id,
                metadata={
                    "action_decision": "SELECT_CANONICAL_PRODUCT",
                    "artifacts": [],
                    "canonical_product_selection": True,
                },
            )

        if status == "FIELD_VALUE":
            # Production defect closure (Defect 2): a specific-attribute
            # question about the currently selected product ("what
            # quantity does this product have") is answered from the
            # canonical row itself -- never by re-showing the generic
            # product card, never inventing a value.
            #
            # Defect B (SINGLE -> MULTI -> SINGLE recovery): when this
            # turn's OWN text resolved a product deterministically
            # because there was no prior SINGLE selection (``data
            # ["resolved_selection"]`` -- see ``DataIntelligenceService.
            # execute_structured_plan_via_model``), restore canonical
            # SINGLE scope and persist that selection FIRST, in this SAME
            # turn, exactly like an explicit ``ROW_FOUND`` selection
            # would -- never a second, competing selection mechanism.
            resolved = data.get("resolved_selection")
            if isinstance(resolved, dict):
                self._persist_resolved_selection(task, workset, resolved)
            mark_executed(self._action_store, task, failed=False)
            column = str(data.get("column") or "")
            label = column or str(data.get("field_label") or "").strip() or "запрошенное поле"
            if data.get("present"):
                reply_text = f"{label}: {data.get('value')}"
            else:
                reply_text = f"В исходных данных нет значения для «{label}»."
            return ConversationResult(
                text=reply_text,
                task_id=task.task_id,
                metadata={"action_decision": "FIELD_QUERY", "artifacts": []},
            )

        if status == "WRITE_PLAN_REQUESTED":
            # Production defect closure (Defect 4): "show me what would be
            # written to Bitrix" for the CURRENTLY SELECTED product,
            # recognized by the SAME one-shot model call regardless of
            # wording/language -- dispatches into the EXISTING, unchanged
            # deterministic write-plan renderer
            # (``resolve_bitrix_write_plan_question`` ->
            # ``_explain_bitrix_write_plan``), reading the SAME canonical
            # ``bitrix_product_fields``/``bitrix_retail_price_preview``
            # state a prior selection already persisted. Never a second
            # write-plan implementation, never a real Bitrix write.
            #
            # Defect B (SINGLE -> MULTI -> SINGLE recovery): see the SAME
            # comment on ``FIELD_VALUE`` above -- restore/persist the
            # resolved selection BEFORE building the write-plan preview so
            # it reads the newly-selected product's own fields, not stale
            # ones from before the scope was lost.
            resolved = data.get("resolved_selection")
            if isinstance(resolved, dict):
                self._persist_resolved_selection(task, workset, resolved)
            write_plan_action = resolve_bitrix_write_plan_question(
                text,
                active=task,
                store=self._action_store,
                request_id=str(request.request_id or request.correlation_id or ""),
            )
            if write_plan_action.decision == EXPLAIN_BITRIX_WRITE_PLAN:
                mark_executed(self._action_store, task, failed=False)
                result = await self._explain_bitrix_write_plan(request, write_plan_action)
                return ConversationResult(
                    text=result.text,
                    task_id=result.task_id or task.task_id,
                    metadata=dict(result.metadata or {}),
                )
            mark_executed(self._action_store, task, failed=True)
            return ConversationResult(
                text=str(write_plan_action.user_message or ""),
                task_id=task.task_id,
                metadata={"action_decision": write_plan_action.decision, "artifacts": []},
            )

        if status == "AMBIGUOUS":
            # Human product resolution (generic, data-driven): the
            # one-shot model call recognized a product-selection intent
            # but the deterministic resolver
            # (``DataIntelligenceService._resolve_product_reference``)
            # found more than one plausible candidate in the CURRENT
            # dataset -- never guessed; returns the SAME clarification
            # text/candidate list ``format_tool_user_text`` already knows
            # how to render for this family. Workset scope/selection is
            # unchanged (no candidate was actually selected).
            #
            # Clarification-continuation (generic, data-driven -- see the
            # pending-ambiguity check at the top of this method): the
            # minimum candidate identity (row_index/matched column &
            # value/summary, never full product data) is PERSISTED on
            # this SAME ``ActiveTask`` so the VERY NEXT turn's natural
            # discriminator resolves against THESE candidates first,
            # instead of re-searching the whole Workset.
            candidate_rows = data.get("candidate_rows")
            if isinstance(candidate_rows, list) and candidate_rows:
                task.parameters["pending_product_ambiguity"] = {
                    "dataset_id": workset.current_dataset_id,
                    "candidates": [dict(c) for c in candidate_rows if isinstance(c, Mapping)],
                }
            else:
                task.parameters.pop("pending_product_ambiguity", None)
            self._action_store.put(task)
            mark_executed(self._action_store, task, failed=True)
            return ConversationResult(
                text=format_tool_user_text(family=FAMILY_EXCEL, data=data, success=True, artifacts=[]),
                task_id=task.task_id,
                metadata={"action_decision": "AMBIGUOUS_PRODUCT_REFERENCE", "artifacts": []},
            )

        if status == "OK":
            new_dataset_id = str(data.get("dataset_id") or "")
            if new_dataset_id:
                if data.get("result_scope") == workset_lib.SCOPE_SINGLE:
                    selected = tuple(str(x) for x in (data.get("selected_identifiers") or ()))
                    next_workset = workset_lib.advance_dataset_version(
                        workset,
                        dataset_id=new_dataset_id,
                        scope=workset_lib.SCOPE_SINGLE,
                        identifiers=selected,
                    )
                    product = data.get("selected_product")
                    if isinstance(product, dict):
                        fields = product.get("product_fields")
                        if isinstance(fields, dict):
                            task.parameters["bitrix_product_fields"] = dict(fields)
                        task.parameters.pop("bitrix_enrichment_write_request", None)
                        task.parameters["bitrix_retail_price_preview"] = str(
                            product.get("retail_price_preview") or ""
                        )
                else:
                    next_workset = workset_lib.apply_tool_result(workset, data)
                workset_lib.apply_to_task(task, next_workset)
                self._action_store.put(task)
            artifacts = artifacts_from_tool_data(data, tool_id=EXCEL_CONTRACT.tool_id)
            mark_executed(
                self._action_store,
                task,
                artifact_ids=tuple(str(a.get("ref") or "") for a in artifacts if a.get("ref")),
                failed=False,
            )

            reply = format_tool_user_text(family=FAMILY_EXCEL, data=data, success=True, artifacts=artifacts)
            selected = data.get("selected_product")
            if isinstance(selected, dict) and selected.get("summary_text"):
                reply = f"{reply}\n\n{selected['summary_text']}"
            return ConversationResult(
                text=reply,
                task_id=task.task_id,
                metadata={
                    "action_decision": "CALL_TOOL",
                    "artifacts": artifacts,
                    "follow_up_kind": None,
                    "canonical_table_execution": True,
                    "table_operation_preview": _table_operation_preview(data),
                },
            )

        if status in ("", "NOT_APPLICABLE"):
            # Either nothing was even attempted, or the model itself
            # returned a genuine, validly-parsed judgment that this text
            # is not a table operation (``data_intel.nl_plan_llm.
            # STATUS_NOT_APPLICABLE``) -- safe to defer to the existing
            # managed-agent/legacy routing for this turn exactly as
            # before.
            return None

        # PRODUCTION DEFECT CLOSURE (PR #93): every OTHER status
        # (``MODEL_ERROR``/``PARSE_ERROR``/``VALIDATION_ERROR`` -- see
        # ``data_intel.nl_plan_llm.ModelPlanError``) is a TECHNICAL
        # failure of the model-plan boundary itself, never a judgment
        # about the user's text. A real production request over a
        # column name containing a comma ("Предоплата, Цена с НДС")
        # failed exactly this way (the model could not reproduce the
        # column string verbatim -- since fixed by resolving columns via
        # stable ids instead, see ``data_intel.nl_plan_llm._column_ids``)
        # -- and that technical failure was previously folded into the
        # SAME outcome as "not a table operation", silently letting the
        # turn fall through to managed-agent product selection/
        # enrichment. Never again: fail closed here with a normal,
        # user-safe message and STOP -- returning ``None`` would let this
        # turn continue on to the managed agent as if it had never been a
        # table-operation attempt at all.
        mark_executed(self._action_store, task, failed=True)
        return ConversationResult(
            text=(
                "Не удалось выполнить операцию над таблицей — данные не изменены. "
                "Попробуйте переформулировать запрос."
            ),
            task_id=task.task_id,
            metadata={
                "action_decision": "CALL_TOOL",
                "artifacts": [],
                "follow_up_kind": None,
                "canonical_table_execution": False,
                "table_operation_failed_closed": True,
                "table_operation_failure_status": status,
                "table_operation_failure_reason": str(data.get("reason_code") or ""),
            },
        )

    async def _auto_prepare_site_ready_card_if_needed(self, request: ConversationRequest, task) -> None:
        """PRODUCT-FIRST DEFECT CLOSURE: the user's business intent (any of
        "подготовь этот товар для сайта" / "сделай карточку товара" /
        "покажи, что будет записано на сайт" / "добавь этот товар в
        Bitrix" / a bare "покажи план записи ... в Bitrix", or an explicit
        write confirmation with no prior preview turn at all) must resolve
        to the SAME end goal -- a complete, site-ready product card --
        without the user ever uttering an internal workflow term
        ("enrichment"/"обогащение"/"SEO"/"характеристики"/"галерея"). This
        is the ONE seam both ``_explain_bitrix_write_plan`` (the read-only
        preview) and ``_invoke_controlled_bitrix_write`` (the governed
        write) call BEFORE building/reusing a ``SingleProductWriteRequest``
        -- if the active FAMILY_EXCEL task already selected a single
        product (``bitrix_product_fields['sku']``) but no card has been
        prepared yet (``bitrix_enrichment_write_request`` absent), this
        runs the EXISTING, unchanged ``product_enrichment_bridge.
        prepare_complete_card`` pipeline exactly once and persists its
        result onto the SAME task -- reusing the EXISTING
        ``CALL_PRODUCT_ENRICHMENT`` machinery/state shape, never a new
        pipeline. Already-prepared state is reused as-is (idempotent
        no-op); this never re-runs enrichment twice for the same task.

        Explicit user limits always win: ``has_explicit_price_list_only_
        constraint`` skips this stage entirely (the card stays exactly the
        bare spreadsheet row, same as before this defect closure);
        ``has_explicit_no_media_constraint``/``has_explicit_no_description_
        constraint`` still run the pipeline but omit images/descriptions
        from the persisted card. A preparation failure (e.g. no research
        backend configured) degrades silently to the pre-existing bare
        fallback -- this auto-preparation must never break an otherwise
        working preview/write turn."""
        from business_assistant.action_continuation import (
            FAMILY_EXCEL,
            has_explicit_no_description_constraint,
            has_explicit_no_media_constraint,
            has_explicit_price_list_only_constraint,
        )

        if task is None or getattr(task, "family", None) != FAMILY_EXCEL:
            return
        if dict(task.parameters.get("bitrix_enrichment_write_request") or {}):
            return
        product_fields = dict(task.parameters.get("bitrix_product_fields") or {})
        if not product_fields.get("sku"):
            return

        text = str(request.text or "")
        if has_explicit_price_list_only_constraint(text):
            return

        import dataclasses

        from business_assistant.product_enrichment_bridge import (
            prepare_complete_card,
            serialize_characteristic_status,
            serialize_write_request,
        )

        retail_price = str(task.parameters.get("bitrix_retail_price_preview") or "")
        skip_media = has_explicit_no_media_constraint(text)
        try:
            result = await prepare_complete_card(
                tenant_id=str(request.tenant_id or task.tenant_id or ""),
                product_fields=product_fields,
                retail_price=retail_price,
                bitrix_bridge=self._bitrix_bridge,
                tool_gateway=self._tool_gateway,
                media_fetcher=None if skip_media else self._media_fetcher,
                cache=self._enrichment_cache,
            )
        except Exception:  # noqa: BLE001 -- auto-preparation must never fail an otherwise working preview/write turn
            return

        write_request = result["write_request"]
        if has_explicit_no_description_constraint(text):
            write_request = dataclasses.replace(write_request, short_description="", detailed_description="")

        task.parameters["bitrix_enrichment_write_request"] = serialize_write_request(write_request)
        task.parameters["bitrix_enrichment_characteristic_status"] = serialize_characteristic_status(
            result["enrichment"]
        )
        task.parameters["bitrix_enrichment_preview"] = dict(result["enrichment_preview"])
        self._action_store.put(task)

    async def _invoke_controlled_bitrix_write(
        self, request: ConversationRequest, action
    ) -> ConversationResult:
        """PANDA -- first controlled production Bitrix product write (PR #43
        conversational glue). Dispatches ``action.decision ==
        CALL_CONTROLLED_BITRIX_WRITE`` straight to
        ``business_assistant.controlled_bitrix_write.execute_single_product_write``
        -- NOT through ``self._tool_gateway`` (this is not a ToolGateway-
        registered tool) -- so PR #43's own gateway/HITL/idempotency
        protections (``IntegrationActivationService.execute_via_gateway``)
        remain the single, unduplicated approval/write boundary."""
        from business_assistant.action_continuation import (
            CALL_CONTROLLED_BITRIX_WRITE,
            mark_executed,
        )
        from business_assistant.controlled_bitrix_write import (
            build_write_request_from_fields,
            execute_single_product_write,
            format_bitrix_write_result_text,
        )

        task = action.task
        idem = str(action.idempotency_key or request.request_id or "")
        if idem and idem in self._executed_keys:
            return ConversationResult(
                text=(
                    "Этот товар уже был создан по этому подтверждению — "
                    "повторная запись не выполняется."
                ),
                task_id=getattr(task, "task_id", None),
                metadata={
                    "action_decision": CALL_CONTROLLED_BITRIX_WRITE,
                    "duplicate": True,
                    "artifacts": [],
                },
            )
        if self._bitrix_bridge is None:
            if task is not None:
                mark_executed(self._action_store, task, failed=True)
            return ConversationResult(
                text="Запись в Bitrix сейчас недоступна — интеграция не настроена.",
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CALL_CONTROLLED_BITRIX_WRITE, "artifacts": []},
            )

        # Product-first defect closure: a governed write confirmation with
        # no prior "show the plan" turn at all (e.g. straight from
        # "Установи розничную цену..." to "Подтверждаю: создай этот товар
        # в Bitrix.") must still write the SAME complete, site-ready card
        # -- never a bare spreadsheet-only request -- so the auto-
        # preparation seam runs here too, before ``bitrix_enrichment_
        # write_request`` is read below. A no-op once already prepared.
        if task is not None:
            await self._auto_prepare_site_ready_card_if_needed(request, task)

        args = dict(action.arguments or {})
        retail_price = str(args.get("retail_price") or "")
        product_fields = dict(args.get("product_fields") or {})
        # Existing canonical product identity for THIS confirmation turn --
        # ``resolve_bitrix_write_confirmation`` already resolves ``fields``
        # from the active task's OWN ``bitrix_product_fields`` (never from
        # the confirmation text) and refuses to reach here at all unless
        # ``sku`` is present, so this is always the strongest verified
        # identity available at this boundary.
        canonical_sku = str(product_fields.get("sku") or "").strip()
        # Product enrichment pipeline follow-up: if this SAME task already
        # went through "Подготовь полную карточку..." (CALL_PRODUCT_ENRICHMENT,
        # see ``_invoke_product_enrichment``), reuse that enriched write
        # request (characteristics/content/media -- see
        # ``product_enrichment_bridge.build_enriched_write_request``)
        # instead of rebuilding a bare one from ``product_fields`` alone.
        # The confirmation turn's own retail price always wins (the owner
        # may confirm a different price than the one shown during
        # enrichment preview).
        enriched = dict(getattr(task, "parameters", {}).get("bitrix_enrichment_write_request") or {})
        if enriched:
            enriched_sku = str(enriched.get("sku") or "").strip()
            # Cross-product write-plan safety (production defect closure):
            # a normal product switch already clears ``bitrix_enrichment_
            # write_request`` the moment a NEW row is selected (see the
            # ``ROW_FOUND`` handling in ``_invoke_tool`` above), so in the
            # ordinary flow ``enriched_sku`` and ``canonical_sku`` always
            # agree. If they ever disagree here regardless -- e.g. the
            # active task's product context was replaced/invalidated by
            # some OTHER path without that same cleanup running first --
            # the enriched write request no longer describes the SAME
            # product/write-plan actually bound to this task. Never guess
            # which one the user meant to confirm: fail closed exactly like
            # ``_bitrix_missing_context_decision`` (the existing safe
            # interaction path) and require a fresh plan/confirmation,
            # self-healing the stale enrichment state so the very next
            # "show the plan"/confirmation naturally falls back to the
            # task's own canonical ``bitrix_product_fields``.
            if canonical_sku and enriched_sku and enriched_sku != canonical_sku:
                if task is not None:
                    task.parameters.pop("bitrix_enrichment_write_request", None)
                    task.parameters.pop("bitrix_enrichment_characteristic_status", None)
                    task.parameters.pop("bitrix_enrichment_preview", None)
                    self._action_store.put(task)
                return ConversationResult(
                    text=(
                        "Подготовленный товар изменился с момента показанного плана — "
                        "это подтверждение относится к устаревшим/несовпадающим данным. "
                        "Покажите план записи ещё раз для текущего товара и подтвердите заново."
                    ),
                    task_id=getattr(task, "task_id", None),
                    metadata={
                        "action_decision": CALL_CONTROLLED_BITRIX_WRITE,
                        "write_confirmation_event": "WRITE_CONFIRMATION_REJECTED_INSUFFICIENT_CONTEXT",
                        "artifacts": [],
                    },
                )

            import dataclasses

            from business_assistant.product_enrichment_bridge import deserialize_write_request

            write_request = deserialize_write_request(enriched)
            if retail_price:
                write_request = dataclasses.replace(write_request, retail_price=retail_price)
        else:
            write_request = build_write_request_from_fields(
                product_fields,
                tenant_id=str(request.tenant_id or ""),
                retail_price=retail_price,
            )
        # Production duplicate-create defect closure (real Bitrix IDs
        # 994/995 created for the SAME SKU 32LQ63806LC.ARUG): LiveBitrixAdapter's
        # own durable pre-create idempotency check
        # (``_write_product_create_live``'s deterministic ``xmlId`` lookup)
        # is keyed EXACTLY by the idempotency_key it receives.
        # ``execute_single_product_write`` already falls back to a
        # tenant+sku+title-deterministic key (``_default_idempotency_key``)
        # whenever it is handed an empty one -- but ``idem`` above prefers
        # ``request.request_id``, a FRESH id generated per HTTP call, which
        # defeats that durable check for every independent confirmation
        # turn, even for the exact same product. Only pass an explicit key
        # to the Bitrix write when ``action.idempotency_key`` genuinely
        # carries one (a real caller-supplied idempotency contract);
        # otherwise pass "" so the deterministic default is used instead of
        # this turn's ephemeral request id. ``idem`` itself is unchanged
        # for ``self._executed_keys`` (this gateway's own same-turn replay
        # guard, a separate and still-useful concept).
        bitrix_write_idem = str(action.idempotency_key or "")

        # TCL.xlsx end-to-end defect closure (Step 3 -- LIVE duplicate
        # guard reused by single-product create): re-check the CONNECTED
        # (LIVE or FIXTURE) catalog by article/SKU immediately before this
        # write, via the SAME governed read
        # (``BitrixProductBridge.check_live_existence``) the batch
        # preview/confirmation below already use. This is a genuinely
        # different guard than ``prepare_single_product_write``'s own
        # ``plan_sync`` check (which only ever consults this bridge's
        # local ``self._store`` mapping cache) or LiveBitrixAdapter's
        # xmlId-keyed pre-create idempotency check (only catches a repeat
        # of THIS SAME deterministic key) -- it catches a product that
        # already exists under a DIFFERENT xmlId (e.g. created through any
        # other path/tool for the same real-world SKU), including
        # inactive products. Only gates this conversational entry point;
        # ``execute_single_product_write`` itself (and its own
        # tightly-scripted unit tests) is unchanged.
        tenant_id_for_write = str(request.tenant_id or "")
        try:
            existing_live_matches = self._bitrix_bridge.check_live_existence(
                tenant_id=tenant_id_for_write, sku=write_request.sku
            )
        except Exception:  # noqa: BLE001 -- a failed guard read must never silently block a write; the existing plan_sync check inside execute_single_product_write still applies
            existing_live_matches = []
        if existing_live_matches:
            if len(existing_live_matches) == 1:
                target = dict(existing_live_matches[0])
                bitrix_id = str(target.get("id") or target.get("external_product_id") or "")
                text = (
                    "Товар с этим артикулом/SKU уже существует в Bitrix"
                    f"{f' (Bitrix ID: {bitrix_id})' if bitrix_id else ''}. "
                    "Эта операция не создаёт дубликаты — запись не выполнена."
                )
            else:
                text = (
                    "Не удалось однозначно определить, существует ли этот товар в Bitrix "
                    "(найдено несколько похожих записей по этому артикулу/SKU). Запись не выполнена."
                )
            return ConversationResult(
                text=text,
                task_id=getattr(task, "task_id", None),
                metadata={
                    "action_decision": CALL_CONTROLLED_BITRIX_WRITE,
                    "artifacts": [],
                    "write_confirmation_event": "WRITE_BLOCKED_LIVE_DUPLICATE_GUARD",
                },
            )

        result = execute_single_product_write(
            self._bitrix_bridge,
            tenant_id=tenant_id_for_write,
            request=write_request,
            approved=True,
            idempotency_key=bitrix_write_idem,
        )
        if idem and result.get("mutated"):
            self._executed_keys.add(idem)
        if task is not None:
            mark_executed(self._action_store, task, failed=not result.get("mutated"))
        return ConversationResult(
            text=format_bitrix_write_result_text(result),
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": CALL_CONTROLLED_BITRIX_WRITE,
                "artifacts": [],
                "bitrix_write_result": result,
                "write_confirmation_event": "WRITE_CONFIRMATION_ROUTED_TO_GOVERNED_WRITE",
            },
        )

    async def _maybe_chain_to_enrichment(
        self, request: ConversationRequest, chain_text: str, tool_result: ConversationResult
    ) -> ConversationResult:
        """Production defect closure: uploaded XLSX + generic analyze-only
        reply on the upload turn (no ROW_FOUND yet), followed by "Подготовь
        полную карточку товара <SKU> из загруженного прайса..." on the NEXT
        turn -- ``resolve_product_enrichment_request`` falls back to
        re-running the SAME, unchanged ``data.excel_assistant`` row lookup
        (dispatched as an ordinary CALL_TOOL by ``_invoke_tool`` above) with
        THIS turn's text instead of giving up. If that lookup now resolves a
        single row (ROW_FOUND, already persisted onto ``bitrix_product_
        fields`` by ``_invoke_tool``'s existing FAMILY_EXCEL block), continue
        straight into ``CALL_PRODUCT_ENRICHMENT`` in the SAME turn -- the
        user should never have to repeat their enrichment request. If the
        lookup still cannot resolve a row (e.g. the SKU genuinely is not in
        the file), the original tool reply is returned unchanged."""
        from business_assistant.action_continuation import CALL_PRODUCT_ENRICHMENT, resolve_product_enrichment_request

        active = self._action_store.get(
            tenant_id=str(request.tenant_id or ""),
            owner_id=str(request.user_id or ""),
            conversation_id=str(request.conversation_id or ""),
        )
        enrichment_action = resolve_product_enrichment_request(
            chain_text,
            active=active,
            store=self._action_store,
            request_id=str(request.request_id or request.correlation_id or ""),
        )
        if enrichment_action.decision != CALL_PRODUCT_ENRICHMENT:
            return tool_result
        return await self._invoke_product_enrichment(request, enrichment_action)

    async def _maybe_chain_to_pricing_category_refinement(
        self, request: ConversationRequest, chain_text: str, tool_result: ConversationResult
    ) -> ConversationResult:
        """Production defect closure: a single, brand-new-conversation turn
        that both attaches an XLSX AND asks to take the first product,
        calculate its retail price and resolve the exact Bitrix/Aspro
        category (e.g. "Возьми первый товар из загруженного LG_TV.xlsx и
        подготовь его для Bitrix/Aspro: ... рассчитанную розничную цену,
        точную категорию Bitrix/Aspro ...") -- ``resolve_product_pricing_
        category_refinement_request`` falls back to re-running the SAME,
        unchanged ``data.excel_assistant`` row lookup (dispatched as an
        ordinary CALL_TOOL by ``_invoke_tool`` above, which resolves the
        first row via PR #62's own fallback and persists it onto
        ``bitrix_product_fields``/``bitrix_retail_price_preview``) with
        THIS turn's text instead of failing closed on "no prepared card".
        If that lookup now resolves a single row (ROW_FOUND), continue
        straight into EXPLAIN_BITRIX_WRITE_PLAN in the SAME turn -- the
        user should never have to repeat their request just because the
        file arrived on the same message. If the lookup still cannot
        resolve a row, the original tool reply is returned unchanged.
        Mirrors ``_maybe_chain_to_enrichment`` exactly."""
        from business_assistant.action_continuation import (
            EXPLAIN_BITRIX_WRITE_PLAN,
            resolve_product_pricing_category_refinement_request,
        )

        active = self._action_store.get(
            tenant_id=str(request.tenant_id or ""),
            owner_id=str(request.user_id or ""),
            conversation_id=str(request.conversation_id or ""),
        )
        refinement_action = resolve_product_pricing_category_refinement_request(
            chain_text,
            active=active,
            store=self._action_store,
            request_id=str(request.request_id or request.correlation_id or ""),
        )
        if refinement_action.decision != EXPLAIN_BITRIX_WRITE_PLAN:
            return tool_result
        return await self._explain_bitrix_write_plan(request, refinement_action)

    async def _invoke_product_enrichment(
        self, request: ConversationRequest, action
    ) -> ConversationResult:
        """Product enrichment pipeline follow-up: dispatches
        ``action.decision == CALL_PRODUCT_ENRICHMENT`` straight to
        ``business_assistant.product_enrichment_bridge.prepare_complete_card``
        -- NOT through ``self._tool_gateway`` (mirrors
        ``_invoke_controlled_bitrix_write``'s own reasoning: this composes
        ToolGateway calls internally for research, but is not itself a
        ToolGateway-registered tool). NEVER mutates Bitrix -- at most calls
        the existing, read-only ``prepare_single_product_write`` for a
        resolved-section preview."""
        from business_assistant.action_continuation import CALL_PRODUCT_ENRICHMENT, mark_executed
        from business_assistant.product_enrichment_bridge import (
            prepare_complete_card,
            serialize_characteristic_status,
            serialize_write_request,
        )

        task = action.task
        idem = str(action.idempotency_key or request.request_id or "")
        if idem and idem in self._executed_keys:
            return ConversationResult(
                text="Полная карточка для этого товара уже была подготовлена ранее.",
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CALL_PRODUCT_ENRICHMENT, "duplicate": True, "artifacts": []},
            )

        args = dict(action.arguments or {})
        product_fields = dict(args.get("product_fields") or {})
        retail_price = str(args.get("retail_price") or "")
        tenant_id = str(request.tenant_id or "")
        try:
            result = await prepare_complete_card(
                tenant_id=tenant_id,
                product_fields=product_fields,
                retail_price=retail_price,
                bitrix_bridge=self._bitrix_bridge,
                tool_gateway=self._tool_gateway,
                media_fetcher=self._media_fetcher,
                cache=self._enrichment_cache,
            )
        except Exception:
            if task is not None:
                mark_executed(self._action_store, task, failed=True)
            return ConversationResult(
                text="Не удалось подготовить полную карточку товара — попробуйте ещё раз.",
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CALL_PRODUCT_ENRICHMENT, "artifacts": []},
            )

        if idem:
            self._executed_keys.add(idem)
        if task is not None:
            # Persist the enriched write request (never the raw
            # dataclasses object -- ``ActiveTask.parameters`` is a plain
            # dict) so the LATER, separate explicit "Подтверждаю: создай
            # этот товар в Bitrix..." confirmation turn writes the SAME
            # enriched fields, not just the original bare XLSX row.
            #
            # ``self._action_store.put(task)`` MUST happen before
            # ``mark_executed`` -- ``ActiveTaskStore.get``/``put`` both
            # return/store a ``snapshot()`` (a shallow copy), so ``task``
            # here is already a copy distinct from whatever is currently
            # stored. ``mark_executed`` re-fetches its own fresh snapshot
            # from the store (see the FAMILY_EXCEL/ROW_FOUND callsite
            # above, which follows the same put-before-mark_executed
            # pattern) -- without persisting this mutation first, that
            # re-fetch would silently discard the enriched write request
            # and the later confirmation turn would fall back to the
            # bare, un-enriched XLSX row.
            task.parameters["bitrix_enrichment_write_request"] = serialize_write_request(result["write_request"])
            # Production defect closure: the read-only "покажи точно, что
            # именно будет записано в Bitrix" follow-up must answer from
            # THIS turn's already computed state (per-characteristic
            # verified/probable status and the rendered preview payload)
            # instead of re-running enrichment -- see
            # ``_explain_bitrix_write_plan``.
            task.parameters["bitrix_enrichment_characteristic_status"] = serialize_characteristic_status(
                result["enrichment"]
            )
            task.parameters["bitrix_enrichment_preview"] = dict(result["enrichment_preview"])
            self._action_store.put(task)
            mark_executed(self._action_store, task, failed=False)

        return ConversationResult(
            text=result["text"],
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": CALL_PRODUCT_ENRICHMENT,
                "artifacts": [],
                "enrichment_preview": result["enrichment_preview"],
            },
        )

    async def _explain_bitrix_write_plan(
        self, request: ConversationRequest, action
    ) -> ConversationResult:
        """Production defect closure: answers the read-only follow-up
        "Покажи точно, какие данные из этой карточки будут записаны в
        Bitrix/Aspro, если я подтвержу запись ... Ничего не записывай" from
        the state the prior ``CALL_PRODUCT_ENRICHMENT`` turn persisted on
        the active task. Zero Bitrix mutation: the only Bitrix call here is
        the EXISTING, read-only ``prepare_single_product_write`` -- the same
        one the enrichment preview already uses -- and enrichment itself is
        never re-run."""
        from business_assistant.action_continuation import EXPLAIN_BITRIX_WRITE_PLAN
        from business_assistant.controlled_bitrix_write import prepare_single_product_write
        from business_assistant.product_enrichment_bridge import (
            deserialize_write_request,
            format_write_plan_text,
        )

        task = action.task
        # Product-first defect closure: "покажи план записи этого товара в
        # Bitrix"/"покажи, что будет записано на сайт" must show the SAME
        # complete, site-ready card an explicit enrichment request would
        # have produced -- never the bare spreadsheet-only fallback below
        # -- unless the card was already prepared (no-op) or the user
        # explicitly narrowed it this turn. Runs BEFORE reading ``action.
        # arguments`` (built by the synchronous resolver from whatever
        # state existed at classification time) so a freshly prepared card
        # is used instead of the resolver's own bare fallback.
        if task is not None:
            await self._auto_prepare_site_ready_card_if_needed(request, task)

        args = dict(action.arguments or {})
        if task is not None:
            enriched = dict(task.parameters.get("bitrix_enrichment_write_request") or {})
            if enriched:
                args = {
                    "write_request": enriched,
                    "characteristic_status": dict(
                        task.parameters.get("bitrix_enrichment_characteristic_status") or {}
                    ),
                    "enrichment_preview": dict(task.parameters.get("bitrix_enrichment_preview") or {}),
                    "retail_price": str(
                        args.get("retail_price") or task.parameters.get("bitrix_retail_price_preview") or ""
                    ),
                }
        write_request = deserialize_write_request(dict(args.get("write_request") or {}))
        if not write_request.retail_price and args.get("retail_price"):
            import dataclasses

            write_request = dataclasses.replace(write_request, retail_price=str(args.get("retail_price")))

        write_preview: dict = {}
        if self._bitrix_bridge is not None:
            # Production defect closure: always attempt the EXISTING,
            # read-only ``prepare_single_product_write`` preview -- even
            # when ``retail_price`` is still unknown. That call now
            # resolves the category/section (a fact about the product,
            # not its price) BEFORE checking retail price internally, and
            # still fails closed with ``missing_or_invalid_retail_price``
            # when no price is known -- it just no longer skips the
            # category lookup and EAN echo on the way there. Previously
            # gating this call on ``write_request.retail_price`` being
            # truthy meant the section resolver was never even invoked
            # whenever the price was still unknown.
            try:
                write_preview = prepare_single_product_write(
                    self._bitrix_bridge,
                    tenant_id=str(request.tenant_id or ""),
                    request=write_request,
                )
            except Exception:  # noqa: BLE001 -- a read-only explanation must never fail on the preview call
                write_preview = {}

        text = format_write_plan_text(
            write_request=write_request,
            write_preview=write_preview,
            characteristic_status=dict(args.get("characteristic_status") or {}),
            enrichment_preview=dict(args.get("enrichment_preview") or {}),
        )
        return ConversationResult(
            text=text,
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": EXPLAIN_BITRIX_WRITE_PLAN,
                "artifacts": [],
                "bitrix_write_preview": write_preview,
                "mutated": False,
            },
        )

    async def _check_bitrix_existence_batch(
        self, request: ConversationRequest, action
    ) -> ConversationResult:
        """Batch Bitrix existence-check defect closure: a read-only "which
        rows of the WHOLE uploaded price list already exist in Bitrix,
        which are new, and which are ambiguous" classification -- e.g.
        "Проверь весь прайс перед загрузкой на сайт. Покажи, какие товары
        уже есть в Bitrix, каких нет и где есть неоднозначность." Reuses
        the EXISTING single-product duplicate/read logic
        (``BitrixProductBridge.check_live_existence``) once per row -- the
        SAME governed read boundary every other Bitrix read here already
        uses -- never a new Bitrix client, never
        ``catalog.product.add``/``update``, never one conversational turn
        per row. TCL.xlsx end-to-end defect closure: this now genuinely
        checks the CONNECTED (LIVE or FIXTURE) catalog by article/SKU,
        never ``plan_sync``'s own local ``self._store`` mapping cache
        (correct for FIXTURE, but never populated by a real LIVE create,
        so it could never see what already exists remotely -- exactly the
        "checks existing products in LIVE Bitrix" requirement this
        closure is for). Rows are read through the EXISTING
        ``data.excel_assistant`` tool's ``canonical_identity_rows``
        operation, which reuses the SAME cached column-role schema/
        extraction the single-row ``ROW_FOUND`` lookup already uses -- no
        second role-detection pass, no second dataset store. The frozen
        NEW/``READY_TO_CREATE`` row list is stored on the task so a later
        explicit batch-write confirmation (``_confirm_batch_bitrix_create``)
        creates exactly what was previewed here, never a live re-scan at
        confirmation time."""
        from business_assistant.action_continuation import CHECK_BITRIX_EXISTENCE_BATCH
        from tools.models import ToolRequest

        task = action.task
        dataset_id = str(dict(action.arguments or {}).get("dataset_id") or "")
        tenant_id = str(request.tenant_id or "")

        def _fail(message: str) -> ConversationResult:
            return ConversationResult(
                text=message,
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CHECK_BITRIX_EXISTENCE_BATCH, "artifacts": [], "mutated": False},
            )

        if self._bitrix_bridge is None:
            return _fail("Проверка по Bitrix сейчас недоступна — интеграция не настроена.")
        if self._tool_gateway is None or not dataset_id:
            return _fail(
                "Не вижу загруженного прайс-листа для проверки по Bitrix. "
                "Сначала приложите файл, затем попросите проверить товары по Bitrix."
            )

        # LIVE Bitrix uses the same per-tenant IntegrationActivationService
        # connection lifecycle as the governed single-product write path.
        # Bootstrap it ONCE before the batch loop; without this, every
        # check_live_existence() call reaches resolve_connection() with no
        # ACTIVE connection for the current tenant and is collapsed below
        # into one ``bitrix_check_failed`` row after another. FIXTURE/
        # SANDBOX keep their existing behavior because this helper is a
        # documented no-op outside LIVE.
        try:
            self._bitrix_bridge.ensure_live_connection_ready(tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 -- read-only preview fails closed
            return _fail(
                "Не удалось подключиться к Bitrix для проверки прайс-листа. "
                "Ничего не записано."
            )

        from business_assistant.action_continuation import EXCEL_CONTRACT

        tool_request = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="",
            task_id=getattr(task, "task_id", None) or str(uuid.uuid4()),
            tool_id=EXCEL_CONTRACT.tool_id,
            operation="canonical_identity_rows",
            arguments={"dataset_id": dataset_id},
            requested_capabilities=tuple(EXCEL_CONTRACT.required_capabilities),
            tenant_id=tenant_id,
            user_id=str(request.user_id or ""),
            actor_id=f"{tenant_id}:{request.user_id or ''}",
        )
        try:
            result = await self._tool_gateway.invoke(tool_request, capabilities=self._tool_capabilities)
        except Exception:  # noqa: BLE001 -- a read-only batch check must never raise
            result = None
        if result is None or not getattr(result, "success", False):
            return _fail("Не удалось прочитать загруженный прайс-лист для проверки по Bitrix.")
        rows = list(dict(getattr(result, "data", None) or {}).get("rows") or [])

        new_rows: list[dict] = []
        existing_rows: list[dict] = []
        ambiguous_rows: list[dict] = []
        invalid_rows: list[dict] = []

        for row in rows:
            title = str(row.get("title") or "")
            sku = str(row.get("sku") or "")
            source_row = row.get("row_source_row")
            # Product identity contract (TCL.xlsx defect closure): a
            # reliable canonical SKU/article/model identity (``sku`` here
            # is ALREADY SKU-or-article-or-model, see
            # ``canonical_identity_rows``) is sufficient on its own for a
            # Bitrix existence/duplicate lookup. ``title`` is descriptive
            # metadata only -- never a mandatory uniqueness key -- so a
            # row is invalid ONLY when there is genuinely no usable
            # identity at all (no sku/article/model). Never invent a
            # title to satisfy this check.
            if not sku:
                invalid_rows.append(
                    {
                        "source_row": source_row,
                        "sku": sku,
                        "title": title,
                        "reason": "missing_sku_or_title",
                        "planned_action": "SKIP_INVALID",
                    }
                )
                continue
            try:
                # Genuine existence check against the CONNECTED (LIVE or
                # FIXTURE) catalog -- never a mutation (see
                # ``BitrixProductBridge.check_live_existence``'s own
                # docstring).
                matches = self._bitrix_bridge.check_live_existence(tenant_id=tenant_id, sku=sku)
            except Exception:  # noqa: BLE001 -- one row's failure must never abort the whole batch
                invalid_rows.append(
                    {
                        "source_row": source_row,
                        "sku": sku,
                        "title": title,
                        "reason": "bitrix_check_failed",
                        "planned_action": "SKIP_INVALID",
                    }
                )
                continue
            if not matches:
                # Step 5 batch write closure: freeze the SAME flat fields
                # ``build_write_request_from_fields`` already reads for the
                # single-product path (ean/category/brand/purchase_price/
                # retail_price) alongside identity, so the later batch
                # create confirmation can build each row's write request
                # from THIS frozen preview -- never a fresh dataset re-read
                # at confirmation time.
                new_rows.append(
                    {
                        "source_row": source_row,
                        "sku": sku,
                        "title": title,
                        "ean": str(row.get("ean") or ""),
                        "category": str(row.get("category") or ""),
                        "brand": str(row.get("brand") or ""),
                        "purchase_price": str(row.get("purchase_price") or ""),
                        "retail_price": str(row.get("retail_price") or ""),
                        "planned_action": "READY_TO_CREATE",
                    }
                )
            elif len(matches) == 1:
                target = dict(matches[0])
                existing_rows.append(
                    {
                        "source_row": source_row,
                        "sku": sku,
                        "title": title,
                        "bitrix_id": str(target.get("id") or target.get("external_product_id") or ""),
                        "planned_action": "SKIP_EXISTS",
                    }
                )
            else:
                ambiguous_rows.append(
                    {
                        "source_row": source_row,
                        "sku": sku,
                        "title": title,
                        "candidates": [
                            str(m.get("id") or m.get("external_product_id") or "") for m in matches
                        ],
                        "planned_action": "SKIP_AMBIGUOUS",
                    }
                )

        total = len(rows)
        lines = [
            "ПРОВЕРКА ПРАЙСА ПО BITRIX (только предпросмотр, ничего не записано):",
            f"ВСЕГО ТОВАРОВ: {total}",
            "",
            f"НОВЫЕ / ГОТОВЫ К СОЗДАНИЮ (READY_TO_CREATE): {len(new_rows)}",
        ]
        # Display-only fallback label when ``title`` is absent -- purely
        # cosmetic (never written back into the row/write-request data,
        # never treated as a real title): a strong sku/article/model
        # identity is enough to list/lookup a row even with no title.
        def _display_title(value: str) -> str:
            return value or "(без названия, есть артикул/SKU)"

        for r in new_rows:
            row_prefix = f"[стр. {r['source_row']}] " if r.get("source_row") is not None else ""
            lines.append(f"  - {row_prefix}{r['sku']} — {_display_title(r['title'])}")
        lines.append("")
        lines.append(f"УЖЕ СУЩЕСТВУЮТ В BITRIX: {len(existing_rows)}")
        for r in existing_rows:
            row_prefix = f"[стр. {r['source_row']}] " if r.get("source_row") is not None else ""
            bitrix_id_suffix = f" (Bitrix ID: {r['bitrix_id']})" if r.get("bitrix_id") else ""
            lines.append(f"  - {row_prefix}{r['sku']} — {_display_title(r['title'])}{bitrix_id_suffix}")
        lines.append("")
        lines.append(f"НЕОДНОЗНАЧНЫЕ (несколько возможных совпадений в Bitrix): {len(ambiguous_rows)}")
        for r in ambiguous_rows:
            row_prefix = f"[стр. {r['source_row']}] " if r.get("source_row") is not None else ""
            lines.append(f"  - {row_prefix}{r['sku']} — {_display_title(r['title'])}")
        lines.append("")
        lines.append(f"ТРЕБУЮТ УТОЧНЕНИЯ (нет артикула/SKU или другая причина): {len(invalid_rows)}")
        for r in invalid_rows:
            row_prefix = f"[стр. {r['source_row']}] " if r.get("source_row") is not None else ""
            sku_label = r["sku"] or "(без артикула)"
            title_label = r["title"] or "(без названия)"
            lines.append(f"  - {row_prefix}{sku_label} — {title_label} [{r['reason']}]")
        lines.append("")
        if new_rows:
            lines.append(
                f"Чтобы создать {len(new_rows)} новых товаров (неактивными, для проверки), "
                "подтвердите отдельным сообщением, например: "
                '"Подтверждаю: создай эти новые товары в Bitrix".'
            )
        lines.append("Ничего в Bitrix не записано: это только предпросмотр.")

        # Step 5 (batch write): freeze the exact approved NEW/READY_TO_CREATE
        # list + dataset on the task now, at preview time -- a later batch
        # confirmation turn creates exactly this list, never a fresh re-scan
        # that could pick up rows the user never actually saw/approved.
        if task is not None:
            task.parameters["bitrix_batch_dataset_id"] = dataset_id
            task.parameters["bitrix_batch_ready_rows"] = list(new_rows)
            self._action_store.put(task)

        return ConversationResult(
            text="\n".join(lines),
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": CHECK_BITRIX_EXISTENCE_BATCH,
                "artifacts": [],
                "mutated": False,
                "bitrix_existence_check": {
                    "total": total,
                    "new": new_rows,
                    "ready_to_create": new_rows,
                    "existing": existing_rows,
                    "ambiguous": ambiguous_rows,
                    "invalid": invalid_rows,
                },
            },
        )

    async def _confirm_batch_bitrix_create(
        self, request: ConversationRequest, action
    ) -> ConversationResult:
        """TCL.xlsx end-to-end defect closure (Step 5 -- minimal safe batch
        write): executes the EXISTING single-product governed write
        primitive (``business_assistant.controlled_bitrix_write.
        execute_single_product_write``) once per row of the FROZEN
        ``bitrix_batch_ready_rows`` list a prior ``CHECK_BITRIX_EXISTENCE_
        BATCH`` preview stored on the task -- NEVER a second/parallel
        batch-write mechanism, and never a fresh dataset re-read at
        confirmation time (only what the owner actually saw/approved in
        the preview is ever attempted). One deterministic loop, zero
        additional agent/model turns.

        Before EACH row's create, re-checks live existence via the SAME
        ``BitrixProductBridge.check_live_existence`` the preview already
        used (Step 3's live duplicate guard) -- this is what makes a
        rerun of the same batch operation safe: a row already created by
        a previous confirmation of this same frozen list (or by any other
        path, e.g. a concurrent single-product write for the same SKU) is
        skipped, never re-created. Every create is inactive, exactly like
        the single-product path's own contract."""
        from business_assistant.action_continuation import CONFIRM_BATCH_BITRIX_CREATE, mark_executed
        from business_assistant.controlled_bitrix_write import (
            build_write_request_from_fields,
            execute_single_product_write,
        )

        task = action.task
        args = dict(action.arguments or {})
        ready_rows = list(args.get("ready_rows") or [])
        tenant_id = str(request.tenant_id or "")

        def _fail(message: str) -> ConversationResult:
            return ConversationResult(
                text=message,
                task_id=getattr(task, "task_id", None),
                metadata={"action_decision": CONFIRM_BATCH_BITRIX_CREATE, "artifacts": [], "mutated": False},
            )

        if self._bitrix_bridge is None:
            return _fail("Запись в Bitrix сейчас недоступна — интеграция не настроена.")
        if not ready_rows:
            return _fail(
                "Нет подготовленных новых товаров для создания. Сначала попросите "
                "проверить прайс-лист по Bitrix, затем подтвердите создание."
            )

        row_lines: list[str] = []
        created_count = 0
        skipped_count = 0
        failed_count = 0

        for row in ready_rows:
            sku = str(row.get("sku") or "")
            title = str(row.get("title") or "")
            source_row = row.get("source_row")
            row_prefix = f"[стр. {source_row}] " if source_row is not None else ""
            sku_label = sku or "(без артикула)"
            title_label = title or "(без названия)"

            if not sku or not title:
                skipped_count += 1
                row_lines.append(f"  - {row_prefix}{sku_label} — {title_label}: пропущено (нет артикула/названия)")
                continue

            try:
                # Pre-create live duplicate re-check -- Step 3's guard,
                # reused unchanged. Protects a rerun of the SAME batch
                # (this row may have been created by an earlier
                # confirmation of this exact frozen list) and any
                # concurrent single-product write for the same SKU.
                matches = self._bitrix_bridge.check_live_existence(tenant_id=tenant_id, sku=sku)
            except Exception:  # noqa: BLE001 -- one row's failure must never abort the whole batch
                failed_count += 1
                row_lines.append(f"  - {row_prefix}{sku_label} — {title_label}: ошибка проверки дублей, не создан")
                continue

            if matches:
                skipped_count += 1
                if len(matches) == 1:
                    target = dict(matches[0])
                    bitrix_id = str(target.get("id") or target.get("external_product_id") or "")
                    row_lines.append(
                        f"  - {row_prefix}{sku_label} — {title_label}: уже существует в Bitrix"
                        f"{f' (ID: {bitrix_id})' if bitrix_id else ''}, не создан повторно"
                    )
                else:
                    row_lines.append(
                        f"  - {row_prefix}{sku_label} — {title_label}: неоднозначно "
                        "(несколько совпадений в Bitrix), не создан"
                    )
                continue

            write_request = build_write_request_from_fields(
                {
                    "title": title,
                    "sku": sku,
                    "ean": row.get("ean") or "",
                    "category": row.get("category") or "",
                    "brand": row.get("brand") or "",
                    "purchase_price": row.get("purchase_price") or "",
                },
                tenant_id=tenant_id,
                retail_price=str(row.get("retail_price") or ""),
            )
            # Empty idempotency_key -- exactly like the single-product path
            # (``_invoke_controlled_bitrix_write``) -- so
            # ``execute_single_product_write`` falls back to its own
            # tenant+sku+title-deterministic default key rather than any
            # ephemeral per-turn id, keeping this row's create durably
            # idempotent on Bitrix's own side too.
            result = execute_single_product_write(
                self._bitrix_bridge,
                tenant_id=tenant_id,
                request=write_request,
                approved=True,
                idempotency_key="",
            )
            if result.get("mutated"):
                created_count += 1
                row_lines.append(
                    f"  - {row_prefix}{sku_label} — {title_label}: создан "
                    f"(Bitrix ID: {result.get('bitrix_product_id')}, неактивен)"
                )
            else:
                failed_count += 1
                row_lines.append(
                    f"  - {row_prefix}{sku_label} — {title_label}: не создан "
                    f"({result.get('status') or 'unknown'})"
                )

        if task is not None:
            mark_executed(self._action_store, task, failed=(created_count == 0 and bool(ready_rows)))

        lines = [
            "ПАКЕТНОЕ СОЗДАНИЕ НОВЫХ ТОВАРОВ В BITRIX (неактивными):",
            f"Создано: {created_count}",
            f"Пропущено (уже существуют/неоднозначно/нет данных): {skipped_count}",
            f"Ошибки записи: {failed_count}",
            "",
        ]
        lines.extend(row_lines)

        return ConversationResult(
            text="\n".join(lines),
            task_id=getattr(task, "task_id", None),
            metadata={
                "action_decision": CONFIRM_BATCH_BITRIX_CREATE,
                "artifacts": [],
                "mutated": created_count > 0,
                "bitrix_batch_create_result": {
                    "created": created_count,
                    "skipped": skipped_count,
                    "failed": failed_count,
                    "total": len(ready_rows),
                },
            },
        )

    async def _respond_direct_image_edit(
        self, request: ConversationRequest, *, instruction: str
    ) -> ConversationResult:
        """Deterministic image.edit dispatch for the direct "Редактировать"
        action (production acceptance defect closure). Builds the exact same
        shape of CALL_TOOL decision ``resolve_action_turn`` would produce for
        FAMILY_IMAGE_EDIT, but without any text classification -- the target
        artifact is already unambiguous (validated in ``_invoke_tool`` via
        ``ArtifactService.resolve_trusted_image_source``), so there is no
        "last generated image"/NLU guessing involved.
        """

        from business_assistant.action_continuation import (
            ActionDecision,
            ActiveTask,
            CALL_TOOL,
            FAMILY_IMAGE_EDIT,
            IMAGE_EDIT_CONTRACT,
            NEW_TASK,
            READY_TO_EXECUTE,
            STATUS_READY,
            user_unavailable_message,
        )

        task_id = str(uuid.uuid4())
        if self._tool_gateway is None:
            return ConversationResult(
                text=user_unavailable_message(FAMILY_IMAGE_EDIT),
                task_id=task_id,
                metadata={"action_decision": "FAIL_UNAVAILABLE", "artifacts": []},
            )
        task = ActiveTask(
            task_id=task_id,
            tenant_id=str(request.tenant_id or ""),
            owner_id=str(request.user_id or ""),
            conversation_id=str(request.conversation_id or ""),
            family=FAMILY_IMAGE_EDIT,
            tool_id=IMAGE_EDIT_CONTRACT.tool_id,
            operation=IMAGE_EDIT_CONTRACT.operation,
            goal=instruction,
            parameters={"instruction": instruction},
            artifact_type=IMAGE_EDIT_CONTRACT.artifact_type,
            status=STATUS_READY,
            risk=IMAGE_EDIT_CONTRACT.risk,
        )
        action = ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=NEW_TASK,
            task=task,
            arguments={"instruction": instruction},
            tool_id=IMAGE_EDIT_CONTRACT.tool_id,
            operation=IMAGE_EDIT_CONTRACT.operation,
            idempotency_key=str(request.request_id or ""),
        )
        self.last_action_decision = action
        return await self._invoke_tool(request, action)

    async def respond(self, request: ConversationRequest) -> ConversationResult:
        if self._workflow_engine is None or self._run_router is None or self._context_manager is None:
            raise ConversationUnavailableError("panda_intelligence_not_configured")
        text = str(request.text or "").strip()
        if not text:
            raise ConversationUnavailableError("empty_message")

        # Production acceptance defect closure ("ChatGPT-like generated image
        # actions"): the direct "Редактировать" UI action already knows the
        # exact target artifact -- dispatch straight to the existing
        # image.edit tool path instead of the ambiguous conversational
        # family-detection heuristic below (resolve_action_turn), which today
        # never classifies free text into FAMILY_IMAGE_EDIT at all. This is
        # not a second pipeline: it reuses the same _invoke_tool()/ToolGateway/
        # ArtifactService/image.edit machinery every other tool call uses.
        if str(request.image_edit_source_ref or "").strip():
            return await self._respond_direct_image_edit(request, instruction=text)

        from business_assistant.action_continuation import (
            ANSWER_TEXT,
            ASK_CLARIFICATION,
            CALL_CONTROLLED_BITRIX_WRITE,
            CALL_PRODUCT_ENRICHMENT,
            CALL_TOOL,
            CHECK_BITRIX_EXISTENCE_BATCH,
            CONFIRM_BATCH_BITRIX_CREATE,
            EXPLAIN_BITRIX_WRITE_PLAN,
            FAIL_UNAVAILABLE,
            REQUEST_APPROVAL,
            is_batch_bitrix_create_confirmation,
            is_batch_bitrix_existence_check_request,
            is_explicit_bitrix_write_confirmation,
            resolve_action_turn,
        )
        from business_assistant.follow_up import build_follow_up_prompt, resolve_follow_up
        from task_queue.lanes import WORKLOAD_INTERACTIVE

        t0 = time.monotonic()
        resolution = resolve_follow_up(text, history=request.history or ())
        prompt = build_follow_up_prompt(text, resolution)
        # Block 4.28.2: the resolved style/tone/length/language directive is
        # appended to the model prompt only -- classification below
        # (resolve_action_turn) always runs on the original, unmodified
        # `text`, so a style preference can never change which tool/agent is
        # selected, and CALL_TOOL replies (format_tool_user_text) below never
        # see this directive at all.
        if request.style_directive:
            prompt = f"{prompt}\n\n[{request.style_directive}]"
        follow_up_ms = int((time.monotonic() - t0) * 1000)
        task_id = str(uuid.uuid4())

        # Block 5.1 chat integration (spec section 12): family detection needs
        # to know -- deterministically, before any tool call -- whether THIS
        # turn attached a spreadsheet, so "attachment + free text" alone
        # routes to Excel without the user ever naming "Excel mode". Resolves
        # through the same trusted, tenant/conversation-verified boundary
        # _invoke_tool() uses for the actual tool call below; cheap (no blob
        # fetch), and resolving twice per turn is a harmless bounded cost.
        spreadsheet_attachment_count = 0
        spreadsheet_refs_this_turn: list[dict] = []
        if self._artifact_service is not None and request.attachment_refs:
            try:
                _pre_resolved = self._artifact_service.resolve_trusted_refs(
                    tenant_id=str(request.tenant_id or ""),
                    conversation_id=str(request.conversation_id or ""),
                    refs=tuple(request.attachment_refs),
                )
            except Exception:
                _pre_resolved = []
            spreadsheet_refs_this_turn = [
                r for r in _pre_resolved if str(r.get("kind") or "") == "spreadsheet"
            ]
            spreadsheet_attachment_count = len(spreadsheet_refs_this_turn)

        # PANDA -- Managed Agent integration boundary (PR #74 integration
        # block, disabled by default). ONE narrow adapter call, gated by
        # PANDA_MANAGED_AGENT_ENABLED (default false): when off, the import
        # below still costs nothing extra behaviorally, but
        # managed_agent_enabled() short-circuits before anything in
        # managed_agent_poc/ is touched, so production behavior is exactly
        # the same as before this integration existed. When on AND this
        # turn is eligible (state/attachment-based only -- see
        # managed_agent_poc.panda_bridge, never a text/phrase check), the
        # existing, already-proven managed-agent runtime (PR #74) answers
        # this turn directly using ONLY its 3 read-only tools
        # (analyze_spreadsheet/select_product/explain_bitrix_write_plan)
        # over the SAME, unmodified data_intel dataset -- the legacy
        # resolve_action_turn() phrase-based routing below never runs for
        # this turn. Any failure (SDK missing, no key, timeout, ...)
        # degrades to None and falls straight through to resolve_action_turn
        # exactly as if this block did not exist.
        #
        # PART 3 of the managed-agent -> governed Bitrix write confirmation
        # defect closure (PR #87 follow-up): an explicit write confirmation
        # (the SAME EXISTING canonical ``is_explicit_bitrix_write_confirmation``
        # classifier the legacy path already uses -- never a second phrase
        # list, never LLM tool-selection "preference") must NEVER enter the
        # managed-agent tool selector at all -- the managed agent exposes no
        # write tool (see its own module docstring), so a real production
        # confirmation message was previously misrouted into
        # ``explain_bitrix_write_plan`` (a read-only re-display of the plan)
        # instead of ever reaching the governed write path below. Gating
        # here -- purely on THIS turn's raw text, exactly the same signal
        # ``resolve_action_turn`` itself already keys off of at its own
        # confirmation check -- lets an explicit confirmation fall straight
        # through to the EXISTING ``resolve_action_turn()``/
        # ``resolve_bitrix_write_confirmation()``/``CALL_CONTROLLED_BITRIX_
        # WRITE`` chain unchanged. Every other managed-agent-eligible turn
        # (including a genuine "show/review the plan" question, which is
        # NOT a confirmation -- see ``is_explicit_bitrix_write_confirmation``'s
        # own docstring) is completely unaffected and keeps going through
        # the managed agent exactly as before.
        #
        # Production defect closure (batch Bitrix existence-check turn
        # silently swallowed by the managed-agent boundary): the managed
        # agent exposes only 3 read-only tools (analyze_spreadsheet/
        # select_product/explain_bitrix_write_plan -- see this module's own
        # docstring) and NO batch/whole-dataset Bitrix existence check, so
        # whenever the managed agent is enabled AND this turn is eligible
        # (state-based only -- an EARLIER turn's spreadsheet upload makes
        # every LATER turn in the SAME conversation eligible, regardless of
        # its own wording), a "Проверь весь прайс перед загрузкой на сайт.
        # Покажи, какие товары уже есть в Bitrix..." turn reached the model
        # with no matching tool, which then answered "I cannot check
        # Bitrix / please provide an export" from general knowledge --
        # exactly like the write-confirmation exclusion immediately above,
        # but for PR #103's ``CHECK_BITRIX_EXISTENCE_BATCH`` seam instead
        # of the governed write. Gated on the SAME EXISTING, purely textual
        # ``is_batch_bitrix_existence_check_request`` predicate
        # ``resolve_action_turn`` already uses to dispatch this action --
        # never a second predicate/implementation, never a phrase-specific
        # response hack. Every other managed-agent-eligible turn is
        # completely unaffected and keeps going through the managed agent
        # exactly as before.
        from managed_agent_poc.panda_bridge import managed_agent_enabled

        if (
            managed_agent_enabled()
            and not is_explicit_bitrix_write_confirmation(text)
            and not is_batch_bitrix_existence_check_request(text)
            # TCL.xlsx end-to-end defect closure (Step 5): the managed
            # agent has no batch-create tool either, so an explicit
            # "Подтверждаю: создай эти новые товары в Bitrix." must be
            # excluded exactly like the two predicates immediately above,
            # for the SAME reason -- otherwise the model answers from
            # general knowledge instead of running the real governed
            # per-row create loop.
            and not is_batch_bitrix_create_confirmation(text)
        ):
            from managed_agent_poc.panda_bridge import maybe_respond_via_managed_agent

            # CANONICAL WORKSET (single business-data ownership): establish/
            # refresh the ONE authoritative business-data context for THIS
            # conversation BEFORE the managed agent runs, whenever a
            # spreadsheet is attached this turn -- see
            # ``_establish_canonical_workset_from_attachment``'s own
            # docstring for why this never depends on, mutates, or
            # synchronizes with ``managed_agent_poc``'s own private dataset.
            # This is what lets a LATER turn -- whether the managed agent
            # keeps handling it, or it falls back to the legacy
            # ``resolve_action_turn`` path (e.g. after a ``MaxTurnsExceeded``
            # the managed agent's own read-only tools cannot satisfy) --
            # resolve the SAME dataset without ever asking the user to
            # reattach the file.
            #
            # Final-review correction (fail-safe, never fail-open): when a
            # spreadsheet IS attached this turn but the canonical (shared
            # data_intel) ingest above fails, the managed agent must be
            # SKIPPED entirely for this turn -- never run on a fresh
            # attachment whose canonical ownership could not be
            # established, which would otherwise let
            # ``managed_agent_poc``'s own private dataset become the ONLY
            # authoritative continuation context (the split-ownership
            # condition this module exists to remove). Falling through
            # (``managed_result`` stays ``None``) routes this turn through
            # the EXISTING ``resolve_action_turn()`` safe-failure/
            # clarification path below -- no new failure/error mechanism
            # is introduced. A turn with NO spreadsheet attached this turn
            # is completely unaffected (``skip_managed_agent_this_turn``
            # stays ``False``).
            skip_managed_agent_this_turn = False
            if spreadsheet_refs_this_turn:
                canonical_established = await self._establish_canonical_workset_from_attachment(
                    request, spreadsheet_refs_this_turn[0]
                )
                if not canonical_established:
                    skip_managed_agent_this_turn = True

            canonical_table_result = None
            if not skip_managed_agent_this_turn:
                # CANONICAL TABLE EXECUTION (NL -> structured operation ->
                # existing deterministic executor): tried BEFORE the
                # managed-agent boundary below, over THIS conversation's
                # canonical Workset (established/refreshed above) -- the
                # managed agent's own 3 read-only tools have no bulk/
                # structural table-transform capability at all (see
                # ``runtime_subprocess.py``'s own module docstring), so it
                # must never "compete" with a genuine table-wide operation
                # this EXISTING executor can already satisfy deterministically.
                # Returns a result ONLY when the EXISTING NL->IR compiler
                # (``data_intel.nl_ops.compile_request``, reached through
                # the SAME ``data.excel_assistant`` tool call every other
                # FAMILY_EXCEL turn already uses) actually compiled and
                # executed a genuine transform this turn -- every other
                # outcome (no dataset yet, single-row lookup, plain
                # analysis, ambiguous) returns ``None`` here with zero side
                # effects, so the managed agent still handles every other
                # conversational/product turn exactly as before.
                canonical_table_result = await self._maybe_execute_canonical_table_operation(request, text)

            if canonical_table_result is not None:
                self._record_latency(t0, follow_up_ms)
                meta = dict(canonical_table_result.metadata or {})
                meta["follow_up_kind"] = resolution.kind
                meta["follow_up_target"] = resolution.target
                return ConversationResult(
                    text=canonical_table_result.text,
                    task_id=canonical_table_result.task_id or task_id,
                    metadata=meta,
                )

            managed_result = None
            if not skip_managed_agent_this_turn:
                managed_result = await maybe_respond_via_managed_agent(
                    text=text,
                    tenant_id=str(request.tenant_id or ""),
                    owner_id=str(request.user_id or ""),
                    conversation_id=str(request.conversation_id or ""),
                    artifact_service=self._artifact_service,
                    spreadsheet_ref=spreadsheet_refs_this_turn[0] if spreadsheet_refs_this_turn else None,
                    # Production defect closure (degraded raw-row card): the
                    # SAME existing deterministic capabilities the legacy
                    # CALL_PRODUCT_ENRICHMENT path below already uses -- never
                    # a second tool_gateway/bitrix_bridge/media_fetcher/cache
                    # instance. Lets the managed-agent boundary DELEGATE a
                    # resolved product selection into the existing Product
                    # Enrichment / controlled Bitrix write-plan pipeline
                    # instead of answering from the raw tool projection.
                    tool_gateway=self._tool_gateway,
                    bitrix_bridge=self._bitrix_bridge,
                    media_fetcher=self._media_fetcher,
                    enrichment_cache=self._enrichment_cache,
                )
            if managed_result is not None:
                self._record_latency(t0, follow_up_ms)
                meta = dict(managed_result.get("metadata") or {})
                # PART 2 of the managed-agent -> governed Bitrix write
                # confirmation defect closure: this gateway -- never
                # ``managed_agent_poc``/``panda_bridge`` -- is the sole
                # owner of durable ``ActiveTaskStore`` state (see
                # ``_persist_managed_agent_product_context``'s own
                # docstring). Persisting BEFORE this early return is what
                # lets a LATER explicit confirmation turn (which now skips
                # the managed agent entirely -- see this method's own PART 3
                # gate above) resolve the SAME canonical product/write
                # context through the EXISTING, unmodified
                # ``resolve_bitrix_write_confirmation``/
                # ``_invoke_controlled_bitrix_write`` chain.
                self._persist_managed_agent_product_context(request, meta)
                meta["follow_up_kind"] = resolution.kind
                meta["follow_up_target"] = resolution.target
                return ConversationResult(
                    text=str(managed_result.get("text") or ""),
                    task_id=task_id,
                    metadata=meta,
                )

        action = resolve_action_turn(
            text,
            tenant_id=request.tenant_id,
            owner_id=request.user_id,
            conversation_id=str(request.conversation_id or ""),
            store=self._action_store,
            follow_up=resolution,
            gateway=self._tool_gateway,
            request_id=str(request.request_id or request.correlation_id or ""),
            spreadsheet_attachment_count=spreadsheet_attachment_count,
        )
        self.last_action_decision = action

        if action.decision == CALL_CONTROLLED_BITRIX_WRITE:
            result = await self._invoke_controlled_bitrix_write(request, action)
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )

        if action.decision == CALL_TOOL and self._tool_gateway is not None:
            result = await self._invoke_tool(request, action)
            chain_text = str(getattr(action, "chain_to_enrichment_text", "") or "")
            if chain_text:
                result = await self._maybe_chain_to_enrichment(request, chain_text, result)
            pricing_category_chain_text = str(
                getattr(action, "chain_to_pricing_category_refinement_text", "") or ""
            )
            if pricing_category_chain_text:
                result = await self._maybe_chain_to_pricing_category_refinement(
                    request, pricing_category_chain_text, result
                )
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )
        if action.decision == EXPLAIN_BITRIX_WRITE_PLAN:
            result = await self._explain_bitrix_write_plan(request, action)
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )
        if action.decision == CALL_PRODUCT_ENRICHMENT:
            result = await self._invoke_product_enrichment(request, action)
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )
        if action.decision == CHECK_BITRIX_EXISTENCE_BATCH:
            result = await self._check_bitrix_existence_batch(request, action)
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )
        if action.decision == CONFIRM_BATCH_BITRIX_CREATE:
            result = await self._confirm_batch_bitrix_create(request, action)
            self._record_latency(t0, follow_up_ms)
            meta = dict(result.metadata or {})
            meta["follow_up_kind"] = resolution.kind
            meta["follow_up_target"] = resolution.target
            return ConversationResult(
                text=result.text,
                workflow_id=result.workflow_id,
                task_id=result.task_id or task_id,
                metadata=meta,
            )
        if action.decision in {ASK_CLARIFICATION, FAIL_UNAVAILABLE, REQUEST_APPROVAL} or (
            action.decision == ANSWER_TEXT and action.user_message
        ):
            self._record_latency(t0, follow_up_ms)
            return ConversationResult(
                text=action.user_message,
                task_id=getattr(action.task, "task_id", None) or task_id,
                metadata={
                    "follow_up_kind": resolution.kind,
                    "follow_up_target": resolution.target,
                    "action_decision": action.decision,
                    "artifacts": [],
                },
            )
        if action.decision == CALL_TOOL and self._tool_gateway is None:
            from business_assistant.action_continuation import (
                FAMILY_IMAGE_GENERATE,
                user_unavailable_message,
            )

            self._record_latency(t0, follow_up_ms)
            family = getattr(action.task, "family", FAMILY_IMAGE_GENERATE)
            return ConversationResult(
                text=user_unavailable_message(family),
                task_id=getattr(action.task, "task_id", None) or task_id,
                metadata={
                    "follow_up_kind": resolution.kind,
                    "follow_up_target": resolution.target,
                    "action_decision": FAIL_UNAVAILABLE,
                    "artifacts": [],
                },
            )

        async def _run_router(**kwargs):
            kwargs["follow_up_kind"] = resolution.kind
            kwargs["classification_text"] = text
            return await self._run_router(**kwargs)

        try:
            result = await self._workflow_engine.execute(
                prompt,
                self._mode,
                self._role,
                context_manager=self._context_manager,
                run_router=_run_router,
                task_id=task_id,
                tenant_id=request.tenant_id,
                request_id=request.request_id or request.correlation_id,
                user_id=request.user_id,
                actor_ref=f"{request.tenant_id}:{request.user_id}",
                # Business Assistant conversational replies are always a live user
                # waiting synchronously for a response -- server-classified, never
                # derived from request.text, so it cannot be spoofed by message content.
                workload_class=WORKLOAD_INTERACTIVE,
            )
        except Exception as exc:
            raise ConversationUnavailableError(str(exc) or "panda_intelligence_failed") from exc
        reply = extract_assistant_text(result if isinstance(result, dict) else {})
        self._record_latency(t0, follow_up_ms)
        return ConversationResult(
            text=reply,
            workflow_id=getattr(self._workflow_engine, "last_workflow_id", None),
            task_id=task_id,
            metadata={
                "role": (result or {}).get("role") if isinstance(result, dict) else None,
                "confidence": (result or {}).get("confidence") if isinstance(result, dict) else None,
                "follow_up_kind": resolution.kind,
                "follow_up_target": resolution.target,
                "action_decision": action.decision,
            },
        )


class FakePandaConversationGateway:
    """Test double — zero network, injectable response."""

    def __init__(self, *, response: str = "Panda intelligence response", calls: list | None = None):
        self.response = response
        self.calls = calls if calls is not None else []

    async def respond(self, request: ConversationRequest) -> ConversationResult:
        self.calls.append(request)
        return ConversationResult(
            text=self.response,
            workflow_id="wf-fake",
            task_id="task-fake",
            metadata={"fake": True},
        )
