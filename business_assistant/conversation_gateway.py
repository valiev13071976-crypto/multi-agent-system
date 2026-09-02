"""Governed adapter from Business Assistant to existing Panda AI core."""

from __future__ import annotations

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


def extract_assistant_text(result: dict[str, Any]) -> str:
    for key in ("best_solution", "summary", "analysis"):
        value = result.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


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
        if not reply:
            raise ConversationUnavailableError("empty_response")
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
