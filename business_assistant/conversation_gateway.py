"""Governed adapter from Business Assistant to existing Panda AI core."""

from __future__ import annotations

import re
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
    ):
        self._workflow_engine = workflow_engine
        self._run_router = run_router
        self._context_manager = context_manager
        self._mode = mode
        self._role = role

    async def respond(self, request: ConversationRequest) -> ConversationResult:
        if self._workflow_engine is None or self._run_router is None or self._context_manager is None:
            raise ConversationUnavailableError("panda_intelligence_not_configured")
        text = str(request.text or "").strip()
        if not text:
            raise ConversationUnavailableError("empty_message")
        task_id = str(uuid.uuid4())
        try:
            result = await self._workflow_engine.execute(
                text,
                self._mode,
                self._role,
                context_manager=self._context_manager,
                run_router=self._run_router,
                task_id=task_id,
                tenant_id=request.tenant_id,
                request_id=request.request_id or request.correlation_id,
                user_id=request.user_id,
                actor_ref=f"{request.tenant_id}:{request.user_id}",
            )
        except Exception as exc:
            raise ConversationUnavailableError(str(exc) or "panda_intelligence_failed") from exc
        reply = extract_assistant_text(result if isinstance(result, dict) else {})
        return ConversationResult(
            text=reply,
            workflow_id=getattr(self._workflow_engine, "last_workflow_id", None),
            task_id=task_id,
            metadata={
                "role": (result or {}).get("role") if isinstance(result, dict) else None,
                "confidence": (result or {}).get("confidence") if isinstance(result, dict) else None,
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
