"""UI Chat service — server-authoritative chat orchestration facade."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from ui_chat.access import (
    assert_attachment_access,
    assert_conversation_access,
    assert_message_access,
    assert_run_access,
    assert_task_access,
)
from ui_chat.attachments import AttachmentRouter, classify_attachment
from ui_chat.errors import (
    ATTACHMENT_AGGREGATE_TOO_LARGE,
    ATTACHMENT_COUNT_EXCEEDED,
    CHAT_EMPTY_TURN,
    CHAT_RUN_NOT_CANCELLABLE,
    TASK_NOT_CANCELLABLE,
    UIChatError,
    VOICE_AUDIO_EMPTY,
    VOICE_AUDIO_TOO_LARGE,
    VOICE_AUDIO_UNSUPPORTED,
    VOICE_MESSAGE_NOT_FOUND,
    VOICE_TRANSCRIPTION_FAILED,
    VOICE_TTS_FAILED,
    VOICE_TTS_TOO_LONG,
)
from ui_chat.markdown import sanitize_filename_display
from ui_chat.models import (
    ATTACH_CLASS_UNKNOWN,
    ATTACH_UPLOADED,
    ATTACH_UPLOADING,
    ROLE_ASSISTANT,
    ROLE_USER,
    RUN_BACKGROUND,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_WAITING_APPROVAL,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    AttachmentRef,
    BackgroundTaskView,
    ChatConversation,
    ChatMessage,
    ChatRun,
    VoiceAudioArtifact,
    VoiceTranscript,
)
from ui_chat.observability import UIChatObservability
from ui_chat.store import UIChatStore
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UIChatService:
    def __init__(
        self,
        store: UIChatStore,
        *,
        attachment_router: AttachmentRouter | None = None,
        stt_provider=None,
        tts_provider=None,
        workflow_engine=None,
        run_router=None,
        context_manager=None,
        workflow_runtime=None,
        memory_scope_factory=None,
        obs: UIChatObservability | None = None,
    ):
        self.store = store
        self.attachments = attachment_router or AttachmentRouter()
        self.stt = stt_provider or FakeSpeechToTextProvider()
        self.tts = tts_provider or FakeTextToSpeechProvider()
        self.workflow_engine = workflow_engine
        self.run_router = run_router
        self.context_manager = context_manager
        self.workflow_runtime = workflow_runtime
        self.memory_scope_factory = memory_scope_factory
        self.obs = obs or UIChatObservability()
        self.max_voice_bytes = int(os.environ.get("UI_CHAT_MAX_VOICE_BYTES") or str(5 * 1024 * 1024))
        self.supported_voice_mimes = frozenset(
            {"audio/wav", "audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4"}
        )

    def create_conversation(
        self, *, tenant_id: str, user_id: str, title: str | None = None
    ) -> ChatConversation:
        now = _utc()
        conv = ChatConversation(
            conversation_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            title=(title or "New conversation").strip()[:200] or "New conversation",
            created_at=now,
            updated_at=now,
        )
        self.store.create_conversation(conv)
        self.obs.emit("ui.chat.opened", metadata={"conversation_id": conv.conversation_id})
        return conv

    def list_conversations(self, *, tenant_id: str, user_id: str) -> list[ChatConversation]:
        return self.store.list_conversations(tenant_id=tenant_id, user_id=user_id)

    def get_conversation(self, *, tenant_id: str, user_id: str, conversation_id: str) -> ChatConversation:
        conv = self.store.get_conversation(conversation_id, tenant_id=tenant_id)
        return assert_conversation_access(conv, tenant_id=tenant_id, user_id=user_id)

    def list_messages(self, *, tenant_id: str, user_id: str, conversation_id: str) -> list[ChatMessage]:
        self.get_conversation(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        return self.store.list_messages(conversation_id, tenant_id=tenant_id)

    async def submit_turn(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        text: str,
        attachment_ids: tuple[str, ...] = (),
        idempotency_key: str,
        request_id: str,
        actor_ref: str,
        mode: str = "both",
        role: str = "Judge",
    ) -> ChatRun:
        conv = self.get_conversation(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
        existing = self.store.get_run_by_idempotency(
            tenant_id=tenant_id, conversation_id=conversation_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing

        content = str(text or "").strip()
        if not content and not attachment_ids:
            raise UIChatError(CHAT_EMPTY_TURN, message="Message text or attachment required.")

        refs: list[AttachmentRef] = []
        total_bytes = 0
        if len(attachment_ids) > self.attachments.limits.max_attachments_per_turn:
            raise UIChatError(ATTACHMENT_COUNT_EXCEEDED)
        for aid in attachment_ids:
            ref = assert_attachment_access(
                self.store.get_attachment(aid, tenant_id=tenant_id),
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if ref.conversation_id and ref.conversation_id != conversation_id:
                raise UIChatError(CHAT_EMPTY_TURN, message="Attachment belongs to another conversation.")
            total_bytes += ref.size_bytes
            refs.append(ref)
        if total_bytes > self.attachments.limits.max_aggregate_bytes:
            raise UIChatError(ATTACHMENT_AGGREGATE_TOO_LARGE)

        now = _utc()
        run_id = str(uuid.uuid4())
        user_msg = ChatMessage(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            role=ROLE_USER,
            content=content,
            content_version=1,
            attachment_ids=tuple(r.attachment_id for r in refs),
            created_at=now,
        )
        run = ChatRun(
            run_id=run_id,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            status=RUN_QUEUED,
            user_message_id=user_msg.message_id,
            created_at=now,
            updated_at=now,
        )
        self.store.add_message(user_msg)
        self.store.save_run(run)
        self.obs.emit("ui.chat.turn_submitted", metadata={"run_id": run_id, "conversation_id": conversation_id})

        prompt = self._build_prompt(content, refs)
        run.status = RUN_RUNNING
        run.updated_at = _utc()
        self.store.save_run(run)

        try:
            if self.workflow_engine is None or self.run_router is None:
                assistant_text = "Assistant response unavailable (orchestration not configured)."
                result = {"summary": assistant_text, "best_solution": assistant_text}
            else:
                task_id = str(uuid.uuid4())
                result = await self.workflow_engine.execute(
                    prompt,
                    mode,
                    role,
                    context_manager=self.context_manager,
                    run_router=self.run_router,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    user_id=user_id,
                    actor_ref=actor_ref,
                )
                run.task_id = task_id
                run.workflow_id = getattr(self.workflow_engine, "last_workflow_id", None)

            assistant_text = (
                result.get("best_solution")
                or result.get("summary")
                or result.get("analysis")
                or "Done."
            )
            assistant_msg = ChatMessage(
                message_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                role=ROLE_ASSISTANT,
                content=str(assistant_text),
                content_version=1,
                created_at=_utc(),
            )
            self.store.add_message(assistant_msg)
            run.assistant_message_id = assistant_msg.message_id
            run.status = RUN_SUCCEEDED
            run.updated_at = run.finished_at = _utc()
            self.store.save_run(run)
            conv.updated_at = run.finished_at
            if conv.title == "New conversation" and content:
                conv.title = content[:60] + ("…" if len(content) > 60 else "")
            self.store.update_conversation(conv)
            self.obs.emit("ui.chat.run_completed", metadata={"run_id": run_id})
            return run
        except Exception as exc:
            run.status = RUN_FAILED
            run.error_code = "run_failed"
            run.error_message = str(exc)[:500]
            run.updated_at = run.finished_at = _utc()
            self.store.save_run(run)
            self.obs.emit("ui.chat.run_failed", metadata={"run_id": run_id, "code": run.error_code})
            return run

    def _build_prompt(self, content: str, refs: list[AttachmentRef]) -> str:
        parts = [content] if content else []
        for ref in refs:
            parts.append(
                f"[Attachment {ref.attachment_class}: {ref.filename_safe} artifact={ref.artifact_ref} status={ref.status}]"
            )
        return "\n\n".join(parts) or "Process attached files."

    def get_run(self, *, tenant_id: str, user_id: str, run_id: str) -> ChatRun:
        run = self.store.get_run(run_id, tenant_id=tenant_id)
        run = assert_run_access(run, tenant_id=tenant_id, user_id=user_id)
        return self._sync_run_from_workflow(run)

    def _sync_run_from_workflow(self, run: ChatRun) -> ChatRun:
        if self.workflow_runtime is None or not run.workflow_id:
            return run
        if run.status in {RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED}:
            return run
        try:
            state = self.workflow_runtime.get_status(run.workflow_id, tenant_id=run.tenant_id)
        except Exception:
            return run
        if not state:
            return run
        if state.get("waiting") or state.get("status") == "waiting_approval":
            run.status = RUN_WAITING_APPROVAL
            run.updated_at = _utc()
            self.store.save_run(run)
        return run

    async def cancel_run(self, *, tenant_id: str, user_id: str, run_id: str) -> ChatRun:
        run = self.get_run(tenant_id=tenant_id, user_id=user_id, run_id=run_id)
        if run.status in {RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED}:
            return run
        if run.status not in {RUN_QUEUED, RUN_RUNNING, RUN_BACKGROUND}:
            raise UIChatError(CHAT_RUN_NOT_CANCELLABLE)
        if self.workflow_runtime is not None and run.workflow_id:
            try:
                await self.workflow_runtime.cancel(run.workflow_id, tenant_id=tenant_id)
            except Exception:
                pass
        run.status = RUN_CANCELLED
        run.updated_at = run.finished_at = _utc()
        self.store.save_run(run)
        return run

    def upload_attachment(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
        filename: str,
        mime_type: str,
        data: bytes,
        idempotency_hash: str | None = None,
    ) -> AttachmentRef:
        safe_name = sanitize_filename_display(filename)
        chash = idempotency_hash or _hash_bytes(data)
        now = _utc()
        ref = AttachmentRef(
            attachment_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            filename_safe=safe_name,
            attachment_class=classify_attachment(safe_name, mime_type),
            mime_type=mime_type or "application/octet-stream",
            size_bytes=len(data),
            status=ATTACH_UPLOADING,
            content_hash=chash,
            created_at=now,
            updated_at=now,
        )
        if ref.attachment_class == ATTACH_CLASS_UNKNOWN:
            raise UIChatError("attachment_unsupported")
        self.obs.emit("ui.upload.started", metadata={"attachment_id": ref.attachment_id})
        scope = self.memory_scope_factory(tenant_id) if self.memory_scope_factory else None
        try:
            ref, bg = self.attachments.ingest(ref, data=data, tenant_id=tenant_id, memory_scope=scope)
            ref.status = ref.status or ATTACH_UPLOADED
            self.store.save_attachment(ref)
            if bg:
                self._register_background_task(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    bg=bg,
                    attachment_id=ref.attachment_id,
                )
            self.obs.emit("ui.upload.completed", metadata={"attachment_id": ref.attachment_id})
            return ref
        except UIChatError:
            self.obs.emit("ui.upload.failed", metadata={"attachment_id": ref.attachment_id})
            raise
        except Exception as exc:
            ref.status = "FAILED"
            ref.error_code = "upload_failed"
            self.store.save_attachment(ref)
            self.obs.emit("ui.upload.failed", metadata={"attachment_id": ref.attachment_id})
            raise UIChatError("upload_failed", message=str(exc)[:200]) from exc

    def _register_background_task(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
        bg: dict[str, Any],
        attachment_id: str,
    ) -> BackgroundTaskView:
        now = _utc()
        task_id = bg.get("task_id") or str(uuid.uuid4())
        task = BackgroundTaskView(
            task_id=str(task_id),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            operation_label=str(bg.get("operation") or "background_processing"),
            status=TASK_QUEUED,
            phase="queued",
            cancel_available=True,
            created_at=now,
            started_at=now,
            workflow_id=bg.get("workflow_id"),
            result_artifact_ids=(attachment_id,),
        )
        self.store.save_task(task)
        return task

    def get_attachment(self, *, tenant_id: str, user_id: str, attachment_id: str) -> AttachmentRef:
        ref = self.store.get_attachment(attachment_id, tenant_id=tenant_id)
        return assert_attachment_access(ref, tenant_id=tenant_id, user_id=user_id)

    def transcribe_voice(
        self, *, tenant_id: str, user_id: str, audio: bytes, mime_type: str
    ) -> VoiceTranscript:
        self.obs.emit("ui.voice.recording_started", metadata={"size": len(audio)})
        if not audio:
            raise UIChatError(VOICE_AUDIO_EMPTY)
        if len(audio) > self.max_voice_bytes:
            raise UIChatError(VOICE_AUDIO_TOO_LARGE)
        mime = (mime_type or "").lower()
        if mime and mime not in self.supported_voice_mimes:
            raise UIChatError(VOICE_AUDIO_UNSUPPORTED)
        try:
            text = self.stt.transcribe(audio=audio, mime_type=mime_type)
        except Exception as exc:
            self.obs.emit("ui.voice.transcription_failed", metadata={"reason": type(exc).__name__})
            raise UIChatError(VOICE_TRANSCRIPTION_FAILED, retryable=True) from exc
        transcript = VoiceTranscript(
            transcript_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            text=text,
            audio_attachment_id=None,
            created_at=_utc(),
        )
        self.store.save_transcript(transcript)
        self.obs.emit("ui.voice.transcription_completed", metadata={"transcript_id": transcript.transcript_id})
        return transcript

    def synthesize_voice(
        self, *, tenant_id: str, user_id: str, message_id: str, voice: str = "default"
    ) -> VoiceAudioArtifact:
        msg = assert_message_access(self.store.get_message(message_id, tenant_id=tenant_id), tenant_id=tenant_id)
        if msg.role != ROLE_ASSISTANT:
            raise UIChatError(VOICE_MESSAGE_NOT_FOUND)
        existing = self.store.find_voice_artifact(
            tenant_id=tenant_id, message_id=message_id, message_version=msg.content_version
        )
        if existing is not None:
            self.obs.emit("ui.tts.completed", metadata={"artifact_id": existing.artifact_id, "cached": True})
            return existing
        self.obs.emit("ui.tts.requested", metadata={"message_id": message_id})
        if len(msg.content) > getattr(self.tts, "max_chars", 8000):
            raise UIChatError(VOICE_TTS_TOO_LONG)
        try:
            blob = self.tts.synthesize(text=msg.content, voice=voice)
        except Exception as exc:
            self.obs.emit("ui.tts.failed", metadata={"message_id": message_id})
            raise UIChatError(VOICE_TTS_FAILED, retryable=True) from exc
        artifact = VoiceAudioArtifact(
            artifact_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            message_id=message_id,
            message_version=msg.content_version,
            mime_type="audio/wav",
            byte_size=len(blob),
            content_hash=_hash_bytes(blob),
            created_at=_utc(),
        )
        self.store.save_voice_artifact(artifact, blob=blob)
        self.obs.emit("ui.tts.completed", metadata={"artifact_id": artifact.artifact_id})
        return artifact

    def get_voice_audio(self, *, tenant_id: str, artifact_id: str) -> tuple[VoiceAudioArtifact, bytes]:
        artifact = self.store.get_voice_artifact(artifact_id, tenant_id=tenant_id)
        if artifact is None:
            raise UIChatError("voice_audio_not_found")
        blob = self.store.get_voice_artifact_blob(artifact_id, tenant_id=tenant_id)
        if blob is None:
            raise UIChatError("voice_audio_not_found")
        return artifact, blob

    def list_tasks(self, *, tenant_id: str, user_id: str) -> list[BackgroundTaskView]:
        return self.store.list_tasks(tenant_id=tenant_id, user_id=user_id)

    def get_task(self, *, tenant_id: str, user_id: str, task_id: str) -> BackgroundTaskView:
        task = self.store.get_task(task_id, tenant_id=tenant_id)
        task = assert_task_access(task, tenant_id=tenant_id, user_id=user_id)
        return self._sync_task_from_workflow(task)

    def _sync_task_from_workflow(self, task: BackgroundTaskView) -> BackgroundTaskView:
        if self.workflow_runtime is None or not task.workflow_id:
            return task
        if task.status in {TASK_SUCCEEDED, TASK_FAILED, TASK_CANCELLED}:
            return task
        try:
            state = self.workflow_runtime.get_status(task.workflow_id, tenant_id=task.tenant_id)
        except Exception:
            return task
        if not state:
            return task
        status = str(state.get("status") or "").lower()
        if status in {"completed", "succeeded", "success"}:
            task.status = TASK_SUCCEEDED
            task.finished_at = _utc()
        elif status in {"failed", "error"}:
            task.status = TASK_FAILED
            task.error_code = state.get("error_code") or "task_failed"
            task.error_message = str(state.get("error_message") or "Task failed.")[:200]
            task.finished_at = _utc()
        elif status in {"cancelled", "canceled"}:
            task.status = TASK_CANCELLED
            task.finished_at = _utc()
        else:
            task.status = TASK_RUNNING
            prog = state.get("progress") or {}
            if isinstance(prog, dict):
                cur = prog.get("current")
                tot = prog.get("total")
                if cur is not None and tot is not None:
                    task.progress_current = int(cur)
                    task.progress_total = int(tot)
                elif prog.get("phase"):
                    task.phase = str(prog.get("phase"))
        self.store.save_task(task)
        return task

    async def cancel_task(self, *, tenant_id: str, user_id: str, task_id: str) -> BackgroundTaskView:
        task = self.get_task(tenant_id=tenant_id, user_id=user_id, task_id=task_id)
        if task.status in {TASK_SUCCEEDED, TASK_FAILED, TASK_CANCELLED}:
            return task
        if not task.cancel_available:
            raise UIChatError(TASK_NOT_CANCELLABLE)
        self.obs.emit("ui.background.cancel_requested", metadata={"task_id": task_id})
        if self.workflow_runtime is not None and task.workflow_id:
            try:
                await self.workflow_runtime.cancel(task.workflow_id, tenant_id=tenant_id)
            except Exception:
                pass
        task.status = TASK_CANCELLED
        task.finished_at = _utc()
        self.store.save_task(task)
        return task
