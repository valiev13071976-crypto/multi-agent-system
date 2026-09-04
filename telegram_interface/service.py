"""Telegram interface orchestration — Business Assistant API transport only."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from business_assistant_api.errors import BusinessAssistantApiError
from business_assistant_api.models import ST_CANCELLED, ST_COMPLETED, ST_REJECTED, ST_WAITING_FOR_APPROVAL, TERMINAL_STATES
from business_assistant_api.service import BusinessAssistantApiService
from business_assistant_api.uploads import safe_filename, save_upload
from security.errors import RateLimitedError
from security.rate_limit import RateLimiter
from security.redaction import redact
from security.tenant import require_tenant_id
from telegram_interface.errors import (
    TGI_ACCESS_DENIED,
    TGI_APPROVAL_REQUIRED,
    TGI_BINDING_REQUIRED,
    TGI_BINDING_REVOKED,
    TGI_CALLBACK_STALE,
    TGI_CAPABILITY_DENIED,
    TGI_DUPLICATE_UPDATE,
    TGI_FILE_TOO_LARGE,
    TGI_FILE_UNSUPPORTED,
    TGI_INVALID_CALLBACK,
    TGI_INVALID_UPDATE,
    TGI_LIVE_FORBIDDEN,
    TGI_PANDA_ERROR,
    TGI_PAYLOAD_TOO_LARGE,
    TGI_RATE_LIMITED,
    TGI_RESPONSE_EMPTY,
    TGI_TENANT_MISMATCH,
    TGI_UNSUPPORTED_MESSAGE,
    TGI_USER_DISABLED,
    TelegramInterfaceError,
)
from telegram_interface.models import CallbackToken, ChatSession, NormalizedTelegramUpdate
from telegram_interface.normalize import (
    MAX_TELEGRAM_PAYLOAD_BYTES,
    attachment_allowed,
    normalize_telegram_payload,
    telegram_payload_size_bytes,
)
from telegram_interface.render import (
    render_artifacts,
    render_error,
    render_preview,
    render_progress,
    render_result,
    render_status_label,
    chunk_telegram_text,
)
from telegram_interface.store import SqliteTelegramInterfaceStore
from telegram_interface.transport import InlineButton, OutboundMessage, ProviderTelegramTransport, new_idempotency


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TERMINAL = frozenset(TERMINAL_STATES)

_BA_CODE_MAP = {
    "baa_unsupported_capability": TGI_CAPABILITY_DENIED,
    "BAA_UNSUPPORTED_CAPABILITY": TGI_CAPABILITY_DENIED,
    "baa_access_denied": TGI_ACCESS_DENIED,
    "BAA_ACCESS_DENIED": TGI_ACCESS_DENIED,
    "baa_approval_required": TGI_APPROVAL_REQUIRED,
    "BAA_APPROVAL_REQUIRED": TGI_APPROVAL_REQUIRED,
    "baa_rate_limited": TGI_RATE_LIMITED,
    "BAA_RATE_LIMITED": TGI_RATE_LIMITED,
}


def _map_ba_error(code: str) -> str:
    return _BA_CODE_MAP.get(code, TGI_PANDA_ERROR)


class TelegramInterfaceService:
    def __init__(
        self,
        *,
        store: SqliteTelegramInterfaceStore,
        ba_api: BusinessAssistantApiService,
        transport: ProviderTelegramTransport,
        upload_dir: str = "",
        default_tenant_id: str = "",
        live_active: bool = False,
        rate_limiter: RateLimiter | None = None,
        max_payload_bytes: int = MAX_TELEGRAM_PAYLOAD_BYTES,
    ):
        self.store = store
        self.ba = ba_api
        self.transport = transport
        self.upload_dir = upload_dir or getattr(ba_api, "upload_dir", "")
        self.default_tenant_id = default_tenant_id
        self.live_active = bool(live_active)
        self.rate_limiter = rate_limiter or RateLimiter()
        self.max_payload_bytes = int(max_payload_bytes)

    def close(self) -> None:
        self.store.close()

    def register_binding(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        telegram_user_id: str,
        chat_id: str,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        conv = self.ba.create_conversation(tenant_id=tenant, owner_id=owner_id, title="Telegram")
        binding = self.store.create_binding(
            tenant_id=tenant,
            owner_id=owner_id,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            conversation_id=conv.conversation_id,
        )
        session = ChatSession(
            chat_id=chat_id,
            tenant_id=tenant,
            owner_id=owner_id,
            conversation_id=conv.conversation_id,
        )
        self.store.save_session(session)
        return {
            "binding_id": binding.binding_id,
            "conversation_id": conv.conversation_id,
            "tenant_id": tenant,
            "owner_id": owner_id,
        }

    def set_binding_status(
        self, *, tenant_id: str, telegram_user_id: str, chat_id: str, status: str
    ) -> None:
        tenant = require_tenant_id(tenant_id)
        binding = self.store.get_binding(
            tenant_id=tenant, telegram_user_id=telegram_user_id, chat_id=chat_id
        )
        if binding is None:
            raise TelegramInterfaceError(TGI_BINDING_REQUIRED, http_status=404)
        binding.status = status
        binding.updated_at = _utc_iso()
        self.store.save_binding(binding)

    def _resolve_binding(self, *, tenant: str, update: NormalizedTelegramUpdate):
        binding = self.store.get_binding(
            tenant_id=tenant, telegram_user_id=update.telegram_user_id, chat_id=update.chat_id
        )
        if binding is not None:
            if binding.status == "disabled":
                raise TelegramInterfaceError(TGI_USER_DISABLED, http_status=403)
            if binding.status != "active":
                raise TelegramInterfaceError(TGI_BINDING_REVOKED, http_status=403)
            return binding
        others = [
            b
            for b in self.store.find_bindings_for_telegram_user(telegram_user_id=update.telegram_user_id)
            if b.status == "active" and b.tenant_id != tenant
        ]
        if others:
            raise TelegramInterfaceError(TGI_TENANT_MISMATCH, http_status=403)
        raise TelegramInterfaceError(TGI_BINDING_REQUIRED, http_status=403)

    def handle_payload(self, *, tenant_id: str, payload: dict[str, Any]) -> dict:
        if self.live_active:
            raise TelegramInterfaceError(TGI_LIVE_FORBIDDEN, http_status=403)
        tenant = require_tenant_id(tenant_id or self.default_tenant_id)
        if not isinstance(payload, dict):
            raise TelegramInterfaceError(TGI_INVALID_UPDATE, http_status=400)
        if telegram_payload_size_bytes(payload) > self.max_payload_bytes:
            raise TelegramInterfaceError(TGI_PAYLOAD_TOO_LARGE, http_status=413)
        update = normalize_telegram_payload(payload)
        if update.kind == "invalid" or not update.update_id or update.update_id in {"None", "null"}:
            raise TelegramInterfaceError(TGI_INVALID_UPDATE, http_status=400)
        if not update.chat_id or not update.telegram_user_id:
            raise TelegramInterfaceError(TGI_INVALID_UPDATE, http_status=400)
        if update.kind == "unsupported":
            raise TelegramInterfaceError(TGI_UNSUPPORTED_MESSAGE, http_status=400)
        if self.store.has_processed_update(update.update_id):
            raise TelegramInterfaceError(TGI_DUPLICATE_UPDATE, http_status=200)
        binding = self._resolve_binding(tenant=tenant, update=update)
        try:
            self.rate_limiter.check_authenticated(tenant_id=tenant, user_id=binding.owner_id)
        except RateLimitedError as exc:
            raise TelegramInterfaceError(TGI_RATE_LIMITED, http_status=429) from exc
        self.store.mark_processed_update(update_id=update.update_id, tenant_id=tenant)
        try:
            if update.kind == "callback_query":
                out = self._handle_callback(update, binding)
            elif update.kind == "command":
                out = self._handle_command(update, binding)
            else:
                out = self._handle_message(update, binding)
        except BusinessAssistantApiError as exc:
            mapped = _map_ba_error(exc.code)
            self._reply(binding.chat_id, render_error(mapped), prefix="err")
            raise TelegramInterfaceError(mapped, redact(exc.message), http_status=422) from exc
        except TelegramInterfaceError:
            raise
        except Exception as exc:
            self._reply(binding.chat_id, render_error(TGI_PANDA_ERROR), prefix="err")
            raise TelegramInterfaceError(TGI_PANDA_ERROR, render_error(TGI_PANDA_ERROR), http_status=500) from exc
        return out

    def _session(self, binding) -> ChatSession:
        session = self.store.get_session(binding.chat_id)
        if session is None:
            session = ChatSession(
                chat_id=binding.chat_id,
                tenant_id=binding.tenant_id,
                owner_id=binding.owner_id,
                conversation_id=binding.conversation_id,
            )
            if not session.conversation_id:
                conv = self.ba.create_conversation(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, title="Telegram"
                )
                session.conversation_id = conv.conversation_id
                binding.conversation_id = conv.conversation_id
                self.store.save_binding(binding)
            self.store.save_session(session)
        return session

    def _handle_command(self, update: NormalizedTelegramUpdate, binding) -> dict:
        cmd = update.command
        session = self._session(binding)
        if cmd == "start":
            self._reply(binding.chat_id, "Panda Business Assistant is ready. Send a message to begin.", prefix="start")
            return {"status": "ok", "command": "start"}
        if cmd == "help":
            self._reply(
                binding.chat_id,
                "Send a business request in plain language.\nCommands: /new /status /cancel /help",
                prefix="help",
            )
            return {"status": "ok", "command": "help"}
        if cmd == "new":
            conv = self.ba.create_conversation(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, title="Telegram"
            )
            session.conversation_id = conv.conversation_id
            session.active_request_id = ""
            session.progress_message_id = ""
            session.last_event_cursor = ""
            binding.conversation_id = conv.conversation_id
            self.store.save_binding(binding)
            self.store.save_session(session)
            self._reply(binding.chat_id, "New conversation started.", prefix="new")
            return {"status": "ok", "command": "new", "conversation_id": conv.conversation_id}
        if cmd == "status":
            if not session.active_request_id:
                self._reply(binding.chat_id, "No active request.", prefix="status")
                return {"status": "ok", "command": "status"}
            rec = self.ba.get_request(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=session.active_request_id
            )
            self._reply(binding.chat_id, render_status_label(rec.status), prefix="status")
            return {"status": "ok", "request_id": rec.request_id, "state": rec.status}
        if cmd == "cancel":
            if not session.active_request_id:
                self._reply(binding.chat_id, "Nothing to cancel.", prefix="cancel")
                return {"status": "ok"}
            rec = self.ba.cancel(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=session.active_request_id
            )
            self._sync_request(session, binding, rec.request_id)
            return {"status": "ok", "command": "cancel", "request_id": rec.request_id}
        return self._handle_message(
            NormalizedTelegramUpdate(
                update_id=update.update_id,
                kind="message",
                chat_id=update.chat_id,
                telegram_user_id=update.telegram_user_id,
                text=update.text,
                attachment=update.attachment,
            ),
            binding,
        )

    def _handle_message(self, update: NormalizedTelegramUpdate, binding) -> dict:
        session = self._session(binding)
        artifact_refs: list[str] = []
        message = update.text.strip()
        if update.attachment:
            artifact_refs.append(self._ingest_attachment(update, binding))
        if not message:
            if update.attachment and any(
                update.attachment.filename.lower().endswith(ext)
                for ext in (".xlsx", ".xls", ".csv")
            ):
                message = "Сравни закупку с текущими ценами и подготовь итоговую Excel таблицу."
            else:
                message = "Analyze attached file"
        if not message:
            self._reply(binding.chat_id, "Please send a message or supported file.", prefix="empty")
            return {"status": "ignored"}
        idem = f"tg-update-{update.update_id}"
        rec = self.ba.submit(
            tenant_id=binding.tenant_id,
            owner_id=binding.owner_id,
            message=message,
            conversation_id=session.conversation_id,
            artifact_refs=artifact_refs,
            idempotency_key=idem,
            trace_id=f"tg-{update.update_id}",
        )
        session.active_request_id = rec.request_id
        self.store.save_session(session)
        self._reply(binding.chat_id, "Request accepted.", prefix="accepted")
        self._sync_request(session, binding, rec.request_id)
        return {"status": "ok", "request_id": rec.request_id}

    def _ingest_attachment(self, update: NormalizedTelegramUpdate, binding) -> str:
        att = update.attachment
        if att is None:
            raise TelegramInterfaceError(TGI_FILE_UNSUPPORTED)
        if not attachment_allowed(att):
            raise TelegramInterfaceError(TGI_FILE_TOO_LARGE if att.size_bytes > 10 * 1024 * 1024 else TGI_FILE_UNSUPPORTED)
        content, filename = self.transport.download_file(att.file_id)
        safe = safe_filename(att.filename or filename)
        out = save_upload(
            base_dir=self.upload_dir,
            tenant_id=binding.tenant_id,
            owner_id=binding.owner_id,
            filename=safe,
            content=content,
            mime_type=att.mime_type,
        )
        return out["artifact_ref"]

    def _handle_callback(self, update: NormalizedTelegramUpdate, binding) -> dict:
        data = update.callback_data or ""
        if not data.startswith("panda:"):
            raise TelegramInterfaceError(TGI_INVALID_CALLBACK)
        token = data.split(":", 1)[1]
        cb = self.store.get_callback(token)
        if cb is None:
            raise TelegramInterfaceError(TGI_INVALID_CALLBACK)
        if cb.tenant_id != binding.tenant_id or cb.owner_id != binding.owner_id:
            raise TelegramInterfaceError(TGI_ACCESS_DENIED, http_status=403)
        if cb.consumed:
            self.transport.answer_callback(update.callback_query_id, "Already handled")
            return {"status": "duplicate", "action": cb.action}
        session = self._session(binding)
        if cb.request_id != session.active_request_id and session.active_request_id:
            rec = self.ba.get_request(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=cb.request_id
            )
        else:
            rec = self.ba.get_request(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=cb.request_id
            )
        if rec.status != ST_WAITING_FOR_APPROVAL and cb.action == "approve":
            self.transport.answer_callback(update.callback_query_id, "Not waiting for approval")
            return {"status": "stale", "request_id": cb.request_id}
        consumed = self.store.consume_callback(token)
        if not consumed:
            self.transport.answer_callback(update.callback_query_id, "Already handled")
            return {"status": "duplicate", "action": cb.action}
        try:
            if cb.action == "approve":
                rec = self.ba.approve(
                    tenant_id=binding.tenant_id,
                    owner_id=binding.owner_id,
                    request_id=cb.request_id,
                    approval_id=cb.approval_id or rec.approval_id,
                    plan_fingerprint=rec.plan_fingerprint,
                )
            elif cb.action == "reject":
                rec = self.ba.reject(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=cb.request_id
                )
            elif cb.action == "cancel":
                rec = self.ba.cancel(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=cb.request_id
                )
            else:
                raise TelegramInterfaceError(TGI_INVALID_CALLBACK)
        except BusinessAssistantApiError as exc:
            if exc.code in {"baa_approval_stale", "baa_invalid_state"}:
                raise TelegramInterfaceError(TGI_CALLBACK_STALE) from exc
            raise
        self.transport.answer_callback(update.callback_query_id, "OK")
        session.active_request_id = cb.request_id
        self.store.save_session(session)
        self._sync_request(session, binding, cb.request_id)
        return {"status": "ok", "action": cb.action, "request_id": cb.request_id, "state": rec.status}

    def _sync_request(self, session: ChatSession, binding, request_id: str) -> None:
        rec = self.ba.get_request(
            tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=request_id
        )
        events = self.ba.list_events(
            tenant_id=binding.tenant_id,
            owner_id=binding.owner_id,
            request_id=request_id,
            after=session.last_event_cursor or None,
        )
        if events:
            session.last_event_cursor = events[-1].timestamp
            progress = render_progress(
                [{"message": e.message, "event_type": e.event_type} for e in events]
            )
            self._reply(binding.chat_id, progress, prefix=f"prog-{request_id}")

        if rec.status == ST_WAITING_FOR_APPROVAL:
            preview = self.ba.get_preview(
                tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=request_id
            )
            text = render_preview(preview)
            self._send_approval(binding, session, request_id, rec.approval_id, text)
            self.store.save_session(session)
            return

        if rec.status in _TERMINAL:
            session.active_request_id = ""
            if rec.status == ST_COMPLETED:
                result = self.ba.get_result(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=request_id
                )
                arts = self.ba.list_artifacts(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=request_id
                )
                body = render_result(result)
                art_text = render_artifacts(arts)
                if art_text:
                    body = f"{body}\n\n{art_text}"
                self._reply(binding.chat_id, body, prefix=f"result-{request_id}")
            elif rec.status == ST_REJECTED:
                self._reply(binding.chat_id, "Action rejected. No external write was performed.", prefix="reject")
            elif rec.status == ST_CANCELLED:
                self._reply(binding.chat_id, "Request cancelled.", prefix="cancel")
            else:
                st = self.ba.get_status(
                    tenant_id=binding.tenant_id, owner_id=binding.owner_id, request_id=request_id
                )
                msg = st.get("error_message") or render_status_label(rec.status)
                self._reply(binding.chat_id, msg, prefix="fail")
        else:
            session.active_request_id = request_id
        self.store.save_session(session)

    def _send_approval(self, binding, session: ChatSession, request_id: str, approval_id: str, text: str) -> None:
        buttons: list[list[InlineButton]] = []
        row: list[InlineButton] = []
        for action in ("approve", "reject", "cancel"):
            token = uuid.uuid4().hex[:16]
            self.store.save_callback(
                CallbackToken(
                    token=token,
                    tenant_id=binding.tenant_id,
                    owner_id=binding.owner_id,
                    request_id=request_id,
                    action=action,
                    approval_id=approval_id,
                    created_at=_utc_iso(),
                )
            )
            label = {"approve": "Approve", "reject": "Reject", "cancel": "Cancel"}[action]
            row.append(InlineButton(text=label, callback_data=f"panda:{token}"))
        buttons.append(row)
        msg = OutboundMessage(
            chat_id=binding.chat_id,
            text=text,
            idempotency_key=new_idempotency(f"approval-{request_id}"),
            buttons=buttons,
        )
        self.transport.send(msg)

    def _reply(self, chat_id: str, text: str, *, prefix: str) -> None:
        body = str(text or "").strip()
        if not body:
            body = render_error(TGI_RESPONSE_EMPTY)
        chunks = chunk_telegram_text(body)
        for i, part in enumerate(chunks):
            self.transport.send(
                OutboundMessage(
                    chat_id=chat_id,
                    text=part,
                    idempotency_key=new_idempotency(f"{prefix}-{i}"),
                )
            )

    def recover_session(self, chat_id: str) -> ChatSession | None:
        return self.store.get_session(chat_id)

    def verify_access(
        self, *, tenant_id: str, owner_id: str, request_id: str, telegram_user_id: str, chat_id: str
    ) -> None:
        binding = self.store.get_binding(
            tenant_id=tenant_id, telegram_user_id=telegram_user_id, chat_id=chat_id
        )
        if binding is None or binding.owner_id != owner_id or binding.status != "active":
            raise TelegramInterfaceError(TGI_ACCESS_DENIED, http_status=403)
        self.ba.get_request(tenant_id=tenant_id, owner_id=owner_id, request_id=request_id)
