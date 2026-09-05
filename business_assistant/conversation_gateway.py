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
    ):
        self._workflow_engine = workflow_engine
        self._run_router = run_router
        self._context_manager = context_manager
        self._mode = mode
        self._role = role
        self._tool_gateway = tool_gateway
        if action_store is None:
            from business_assistant.action_continuation import ActiveTaskStore

            action_store = ActiveTaskStore()
        self._action_store = action_store
        self._tool_capabilities = tool_capabilities
        self._executed_keys: set[str] = set()
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
            artifacts_from_tool_data,
            format_tool_user_text,
            mark_executed,
        )
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
        from business_assistant.action_continuation import CONTRACTS

        contract = CONTRACTS.get(family)
        if contract is not None:
            provided = tuple(contract.required_capabilities)
        if self._tool_capabilities is not None:
            provided = tuple(self._tool_capabilities.capabilities) or provided
        tool_request = ToolRequest(
            request_id=str(request.request_id or uuid.uuid4()),
            workflow_id="",
            task_id=str(task.task_id if task else uuid.uuid4()),
            tool_id=action.tool_id,
            operation=action.operation or "generate",
            arguments=dict(action.arguments or {}),
            requested_capabilities=provided,
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
        if idem and success:
            self._executed_keys.add(idem)
        artifacts = artifacts_from_tool_data(data, tool_id=action.tool_id) if success else []
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

    async def respond(self, request: ConversationRequest) -> ConversationResult:
        if self._workflow_engine is None or self._run_router is None or self._context_manager is None:
            raise ConversationUnavailableError("panda_intelligence_not_configured")
        text = str(request.text or "").strip()
        if not text:
            raise ConversationUnavailableError("empty_message")
        from business_assistant.action_continuation import (
            ANSWER_TEXT,
            ASK_CLARIFICATION,
            CALL_TOOL,
            FAIL_UNAVAILABLE,
            REQUEST_APPROVAL,
            resolve_action_turn,
        )
        from business_assistant.follow_up import build_follow_up_prompt, resolve_follow_up

        t0 = time.monotonic()
        resolution = resolve_follow_up(text, history=request.history or ())
        prompt = build_follow_up_prompt(text, resolution)
        follow_up_ms = int((time.monotonic() - t0) * 1000)
        task_id = str(uuid.uuid4())

        action = resolve_action_turn(
            text,
            tenant_id=request.tenant_id,
            owner_id=request.user_id,
            conversation_id=str(request.conversation_id or ""),
            store=self._action_store,
            follow_up=resolution,
            gateway=self._tool_gateway,
            request_id=str(request.request_id or request.correlation_id or ""),
        )
        self.last_action_decision = action

        if action.decision == CALL_TOOL and self._tool_gateway is not None:
            result = await self._invoke_tool(request, action)
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
