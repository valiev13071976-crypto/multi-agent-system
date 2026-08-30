"""Tenant-scoped access checks for UI Chat."""

from __future__ import annotations

from ui_chat.errors import CHAT_ACCESS_DENIED, UIChatError
from ui_chat.models import AttachmentRef, BackgroundTaskView, ChatConversation, ChatMessage, ChatRun


def assert_conversation_access(conv: ChatConversation | None, *, tenant_id: str, user_id: str) -> ChatConversation:
    if conv is None or conv.tenant_id != tenant_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Conversation not found.")
    if conv.user_id != user_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Conversation not found.")
    return conv


def assert_message_access(msg: ChatMessage | None, *, tenant_id: str) -> ChatMessage:
    if msg is None or msg.tenant_id != tenant_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Message not found.")
    return msg


def assert_attachment_access(ref: AttachmentRef | None, *, tenant_id: str, user_id: str) -> AttachmentRef:
    if ref is None or ref.tenant_id != tenant_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Attachment not found.")
    if ref.user_id != user_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Attachment not found.")
    return ref


def assert_run_access(run: ChatRun | None, *, tenant_id: str, user_id: str) -> ChatRun:
    if run is None or run.tenant_id != tenant_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Run not found.")
    if run.user_id != user_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Run not found.")
    return run


def assert_task_access(task: BackgroundTaskView | None, *, tenant_id: str, user_id: str) -> BackgroundTaskView:
    if task is None or task.tenant_id != tenant_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Task not found.")
    if task.user_id != user_id:
        raise UIChatError(CHAT_ACCESS_DENIED, message="Task not found.")
    return task
