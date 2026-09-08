"""Business Assistant API orchestration — interaction boundary only."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from business_assistant.errors import (
    BA_APPROVAL_MISMATCH,
    BA_APPROVAL_REQUIRED,
    BA_APPROVAL_STALE,
    BA_CANCELLED,
    BA_CONVERSATION_UNAVAILABLE,
    BA_CROSS_TENANT,
    BA_NOT_FOUND,
    BA_STALE_PREVIEW,
    BusinessAssistantError,
)
from business_assistant.models import (
    BATCH_ROW_THRESHOLD,
    INTENT_CONVERSATIONAL,
    STATUS_BATCH_RUNNING,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_WARNINGS,
    STATUS_FAILED,
    STATUS_PARTIALLY_COMPLETED,
    STATUS_RUNNING,
    STATUS_WAITING_FOR_APPROVAL,
)
from business_assistant.conversation_gateway import (
    ConversationRequest,
    ConversationUnavailableError,
    is_internal_assistant_text,
    select_canonical_final_answer,
)
from business_assistant.follow_up import HistoryTurn
from business_assistant.service import BusinessAssistantService
from business_assistant_api.errors import (
    BAA_ACCESS_DENIED,
    BAA_APPROVAL_STALE,
    BAA_CANCELLED,
    BAA_CONVERSATION_UNAVAILABLE,
    BAA_IDEMPOTENCY_CONFLICT,
    BAA_INVALID_REQUEST,
    BAA_INVALID_STATE,
    BAA_NOT_FOUND,
    BAA_REJECTED,
    BusinessAssistantApiError,
)
from business_assistant_api.titles import auto_title_update, title_metadata_for_create, user_title_update
from business_assistant_api.models import (
    ST_BLOCKED,
    ST_CANCELLED,
    ST_COMPLETED,
    ST_FAILED,
    ST_PLANNING,
    ST_QUEUED,
    ST_RECEIVED,
    ST_REJECTED,
    ST_RESUMING,
    ST_RUNNING,
    ST_VALIDATING,
    ST_WAITING_FOR_APPROVAL,
    TERMINAL_STATES,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    ApiRequestRecord,
    ConversationRecord,
    EV_APPROVAL_RECEIVED,
    EV_APPROVAL_REQUIRED,
    EV_ARTIFACT_CREATED,
    EV_EXECUTION_STARTED,
    EV_PLAN_CREATED,
    EV_PREVIEW_READY,
    EV_REQUEST_ACCEPTED,
    EV_REQUEST_BLOCKED,
    EV_REQUEST_CANCELLED,
    EV_REQUEST_COMPLETED,
    EV_REQUEST_FAILED,
    EV_RESULT_READY,
    EV_RESUME_STARTED,
    EV_VALIDATION_STARTED,
    MessageRecord,
    ProgressEvent,
)
from business_assistant_api.normalizer import NormalizedSubmission, normalize_submission
from business_assistant_api.snapshot import hydrate_ba_service, snapshot_ba_service
from business_assistant_api.store import SqliteBusinessAssistantApiStore
from security.redaction import redact
from security.tenant import require_tenant_id


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Production defect closure (XLSX attachment -> failed response): HTTP 200
# end-to-end told us nothing about the internal outcome -- the request/
# status/result endpoints all returned 200 while Panda produced no usable
# answer. Same pattern already established for the analogous voice-mode
# observability defect (realtime.bridge._log_voice_event / PR #24): reuse
# the standard `logging` module (no new event-bus/telemetry platform), and
# fold the structured payload into the message text itself so it survives
# through whatever log format string is configured in production, not only
# through `extra=`. Never logs secrets/credentials/full attachment content
# -- only bounded, already-non-sensitive correlation identifiers/counters.
_diag_log = logging.getLogger("business_assistant_api.diagnostics")


def _log_ba_event(event: str, *, request_id: str, tenant_id: str = "", **fields) -> None:
    try:
        payload = {"event": event, "request_id": request_id}
        if tenant_id:
            payload["tenant_id"] = tenant_id
        payload.update(fields)
        _diag_log.info(
            "business_assistant_event %s",
            json.dumps(payload, default=str, sort_keys=True),
            extra={"ba_event": payload},
        )
    except Exception:
        pass


_LEGAL = {
    ST_RECEIVED: {ST_VALIDATING, ST_FAILED, ST_CANCELLED},
    ST_VALIDATING: {ST_PLANNING, ST_QUEUED, ST_FAILED, ST_BLOCKED, ST_CANCELLED},
    ST_PLANNING: {ST_QUEUED, ST_RUNNING, ST_FAILED, ST_BLOCKED, ST_CANCELLED},
    ST_QUEUED: {ST_PLANNING, ST_RUNNING, ST_CANCELLED},
    ST_RUNNING: {
        ST_WAITING_FOR_APPROVAL,
        ST_COMPLETED,
        ST_FAILED,
        ST_BLOCKED,
        ST_CANCELLED,
    },
    ST_WAITING_FOR_APPROVAL: {ST_RESUMING, ST_REJECTED, ST_CANCELLED},
    ST_RESUMING: {ST_RUNNING, ST_COMPLETED, ST_FAILED},
}


def _map_ba_status(status: str) -> str:
    if status == STATUS_WAITING_FOR_APPROVAL:
        return ST_WAITING_FOR_APPROVAL
    if status in {STATUS_COMPLETED, STATUS_COMPLETED_WITH_WARNINGS, STATUS_PARTIALLY_COMPLETED}:
        return ST_COMPLETED
    if status == STATUS_FAILED:
        return ST_FAILED
    if status == STATUS_CANCELLED:
        return ST_CANCELLED
    if status == STATUS_BLOCKED:
        return ST_BLOCKED
    if status == STATUS_BATCH_RUNNING:
        return ST_RUNNING
    return ST_RUNNING


class BusinessAssistantApiService:
    """Canonical API/chat interaction layer over closed Business Assistant."""

    def __init__(
        self,
        *,
        store: SqliteBusinessAssistantApiStore,
        ba_service: BusinessAssistantService | None = None,
    ):
        self.store = store
        self.ba = ba_service or BusinessAssistantService()
        self.upload_dir = ""
        # Canonical artifact layer (Block 3.5) -- optional so existing callers
        # that never wire it (e.g. older tests) keep working unchanged.
        self.artifact_service = None
        # Block 4.28: optional canonical personalization layer -- resolved
        # fresh on every conversational turn (submit/submit_async below) so
        # both the text chat pipeline AND the realtime voice pipeline
        # (realtime.bridge, which also calls submit_async) get the exact
        # same, single source of style/tone/length/language preference.
        # None is safe (older tests/callers keep existing behavior).
        self.personalization_service = None

    def close(self) -> None:
        self.store.close()

    def _resolve_style_directive(self, *, tenant_id: str, owner_id: str) -> str:
        if self.personalization_service is None:
            return ""
        try:
            profile = self.personalization_service.resolve_style_profile(
                tenant_id=tenant_id, owner_id=owner_id
            )
        except Exception:
            # Best-effort: a personalization lookup failure must never break
            # the already-working conversational reply path.
            return ""
        return profile.directive_text

    def _fail_conversation_unavailable(self, rec: ApiRequestRecord, exc: BusinessAssistantError) -> None:
        rec.status = ST_FAILED
        rec.error_code = BAA_CONVERSATION_UNAVAILABLE
        rec.error_message = "Panda assistant is temporarily unavailable. Please try again later."
        self._event(rec, EV_REQUEST_FAILED, message=rec.error_message, status=ST_FAILED)
        self.store.save_request(rec)
        self._persist(rec)
        raise BusinessAssistantApiError(
            BAA_CONVERSATION_UNAVAILABLE,
            rec.error_message,
            http_status=503,
        ) from exc

    def _complete_conversational(
        self,
        rec: ApiRequestRecord,
        ex,
        *,
        norm: NormalizedSubmission,
        tenant: str,
        owner_id: str,
        request_id: str,
    ) -> ApiRequestRecord:
        rec.execution_id = ex.execution_id
        rec.workflow_id = ex.workflow_id
        rec.status = ST_COMPLETED
        rec.updated_at = _utc_iso()
        self._event(rec, EV_REQUEST_COMPLETED, message="Request completed", status=ST_COMPLETED)
        self._persist(rec)
        if norm.conversation_id:
            # Non-image artifacts (PDF/XLSX/DOCX/CSV/TXT/generic) generated during
            # this turn are referenced on the assistant message so they survive
            # reload/resume (3.5.3, 3.5.11, 3.5.18-F). Images keep using the
            # existing inline markdown/view_url rendering path unchanged.
            artifact_refs = self._non_image_artifact_refs(getattr(ex, "artifacts", None))
            final_text = select_canonical_final_answer({"summary": ex.summary, "final_answer": ex.summary})
            if not final_text.strip() and norm.artifact_refs:
                # Production defect closure: an HTTP-200 conversational turn
                # that carried an attachment but produced no usable final
                # answer is exactly the original XLSX-attachment defect
                # shape. Log it so a future recurrence is diagnosable by
                # request_id without needing another reproduction round.
                _log_ba_event(
                    "conversational_empty_result_with_attachment",
                    request_id=request_id,
                    tenant_id=tenant,
                    execution_id=getattr(ex, "execution_id", ""),
                    attachment_count=len(norm.artifact_refs),
                )
            self._append_message(
                tenant,
                norm.conversation_id,
                role="assistant",
                content=final_text,
                request_id=request_id,
                artifact_refs=artifact_refs,
            )
            self.store.touch_conversation(
                conversation_id=norm.conversation_id, tenant_id=tenant, owner_id=owner_id
            )
        return rec

    def _complete_business_workflow(
        self,
        rec: ApiRequestRecord,
        ba_req,
        *,
        norm: NormalizedSubmission,
        tenant: str,
        owner_id: str,
        request_id: str,
    ) -> ApiRequestRecord:
        plan = self.ba.build_plan(request_id=ba_req.request_id, tenant_id=tenant)
        rec.plan_id = plan.plan_id
        rec.plan_fingerprint = plan.fingerprint
        self._event(
            rec,
            EV_PLAN_CREATED,
            message=f"Plan created ({plan.recipe})",
            metadata={"steps": len(plan.steps), "recipe": plan.recipe},
        )
        self._transition(rec, ST_RUNNING)
        self._event(rec, EV_EXECUTION_STARTED, message="Execution started")
        ex = self.ba.execute(plan_id=plan.plan_id, tenant_id=tenant)
        rec.execution_id = ex.execution_id
        rec.workflow_id = ex.workflow_id
        self._sync_from_execution(rec, ex)
        self._persist(rec)
        if norm.conversation_id:
            self._append_message(
                tenant,
                norm.conversation_id,
                role="assistant",
                content=self._safe_summary(rec),
                request_id=request_id,
                artifact_refs=self._non_image_artifact_refs(getattr(ex, "artifacts", None)),
            )
            self.store.touch_conversation(
                conversation_id=norm.conversation_id, tenant_id=tenant, owner_id=owner_id
            )
        return rec

    def _submit_prepare(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        message: str,
        artifact_refs: list[str] | None = None,
        requested_capability: str | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        read_only: bool = False,
        priority: str | None = None,
        metadata: dict | None = None,
        trace_id: str = "",
    ) -> ApiRequestRecord | tuple[ApiRequestRecord, NormalizedSubmission, str, str, str]:
        tenant = require_tenant_id(tenant_id)
        norm = normalize_submission(
            message=message,
            artifact_refs=artifact_refs,
            requested_capability=requested_capability,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            read_only=read_only,
            priority=priority,
            metadata=metadata,
        )
        if norm.idempotency_key:
            existing = self.store.get_request_by_idempotency(
                tenant_id=tenant, idempotency_key=norm.idempotency_key
            )
            if existing:
                if existing.payload_hash != norm.payload_hash:
                    raise BusinessAssistantApiError(
                        BAA_IDEMPOTENCY_CONFLICT, "payload_mismatch", http_status=409
                    )
                return existing

        self._ensure_conversation(tenant, owner_id, norm.conversation_id)
        request_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        now = _utc_iso()
        if norm.conversation_id:
            # 3.5.3: persist the user's attached artifact refs on the message
            # itself so they survive reload/resume, independent of request state.
            self._append_message(
                tenant,
                norm.conversation_id,
                role="user",
                content=norm.message,
                request_id=request_id,
                artifact_refs=tuple(norm.artifact_refs or ()),
            )
            for ref in tuple(norm.artifact_refs or ()):
                self._attach_artifact_to_conversation(tenant, ref, norm.conversation_id)
            conv = self.store.get_conversation(
                tenant_id=tenant, owner_id=owner_id, conversation_id=norm.conversation_id
            )
            auto = auto_title_update(conv.metadata if conv else {}, norm.message)
            if auto:
                self.store.touch_conversation(
                    conversation_id=norm.conversation_id,
                    tenant_id=tenant,
                    owner_id=owner_id,
                    title=auto["title"],
                    metadata_update=auto,
                )
            else:
                self.store.touch_conversation(
                    conversation_id=norm.conversation_id,
                    tenant_id=tenant,
                    owner_id=owner_id,
                )
        rec = ApiRequestRecord(
            request_id=request_id,
            tenant_id=tenant,
            owner_id=owner_id,
            status=ST_RECEIVED,
            message=norm.message,
            created_at=now,
            updated_at=now,
            conversation_id=norm.conversation_id,
            idempotency_key=norm.idempotency_key,
            payload_hash=norm.payload_hash,
            correlation_id=correlation_id,
            trace_id=trace_id or correlation_id,
            artifact_refs=norm.artifact_refs,
            read_only=norm.read_only,
        )
        self.store.save_request(rec)
        self._event(rec, EV_REQUEST_ACCEPTED, message="Request accepted")
        self._transition(rec, ST_VALIDATING)
        self._event(rec, EV_VALIDATION_STARTED, message="Validating request")

        if self._is_batch_request(norm):
            rec.workload_class = WORKLOAD_BATCH
            self._seed_batch_fixture(norm)
            self._transition(rec, ST_QUEUED)
        else:
            rec.workload_class = WORKLOAD_INTERACTIVE

        self._transition(rec, ST_PLANNING)
        return rec, norm, tenant, owner_id, request_id

    # --- submission ---

    def submit(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        message: str,
        artifact_refs: list[str] | None = None,
        requested_capability: str | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        read_only: bool = False,
        priority: str | None = None,
        metadata: dict | None = None,
        trace_id: str = "",
    ) -> ApiRequestRecord:
        prepared = self._submit_prepare(
            tenant_id=tenant_id,
            owner_id=owner_id,
            message=message,
            artifact_refs=artifact_refs,
            requested_capability=requested_capability,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            read_only=read_only,
            priority=priority,
            metadata=metadata,
            trace_id=trace_id,
        )
        if isinstance(prepared, ApiRequestRecord):
            return prepared
        rec, norm, tenant, owner_id, request_id = prepared
        try:
            ba_req = self.ba.submit_request(
                tenant_id=tenant,
                user_id=owner_id,
                text=norm.message,
                artifact_refs=norm.artifact_refs,
                read_only=norm.read_only,
                # Large-batch supplier datasets already routed away from the
                # interactive/conversational path via _is_batch_request()
                # above must keep going through the scale-safe batch
                # workflow engine unchanged -- only non-batch attachments
                # get the new attachment->conversational preference.
                attachments_prefer_conversational=(rec.workload_class != WORKLOAD_BATCH),
            )
            rec.ba_request_id = ba_req.request_id

            if ba_req.intent == INTENT_CONVERSATIONAL:
                self._transition(rec, ST_RUNNING)
                self._event(rec, EV_EXECUTION_STARTED, message="Conversational response")
                try:
                    ex = self.ba.respond_conversationally(
                        request_id=ba_req.request_id,
                        tenant_id=tenant,
                        conversation_id=norm.conversation_id,
                        history=self._history_turns(
                            tenant=tenant,
                            owner_id=owner_id,
                            conversation_id=norm.conversation_id,
                        ),
                        style_directive=self._resolve_style_directive(
                            tenant_id=tenant, owner_id=owner_id
                        ),
                    )
                except BusinessAssistantError as exc:
                    if exc.code == BA_CONVERSATION_UNAVAILABLE:
                        self._fail_conversation_unavailable(rec, exc)
                    raise
                return self._complete_conversational(
                    rec, ex, norm=norm, tenant=tenant, owner_id=owner_id, request_id=request_id
                )

            return self._complete_business_workflow(
                rec, ba_req, norm=norm, tenant=tenant, owner_id=owner_id, request_id=request_id
            )
        except BusinessAssistantError as exc:
            rec.status = ST_FAILED if exc.code != BA_CROSS_TENANT else ST_BLOCKED
            rec.error_code = exc.code
            rec.error_message = redact(str(exc))
            self._event(rec, EV_REQUEST_FAILED, message=rec.error_message, status=rec.status)
            self.store.save_request(rec)
            self._persist(rec)
            raise BusinessAssistantApiError(exc.code, rec.error_message, http_status=422) from exc

    async def submit_async(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        message: str,
        artifact_refs: list[str] | None = None,
        requested_capability: str | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        read_only: bool = False,
        priority: str | None = None,
        metadata: dict | None = None,
        trace_id: str = "",
    ) -> ApiRequestRecord:
        """Async submission — conversational branch awaits Panda gateway without blocking the loop."""
        prepared = self._submit_prepare(
            tenant_id=tenant_id,
            owner_id=owner_id,
            message=message,
            artifact_refs=artifact_refs,
            requested_capability=requested_capability,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            read_only=read_only,
            priority=priority,
            metadata=metadata,
            trace_id=trace_id,
        )
        if isinstance(prepared, ApiRequestRecord):
            return prepared
        rec, norm, tenant, owner_id, request_id = prepared
        try:
            ba_req = self.ba.submit_request(
                tenant_id=tenant,
                user_id=owner_id,
                text=norm.message,
                artifact_refs=norm.artifact_refs,
                read_only=norm.read_only,
                # Large-batch supplier datasets already routed away from the
                # interactive/conversational path via _is_batch_request()
                # above must keep going through the scale-safe batch
                # workflow engine unchanged -- only non-batch attachments
                # get the new attachment->conversational preference.
                attachments_prefer_conversational=(rec.workload_class != WORKLOAD_BATCH),
            )
            rec.ba_request_id = ba_req.request_id

            if ba_req.intent == INTENT_CONVERSATIONAL:
                self._transition(rec, ST_RUNNING)
                self._event(rec, EV_EXECUTION_STARTED, message="Conversational response")
                try:
                    ex = await self.ba.respond_conversationally_async(
                        request_id=ba_req.request_id,
                        tenant_id=tenant,
                        conversation_id=norm.conversation_id,
                        history=self._history_turns(
                            tenant=tenant,
                            owner_id=owner_id,
                            conversation_id=norm.conversation_id,
                        ),
                        style_directive=self._resolve_style_directive(
                            tenant_id=tenant, owner_id=owner_id
                        ),
                    )
                except BusinessAssistantError as exc:
                    if exc.code == BA_CONVERSATION_UNAVAILABLE:
                        self._fail_conversation_unavailable(rec, exc)
                    raise
                return self._complete_conversational(
                    rec, ex, norm=norm, tenant=tenant, owner_id=owner_id, request_id=request_id
                )

            return self._complete_business_workflow(
                rec, ba_req, norm=norm, tenant=tenant, owner_id=owner_id, request_id=request_id
            )
        except BusinessAssistantError as exc:
            rec.status = ST_FAILED if exc.code != BA_CROSS_TENANT else ST_BLOCKED
            rec.error_code = exc.code
            rec.error_message = redact(str(exc))
            self._event(rec, EV_REQUEST_FAILED, message=rec.error_message, status=rec.status)
            self.store.save_request(rec)
            self._persist(rec)
            raise BusinessAssistantApiError(exc.code, rec.error_message, http_status=422) from exc

    def get_request(self, *, tenant_id: str, owner_id: str, request_id: str) -> ApiRequestRecord:
        rec = self._load(tenant_id, request_id)
        if rec.owner_id != owner_id:
            raise BusinessAssistantApiError(BAA_ACCESS_DENIED, http_status=403)
        return rec

    def get_status(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        plan_summary = None
        if rec.plan_id:
            try:
                plan_summary = self.ba.plan_preview(plan_id=rec.plan_id, tenant_id=rec.tenant_id)
            except BusinessAssistantError:
                plan_summary = None
        progress = None
        if rec.execution_id:
            try:
                progress = self.ba.get_status(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
            except BusinessAssistantError:
                progress = None
        return {
            "request_id": rec.request_id,
            "status": rec.status,
            "workflow_id": rec.workflow_id,
            "execution_id": rec.execution_id,
            "correlation_id": rec.correlation_id,
            "trace_id": rec.trace_id,
            "workload_class": rec.workload_class,
            "approval_required": rec.status == ST_WAITING_FOR_APPROVAL,
            "approval_id": rec.approval_id,
            "preview_id": rec.preview_id,
            "plan_summary": self._safe_plan(plan_summary),
            "progress": progress,
            "error_code": rec.error_code,
            "error_message": rec.error_message,
        }

    def list_events(
        self, *, tenant_id: str, owner_id: str, request_id: str, after: str | None = None, limit: int = 200
    ) -> list[ProgressEvent]:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        return self.store.list_events(tenant_id=rec.tenant_id, request_id=request_id, after=after, limit=limit)

    def get_result(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        if not rec.execution_id:
            return {
                "request_id": rec.request_id,
                "status": rec.status,
                "summary": rec.error_message or "No execution yet",
                "final_answer": "",
                "artifacts": [],
                "warnings": [],
                "cost": rec.finops_cost,
            }
        self._hydrate_if_needed(rec)
        result = self.ba.get_result(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
        self._event(rec, EV_RESULT_READY, message="Result ready")
        summary = redact(str(result.get("summary") or ""))
        final_answer = select_canonical_final_answer(
            {
                "final_answer": result.get("final_answer") or summary,
                "experts": result.get("experts"),
                "best_solution": result.get("best_solution"),
                "summary": summary,
                "analysis": result.get("analysis"),
                "mode": result.get("mode"),
            }
        )
        return {
            "request_id": rec.request_id,
            "workflow_id": rec.workflow_id,
            "execution_id": rec.execution_id,
            "status": rec.status,
            "summary": summary,
            "final_answer": final_answer,
            "structured_result": {
                "findings": result.get("findings") or [],
                "published": result.get("published"),
                "mode": result.get("mode"),
            },
            "artifacts": result.get("artifacts") or [],
            "warnings": [],
            "evidence_refs": [],
            "cost": result.get("cost") or rec.finops_cost,
            "correlation_id": rec.correlation_id,
            "error_summary": rec.error_message or None,
        }

    def list_artifacts(self, *, tenant_id: str, owner_id: str, request_id: str) -> list[dict]:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        arts = self.store.list_artifacts(tenant_id=rec.tenant_id, request_id=request_id)
        if rec.execution_id:
            self._hydrate_if_needed(rec)
            ex_result = self.ba.get_result(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
            for a in ex_result.get("artifacts") or []:
                arts.append(
                    {
                        "artifact_id": a.get("ref") or str(uuid.uuid4()),
                        "artifact_type": a.get("type") or "execution_artifact",
                        "ref": a.get("ref") or "",
                        "metadata": {"step": a.get("step")},
                        "content": {},
                    }
                )
        return arts

    def approve(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        request_id: str,
        approval_id: str | None = None,
        plan_fingerprint: str | None = None,
    ) -> ApiRequestRecord:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        if rec.status != ST_WAITING_FOR_APPROVAL:
            raise BusinessAssistantApiError(BAA_INVALID_STATE, "not_waiting_for_approval", http_status=409)
        self._hydrate_if_needed(rec)
        self._transition(rec, ST_RESUMING)
        self._event(rec, EV_RESUME_STARTED, message="Resuming after approval")
        try:
            ex = self.ba.approve(
                execution_id=rec.execution_id,
                tenant_id=rec.tenant_id,
                actor_id=owner_id,
                approval_id=approval_id or rec.approval_id,
                plan_fingerprint=plan_fingerprint or rec.plan_fingerprint,
            )
            if any(s.status == "PENDING" for s in ex.steps.values()):
                ex.status = STATUS_RUNNING
                ex = self.ba.resume(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
        except BusinessAssistantError as exc:
            if exc.code in {BA_APPROVAL_STALE, BA_STALE_PREVIEW}:
                raise BusinessAssistantApiError(BAA_APPROVAL_STALE, exc.code, http_status=409) from exc
            if exc.code == BA_APPROVAL_MISMATCH:
                raise BusinessAssistantApiError(BAA_APPROVAL_STALE, exc.code, http_status=409) from exc
            raise BusinessAssistantApiError(exc.code, str(exc), http_status=422) from exc
        self._event(rec, EV_APPROVAL_RECEIVED, message="Approval received")
        self._sync_from_execution(rec, ex)
        self._persist(rec)
        return rec

    def reject(self, *, tenant_id: str, owner_id: str, request_id: str) -> ApiRequestRecord:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        if rec.status not in {ST_WAITING_FOR_APPROVAL, ST_RUNNING}:
            if rec.status in TERMINAL_STATES:
                raise BusinessAssistantApiError(BAA_INVALID_STATE, "already_terminal", http_status=409)
        self._hydrate_if_needed(rec)
        if rec.execution_id:
            self.ba.reject(execution_id=rec.execution_id, tenant_id=rec.tenant_id, actor_id=owner_id)
        rec.status = ST_REJECTED
        rec.updated_at = _utc_iso()
        self._event(rec, EV_REQUEST_CANCELLED, message="Rejected by user", status=ST_REJECTED)
        self.store.save_request(rec)
        self._persist(rec)
        return rec

    def cancel(self, *, tenant_id: str, owner_id: str, request_id: str) -> ApiRequestRecord:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        if rec.status in TERMINAL_STATES:
            return rec  # idempotent
        self._hydrate_if_needed(rec)
        if rec.execution_id:
            try:
                self.ba.cancel(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
            except BusinessAssistantError as exc:
                if exc.code != BA_CANCELLED:
                    raise BusinessAssistantApiError(exc.code, str(exc), http_status=422) from exc
        rec.status = ST_CANCELLED
        rec.updated_at = _utc_iso()
        self._event(rec, EV_REQUEST_CANCELLED, message="Cancelled", status=ST_CANCELLED)
        self.store.save_request(rec)
        self._persist(rec)
        return rec

    def get_preview(self, *, tenant_id: str, owner_id: str, request_id: str) -> dict:
        rec = self.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
        self._hydrate_if_needed(rec)
        preview = self.ba.get_preview(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
        return {
            "preview_id": preview.preview_id,
            "plan_fingerprint": preview.plan_fingerprint,
            "artifact_checksum": preview.artifact_checksum,
            "changes": list(preview.changes),
            "warnings": list(preview.warnings),
            "external_writes": list(preview.external_writes),
            "approval_required": True,
        }

    def reload_from_store(self) -> "BusinessAssistantApiService":
        """Create fresh BA service hydrated from empty — per-request hydration on access."""
        return BusinessAssistantApiService(store=self.store, ba_service=BusinessAssistantService())

    def create_conversation(self, *, tenant_id: str, owner_id: str, title: str = "New chat") -> ConversationRecord:
        tenant = require_tenant_id(tenant_id)
        cid = str(uuid.uuid4())
        now = _utc_iso()
        conv = ConversationRecord(
            conversation_id=cid,
            tenant_id=tenant,
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
            metadata=title_metadata_for_create(title),
        )
        self.store.save_conversation(conv)
        return conv

    def rename_conversation(
        self, *, tenant_id: str, owner_id: str, conversation_id: str, title: str
    ) -> ConversationRecord:
        tenant = require_tenant_id(tenant_id)
        conv = self.store.get_conversation(
            tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id
        )
        if conv is None:
            raise BusinessAssistantApiError(BAA_NOT_FOUND, http_status=404)
        update = user_title_update(title)
        if not update["title"]:
            raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "empty_title", http_status=400)
        self.store.touch_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant,
            owner_id=owner_id,
            title=update["title"],
            metadata_update=update,
        )
        renamed = self.store.get_conversation(
            tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id
        )
        if renamed is None:
            raise BusinessAssistantApiError(BAA_NOT_FOUND, http_status=404)
        return renamed

    def delete_conversation(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> None:
        tenant = require_tenant_id(tenant_id)
        deleted = self.store.delete_conversation(
            tenant_id=tenant, owner_id=owner_id, conversation_id=conversation_id
        )
        if not deleted:
            raise BusinessAssistantApiError(BAA_NOT_FOUND, http_status=404)
        # Block 3.5.13: cascade-retire this conversation's attachments/
        # generated files too -- best-effort, never blocks the (already
        # committed) conversation deletion above.
        if self.artifact_service is not None:
            try:
                self.artifact_service.delete_artifacts_for_conversation(
                    tenant_id=tenant, conversation_id=conversation_id
                )
            except Exception:
                pass

    def list_conversations(self, *, tenant_id: str, owner_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.list_conversations(tenant_id=require_tenant_id(tenant_id), owner_id=owner_id, limit=limit)
        return [
            {
                "conversation_id": c.conversation_id,
                "title": str(c.metadata.get("title") or "Conversation"),
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in rows
        ]

    def get_conversation_messages(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> list[dict]:
        conv = self.store.get_conversation(
            tenant_id=require_tenant_id(tenant_id), owner_id=owner_id, conversation_id=conversation_id
        )
        if conv is None:
            raise BusinessAssistantApiError(BAA_NOT_FOUND, http_status=404)
        msgs = self.store.list_messages(tenant_id=tenant_id, conversation_id=conversation_id)
        return [
            {
                "message_id": m.message_id,
                "role": m.role,
                "content": m.content,
                "request_id": m.request_id,
                "created_at": m.created_at,
                "artifact_refs": list(m.artifact_refs),
            }
            for m in msgs
        ]

    def upload_attachment(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        upload_base_dir: str,
    ) -> dict:
        from business_assistant_api.uploads import save_upload

        tenant = require_tenant_id(tenant_id)
        try:
            out = save_upload(
                base_dir=upload_base_dir,
                tenant_id=tenant,
                owner_id=owner_id,
                filename=filename,
                content=content,
                mime_type=mime_type,
            )
        except ValueError as exc:
            code = str(exc)
            status = 413 if "too_large" in code else 422
            raise BusinessAssistantApiError(code, http_status=status) from exc
        # 3.5.1/3.5.2: also register through the canonical artifact layer so
        # this same file gets a stable artifact_id usable with the generic
        # metadata/download/view endpoints (additive -- the legacy
        # artifact://upload/... ref and filesystem write above are unchanged).
        if self.artifact_service is not None:
            try:
                rec = self.artifact_service.register_upload(
                    tenant_id=tenant,
                    owner_id=owner_id,
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                    legacy_ref=out.get("artifact_ref", ""),
                )
                out["artifact_id"] = rec.artifact_id
                # 3.5.16: let the composer render an immediate, correct
                # preview/download affordance for this exact upload without
                # a second round-trip to GET /artifacts/{id}.
                public = rec.as_public_dict()
                out["kind"] = public["kind"]
                out["view_url"] = public["view_url"]
                out["download_url"] = public["download_url"]
            except Exception:
                out["artifact_id"] = ""
        else:
            out["artifact_id"] = ""
        return out

    async def edit_generated_image(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        conversation_id: str,
        source_artifact_id: str,
        instruction: str,
    ) -> dict:
        """Direct "Редактировать" action (production acceptance defect
        closure): deterministic image.edit dispatch that targets the EXACT
        artifact the user selected in the normal conversation view.

        Reuses every existing trusted component unchanged -- ArtifactService
        tenant/conversation-verified resolution (fails closed on any
        cross-tenant/foreign/non-image ref, see
        ``WorkflowPandaConversationGateway._invoke_tool``), the existing
        image.edit tool via ToolGateway, existing generated-image artifact
        registration, and existing conversation message persistence. This is
        not a second editing pipeline: it calls the exact same
        ``conversation_gateway.respond()`` entry point every ordinary chat
        turn uses, just without the ambiguous NLU family-detection step
        (unnecessary here -- the artifact and instruction already arrived
        unambiguous from the UI's direct action).
        """

        tenant = require_tenant_id(tenant_id)
        conv = str(conversation_id or "").strip()
        text = str(instruction or "").strip()
        source_ref = str(source_artifact_id or "").strip()
        if not conv:
            raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "conversation_id_required", http_status=400)
        if not text or len(text) > 4000:
            raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "instruction_invalid", http_status=400)
        if not source_ref:
            raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "source_artifact_required", http_status=400)
        gateway = getattr(self.ba, "conversation_gateway", None)
        if gateway is None:
            raise BusinessAssistantApiError(
                BAA_CONVERSATION_UNAVAILABLE, "conversation_gateway_unconfigured", http_status=503
            )

        self._ensure_conversation(tenant, owner_id, conv)
        request_id = str(uuid.uuid4())
        # 3.5.3-style persistence: the instruction and the exact source
        # artifact it targeted survive reload/resume just like any other
        # attached-file turn.
        self._append_message(tenant, conv, role="user", content=text, request_id=request_id, artifact_refs=(source_ref,))
        try:
            result = await gateway.respond(
                ConversationRequest(
                    text=text,
                    tenant_id=tenant,
                    user_id=owner_id,
                    request_id=request_id,
                    conversation_id=conv,
                    image_edit_source_ref=source_ref,
                )
            )
        except ConversationUnavailableError as exc:
            raise BusinessAssistantApiError(BAA_CONVERSATION_UNAVAILABLE, str(exc), http_status=503) from exc
        artifacts = []
        if isinstance(result.metadata, dict):
            artifacts = list(result.metadata.get("artifacts") or [])
        self._append_message(
            tenant,
            conv,
            role="assistant",
            content=result.text,
            request_id=request_id,
            artifact_refs=self._non_image_artifact_refs(artifacts),
        )
        self.store.touch_conversation(conversation_id=conv, tenant_id=tenant, owner_id=owner_id)
        return {"request_id": request_id, "text": result.text, "artifacts": artifacts}

    # --- internals ---

    def _load(self, tenant_id: str, request_id: str) -> ApiRequestRecord:
        rec = self.store.get_request(tenant_id=require_tenant_id(tenant_id), request_id=request_id)
        if rec is None:
            raise BusinessAssistantApiError(BAA_NOT_FOUND, http_status=404)
        return rec

    def _persist(self, rec: ApiRequestRecord) -> None:
        snap = snapshot_ba_service(self.ba)
        if self.ba.integration_activation is not None:
            snap["integration"] = _snapshot_integration(self.ba.integration_activation)
        self.store.save_snapshot(request_id=rec.request_id, snapshot=snap)
        self.store.save_request(rec)

    def _hydrate_if_needed(self, rec: ApiRequestRecord) -> None:
        snap = self.store.load_snapshot(request_id=rec.request_id)
        if snap:
            hydrate_ba_service(self.ba, snap)
            if snap.get("integration") and self.ba.integration_activation is not None:
                _hydrate_integration(self.ba.integration_activation, snap["integration"])

    def _transition(self, rec: ApiRequestRecord, new_status: str) -> None:
        allowed = _LEGAL.get(rec.status, set())
        if rec.status != new_status and new_status not in allowed and rec.status not in TERMINAL_STATES:
            # allow direct mapping from BA on first sync
            if rec.status == ST_RECEIVED and new_status == ST_VALIDATING:
                pass
            elif new_status in TERMINAL_STATES or rec.status in {ST_RUNNING, ST_RESUMING}:
                pass
            else:
                raise BusinessAssistantApiError(
                    BAA_INVALID_STATE, f"{rec.status}->{new_status}", http_status=409
                )
        rec.status = new_status
        rec.updated_at = _utc_iso()
        self.store.save_request(rec)

    def _sync_from_execution(self, rec: ApiRequestRecord, ex) -> None:
        mapped = _map_ba_status(ex.status)
        rec.status = mapped
        rec.updated_at = _utc_iso()
        rec.finops_cost = str(ex.cost)
        if ex.approval:
            rec.approval_id = ex.approval.approval_id
        if ex.preview:
            rec.preview_id = ex.preview.preview_id
            rec.plan_fingerprint = ex.plan_fingerprint
            self._event(rec, EV_PREVIEW_READY, message="Preview ready")
            self._event(rec, EV_APPROVAL_REQUIRED, message="Approval required")
        if mapped == ST_COMPLETED:
            self._event(rec, EV_REQUEST_COMPLETED, message="Request completed", status=ST_COMPLETED)
            self._maybe_create_artifacts(rec, ex)
        elif mapped == ST_BLOCKED:
            self._event(rec, EV_REQUEST_BLOCKED, message="Request blocked", status=ST_BLOCKED)
        elif mapped == ST_FAILED:
            self._event(rec, EV_REQUEST_FAILED, message="Execution failed", status=ST_FAILED)
        # Production defect closure (XLSX attachment -> failed response):
        # a terminal execution that is reported COMPLETED to the caller but
        # (a) had one or more BLOCKED steps, or (b) still ends up with an
        # empty summary/final_answer, is exactly the internal-failure-behind-
        # HTTP-200 shape that made the original defect invisible in
        # production logs. Emit ONE bounded, safe diagnostic line so a real
        # future occurrence is correlatable by request_id alone (never logs
        # message text, attachment content, or secrets).
        blocked_steps = [
            {"step_id": step_id, "code": step.error_code}
            for step_id, step in ex.steps.items()
            if step.status == "BLOCKED" and step.error_code
        ]
        if blocked_steps or (mapped == ST_COMPLETED and not str(ex.summary or "").strip()):
            _log_ba_event(
                "business_workflow_degraded_or_empty_result",
                request_id=rec.request_id,
                tenant_id=rec.tenant_id,
                execution_id=getattr(ex, "execution_id", ""),
                ba_status=ex.status,
                mapped_status=mapped,
                blocked_step_count=len(blocked_steps),
                blocked_step_codes=sorted({b["code"] for b in blocked_steps}),
                attachment_count=len(rec.artifact_refs or ()),
                summary_empty=not str(ex.summary or "").strip(),
            )
        self.store.save_request(rec)

    def _maybe_create_artifacts(self, rec: ApiRequestRecord, ex) -> None:
        for art in ex.artifacts or []:
            aid = str(uuid.uuid4())
            self.store.save_artifact(
                artifact_id=aid,
                request_id=rec.request_id,
                tenant_id=rec.tenant_id,
                owner_id=rec.owner_id,
                artifact_type=str(art.get("type") or "artifact"),
                ref=str(art.get("ref") or ""),
                metadata={"safe": True},
                content={k: v for k, v in art.items() if k not in {"secret", "token"}},
                created_at=_utc_iso(),
            )
            self._event(rec, EV_ARTIFACT_CREATED, message=f"Artifact {aid}", metadata={"artifact_id": aid})
        if rec.workload_class == WORKLOAD_BATCH:
            aid = str(uuid.uuid4())
            self.store.save_artifact(
                artifact_id=aid,
                request_id=rec.request_id,
                tenant_id=rec.tenant_id,
                owner_id=rec.owner_id,
                artifact_type="excel_report",
                ref=f"artifact://excel/{rec.request_id}",
                content={"rows": len(self.ba._fixture_rows), "mode": "FIXTURE"},
                created_at=_utc_iso(),
            )
            self._event(rec, EV_ARTIFACT_CREATED, message="Excel output artifact", metadata={"artifact_id": aid})

    def _event(
        self,
        rec: ApiRequestRecord,
        event_type: str,
        *,
        message: str = "",
        status: str = "",
        metadata: dict | None = None,
    ) -> None:
        ev = ProgressEvent(
            event_id=str(uuid.uuid4()),
            request_id=rec.request_id,
            tenant_id=rec.tenant_id,
            event_type=event_type,
            timestamp=_utc_iso(),
            workflow_id=rec.workflow_id,
            status=status or rec.status,
            message=redact(message),
            metadata=metadata or {},
            correlation_id=rec.correlation_id,
        )
        self.store.save_event(ev)

    def _history_turns(
        self, *, tenant: str, owner_id: str, conversation_id: str | None
    ) -> tuple[HistoryTurn, ...]:
        cid = (conversation_id or "").strip()
        if not cid:
            return ()
        conv = self.store.get_conversation(
            tenant_id=tenant, owner_id=owner_id, conversation_id=cid
        )
        if conv is None:
            return ()
        out: list[HistoryTurn] = []
        for msg in self.store.list_messages(tenant_id=tenant, conversation_id=cid):
            role = str(msg.role or "").lower()
            if role not in {"user", "assistant"}:
                continue
            content = str(msg.content or "").strip()
            if not content:
                continue
            if role == "assistant" and is_internal_assistant_text(content):
                continue
            out.append(HistoryTurn(role=role, content=content))
        return tuple(out)

    def _ensure_conversation(self, tenant: str, owner: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        existing = self.store.get_conversation(
            tenant_id=tenant, owner_id=owner, conversation_id=conversation_id
        )
        if existing:
            return
        now = _utc_iso()
        self.store.save_conversation(
            ConversationRecord(
                conversation_id=conversation_id,
                tenant_id=tenant,
                owner_id=owner,
                created_at=now,
                updated_at=now,
                metadata=title_metadata_for_create(None),
            )
        )

    def _append_message(
        self,
        tenant: str,
        conversation_id: str,
        *,
        role: str,
        content: str,
        request_id: str,
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        self.store.save_message(
            MessageRecord(
                message_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                tenant_id=tenant,
                role=role,
                content=redact(content)[:8000],
                created_at=_utc_iso(),
                request_id=request_id,
                artifact_refs=tuple(artifact_refs or ()),
            )
        )

    def _non_image_artifact_refs(self, artifacts) -> tuple[str, ...]:
        """Canonical artifact_ids for non-image artifacts only -- images keep
        using the existing inline markdown/view_url rendering path so this
        never duplicates an already-working image display (3.5.11)."""

        out: list[str] = []
        for art in artifacts or ():
            if not isinstance(art, dict):
                continue
            kind = str(art.get("artifact_type") or art.get("type") or "")
            if kind == "image":
                continue
            aid = str(art.get("artifact_id") or "")
            if aid:
                out.append(aid)
        return tuple(out)

    def _attach_artifact_to_conversation(self, tenant: str, ref: str, conversation_id: str) -> None:
        svc = self.artifact_service
        if svc is None:
            return
        try:
            svc.attach_to_conversation(tenant_id=tenant, artifact_id=ref, conversation_id=conversation_id)
        except Exception:
            pass

    def _safe_summary(self, rec: ApiRequestRecord) -> str:
        if rec.execution_id:
            try:
                self._hydrate_if_needed(rec)
                r = self.ba.get_result(execution_id=rec.execution_id, tenant_id=rec.tenant_id)
                return redact(str(r.get("summary") or rec.status))
            except BusinessAssistantError:
                pass
        return f"Status: {rec.status}"

    def _safe_plan(self, plan_summary: dict | None) -> dict | None:
        if not plan_summary:
            return None
        return {
            "recipe": plan_summary.get("recipe"),
            "steps": plan_summary.get("steps"),
            "writes": plan_summary.get("writes"),
            "approvals_required": plan_summary.get("approvals_required"),
            "external_mutation": plan_summary.get("external_mutation"),
        }

    def _is_batch_request(self, norm: NormalizedSubmission) -> bool:
        tl = norm.message.casefold()
        if norm.artifact_refs and any("excel" in r.casefold() or "csv" in r.casefold() for r in norm.artifact_refs):
            return True
        if any(w in tl for w in ("excel", "таблиц", "xlsx", "csv")) and any(
            w in tl for w in ("сравни", "compare", "итог", "прайс")
        ):
            return True
        return False

    def _seed_batch_fixture(self, norm: NormalizedSubmission) -> None:
        rows = [{"sku": f"sku-{i}", "price": str(100 + i), "brand": "Samsung"} for i in range(BATCH_ROW_THRESHOLD)]
        self.ba.seed_supplier_fixture(rows=rows, costs={f"sku-{i}": "50" for i in range(BATCH_ROW_THRESHOLD)})


def _snapshot_integration(act) -> dict:
    connections = {}
    for cid, conn in act._connections.items():
        connections[cid] = {
            "connection_id": conn.connection_id,
            "tenant_id": conn.tenant_id,
            "provider_id": conn.provider_id,
            "environment": conn.environment,
            "credential_ref": conn.credential_ref,
            "status": conn.status,
            "owner_id": conn.owner_id,
            "priority": conn.priority,
            "read_capabilities": list(conn.read_capabilities),
            "write_capabilities": list(conn.write_capabilities),
            "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        }
    return {
        "connections": connections,
        "by_tenant": dict(act._by_tenant),
        "secret_values": {f"{t}|{r}": v for (t, r), v in act._secret_values.items()},
    }


def _hydrate_integration(act, data: dict) -> None:
    from integrations.activation.models import IntegrationConnection
    from datetime import datetime

    act._connections = {}
    act._by_tenant = {}
    for cid, c in (data.get("connections") or {}).items():
        lva = c.get("last_verified_at")
        conn = IntegrationConnection(
            connection_id=c["connection_id"],
            tenant_id=c["tenant_id"],
            provider_id=c["provider_id"],
            environment=c["environment"],
            credential_ref=c["credential_ref"],
            status=c["status"],
            owner_id=c.get("owner_id") or "",
            priority=int(c.get("priority") or 100),
            read_capabilities=tuple(c.get("read_capabilities") or ()),
            write_capabilities=tuple(c.get("write_capabilities") or ()),
            last_verified_at=datetime.fromisoformat(lva) if lva else None,
        )
        act._connections[cid] = conn
    for tenant, ids in (data.get("by_tenant") or {}).items():
        act._by_tenant[tenant] = list(ids)
    act._secret_values = {}
    for key, val in (data.get("secret_values") or {}).items():
        tenant, ref = key.split("|", 1)
        act._secret_values[(tenant, ref)] = val
