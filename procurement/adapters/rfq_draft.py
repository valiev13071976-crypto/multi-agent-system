"""RFQ draft adapter — local draft only; zero external side effects."""

from __future__ import annotations

from memory.models import utc_now
from procurement.adapters.models import (
    OP_RFQ_DRAFT,
    PROCUREMENT_RFQ_DRAFT_VERSION,
    TOOL_RFQ_DRAFT,
    RfqDraft,
)
from procurement.errors import ProcurementError
from tools.errors import ToolError


PROCUREMENT_RFQ_DRAFT_INVALID = "procurement_rfq_draft_invalid"
PROCUREMENT_RFQ_SEND_UNSUPPORTED = "procurement_rfq_send_unsupported"

_FORBIDDEN_ARG_KEYS = frozenset(
    {
        "base_url",
        "headers",
        "auth",
        "authorization",
        "timeout",
        "method",
        "raw_url",
        "url",
        "shell",
        "command",
        "send",
        "deliver",
        "email_to",
    }
)


class RfqDraftAdapter:
    """Produce structured RFQ draft. Never sends externally."""

    tool_id = TOOL_RFQ_DRAFT
    operation = OP_RFQ_DRAFT

    def __init__(self, *, max_chars: int = 8000, enabled: bool = True):
        self.max_chars = int(max_chars)
        self.enabled = bool(enabled)
        self.calls: list[dict] = []
        self.write_calls = 0
        self.send_calls = 0

    async def execute_read(self, request, context) -> dict:
        _ = context
        if not self.enabled:
            raise ToolError(PROCUREMENT_RFQ_DRAFT_INVALID)
        args = dict(request.arguments or {})
        for key in args:
            if str(key).lower() in _FORBIDDEN_ARG_KEYS:
                raise ToolError(PROCUREMENT_RFQ_DRAFT_INVALID)
        if args.get("send") or args.get("deliver"):
            self.send_calls += 1
            raise ToolError(PROCUREMENT_RFQ_SEND_UNSUPPORTED)
        request_id = str(args.get("request_id") or "").strip()
        supplier_ref = str(args.get("supplier_ref") or "").strip()
        item_name = str(args.get("item_name") or "").strip()
        if not request_id or not supplier_ref or not item_name:
            raise ToolError(PROCUREMENT_RFQ_DRAFT_INVALID)
        quantity = args.get("quantity")
        unit = args.get("unit")
        specs = args.get("specs") if isinstance(args.get("specs"), dict) else {}
        deadline = args.get("deadline")
        questions = args.get("questions") if isinstance(args.get("questions"), (list, tuple)) else ()
        language = str(args.get("language") or "en").strip()[:8]
        citations = tuple(str(c) for c in (args.get("citations") or ()) if c)[:20]
        self.calls.append({"request_id_len": len(request_id), "supplier_ref_len": len(supplier_ref)})

        subject = f"RFQ: {item_name}"[:200]
        lines = [
            f"Request ID: {request_id}",
            f"Item: {item_name}",
            f"Quantity: {quantity} {unit or ''}".strip(),
            f"Language: {language}",
        ]
        if deadline:
            lines.append(f"Required by: {deadline}")
        if specs:
            lines.append("Specifications:")
            for k, v in list(specs.items())[:30]:
                lines.append(f"- {k}: {v}")
        if questions:
            lines.append("Questions:")
            for q in list(questions)[:20]:
                lines.append(f"- {q}")
        lines.append("")
        lines.append("This is a draft only. Human review and send required.")
        body = "\n".join(lines)[: self.max_chars]
        draft = RfqDraft(
            subject=subject,
            body=body,
            supplier_ref=supplier_ref,
            request_id=request_id,
            citations=citations,
            warnings=("requires_human_send",),
            requires_human_send=True,
            draft_version=PROCUREMENT_RFQ_DRAFT_VERSION,
            created_at=utc_now(),
            metadata_safe={"external_send": False, "side_effects": 0},
        )
        return draft.as_dict()

    async def execute_send(self, *_a, **_k):
        self.send_calls += 1
        raise ToolError(PROCUREMENT_RFQ_SEND_UNSUPPORTED)
