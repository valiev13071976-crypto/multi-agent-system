from workflow.models import Checkpoint

APPROVAL_CHECKPOINT_KEYS = (
    "action_id",
    "decision_id",
    "required_approval",
    "approval_id",
    "action_fingerprint",
    "approval_class",
)


def approval_checkpoint_fields(extra) -> dict:
    allowed = {}
    source = dict(extra or {})
    for key in APPROVAL_CHECKPOINT_KEYS:
        if key in source:
            allowed[key] = source[key]
    return allowed


def public_checkpoint_view(checkpoint: Checkpoint) -> dict:
    return {
        "workflow_id": checkpoint.workflow_id,
        "workflow_version": checkpoint.workflow_version,
        "status": checkpoint.status,
        "current_step": checkpoint.current_step,
        "completed_steps": list(checkpoint.completed_steps),
        "timestamp": checkpoint.timestamp.isoformat(),
        "payload_keys": sorted(checkpoint.payload.keys()),
    }
