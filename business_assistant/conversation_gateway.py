"""Governed adapter from Business Assistant to existing Panda AI core."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
            # Block 5.1 multi-turn continuation (spec section 13): persist the
            # resulting dataset_id (new dataset after a transform, or the
            # unchanged source dataset after an analyze/ambiguous turn) BEFORE
            # mark_executed() below re-reads the task from the store, so the
            # next turn can resolve "them"/"it" without re-upload.
            new_dataset_id = str(data.get("dataset_id") or "")
            changed = False
            if new_dataset_id:
                task.parameters["dataset_id"] = new_dataset_id
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
                task.parameters["dataset_id"] = new_dataset_id
                task.family = FAMILY_EXCEL
                task.tool_id = TOOL_DATA_EXCEL_ASSISTANT
                task.operation = "assist"
                task.artifact_type = "workbook"
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

        args = dict(action.arguments or {})
        retail_price = str(args.get("retail_price") or "")
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
            import dataclasses

            from business_assistant.product_enrichment_bridge import deserialize_write_request

            write_request = deserialize_write_request(enriched)
            if retail_price:
                write_request = dataclasses.replace(write_request, retail_price=retail_price)
        else:
            write_request = build_write_request_from_fields(
                dict(args.get("product_fields") or {}),
                tenant_id=str(request.tenant_id or ""),
                retail_price=retail_price,
            )
        result = execute_single_product_write(
            self._bitrix_bridge,
            tenant_id=str(request.tenant_id or ""),
            request=write_request,
            approved=True,
            idempotency_key=idem,
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
        args = dict(action.arguments or {})
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
            EXPLAIN_BITRIX_WRITE_PLAN,
            FAIL_UNAVAILABLE,
            REQUEST_APPROVAL,
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
        if self._artifact_service is not None and request.attachment_refs:
            try:
                _pre_resolved = self._artifact_service.resolve_trusted_refs(
                    tenant_id=str(request.tenant_id or ""),
                    conversation_id=str(request.conversation_id or ""),
                    refs=tuple(request.attachment_refs),
                )
            except Exception:
                _pre_resolved = []
            spreadsheet_attachment_count = sum(
                1 for r in _pre_resolved if str(r.get("kind") or "") == "spreadsheet"
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
