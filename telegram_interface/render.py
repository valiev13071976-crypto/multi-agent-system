"""Safe Telegram message rendering."""

from __future__ import annotations

import re

_MAX = 3900
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def escape_telegram_text(text: str) -> str:
    s = _CTRL.sub("", str(text or ""))
    for ch in ("\\", "`", "*", "_", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        s = s.replace(ch, f"\\{ch}")
    return s


def truncate_safe(text: str, limit: int = _MAX) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n\n… (truncated)"


def chunk_telegram_text(text: str, limit: int = _MAX) -> list[str]:
    """Deterministic ordered chunks for Telegram length limits."""
    s = str(text or "")
    if not s:
        return []
    return [s[i : i + limit] for i in range(0, len(s), limit)]


def render_status_label(status: str) -> str:
    labels = {
        "RECEIVED": "Request received",
        "VALIDATING": "Validating",
        "PLANNING": "Planning",
        "QUEUED": "Queued",
        "RUNNING": "Running",
        "WAITING_FOR_APPROVAL": "Waiting for approval",
        "RESUMING": "Resuming",
        "COMPLETED": "Completed",
        "FAILED": "Failed",
        "REJECTED": "Rejected",
        "CANCELLED": "Cancelled",
        "BLOCKED": "Blocked",
    }
    return labels.get(status, status)


def render_progress(events: list[dict]) -> str:
    if not events:
        return "Processing…"
    lines = []
    for ev in events[-5:]:
        msg = ev.get("message") or ev.get("event_type") or ""
        if msg:
            lines.append(f"• {escape_telegram_text(msg)}")
    return truncate_safe("\n".join(lines) or "Processing…")


def render_preview(preview: dict | None) -> str:
    if not preview:
        return "Approval required for a governed external action."
    changes = preview.get("changes") or []
    warnings = preview.get("warnings") or []
    parts = ["*Approval required*", ""]
    if changes:
        parts.append("Proposed changes:")
        for c in changes[:10]:
            parts.append(f"• {escape_telegram_text(str(c)[:200])}")
    else:
        parts.append("Proposed external write pending approval.")
    for w in warnings[:5]:
        parts.append(f"⚠ {escape_telegram_text(str(w)[:200])}")
    return truncate_safe("\n".join(parts))


def render_result(result: dict | None) -> str:
    if not result:
        return "Done."
    summary = result.get("summary") or "Completed."
    parts = [escape_telegram_text(summary)]
    findings = (result.get("structured_result") or {}).get("findings") or []
    if findings:
        parts.append("")
        parts.append("Findings:")
        for f in findings[:15]:
            parts.append(f"• {escape_telegram_text(str(f.get('summary') or f)[:180])}")
    return truncate_safe("\n".join(parts))


def render_artifacts(artifacts: list[dict]) -> str:
    if not artifacts:
        return ""
    lines = ["Artifacts:"]
    for a in artifacts[:10]:
        name = a.get("filename") or a.get("artifact_type") or a.get("ref") or "artifact"
        lines.append(f"• {escape_telegram_text(str(name))}")
    return truncate_safe("\n".join(lines))


def render_error(code: str) -> str:
    mapping = {
        "tgi_unauthorized": "You are not authorized.",
        "tgi_binding_required": "Account binding required. Contact your administrator.",
        "tgi_binding_revoked": "Your Telegram access was revoked.",
        "tgi_file_unsupported": "Unsupported file type.",
        "tgi_file_too_large": "File is too large.",
        "tgi_invalid_callback": "This action is no longer valid.",
        "tgi_callback_stale": "Approval expired. Refresh and try again.",
        "tgi_access_denied": "Access denied.",
        "tgi_tenant_mismatch": "Access denied.",
        "tgi_user_disabled": "This account is disabled.",
        "tgi_unsupported_message": "This message type is not supported.",
        "tgi_invalid_update": "Could not read that update.",
        "tgi_payload_too_large": "Message is too large.",
        "tgi_rate_limited": "Too many requests. Try again later.",
        "tgi_live_forbidden": "Live Telegram is not enabled.",
        "tgi_response_empty": "No response.",
        "tgi_panda_error": "Something went wrong. Please try again.",
        "tgi_capability_denied": "This action is not permitted.",
        "tgi_approval_required": "Approval is required before this action can continue.",
        "baa_not_found": "Request not found.",
        "baa_invalid_state": "Action not available in current state.",
        "baa_approval_stale": "Approval is stale.",
    }
    return mapping.get(code, "Something went wrong. Please try again.")
