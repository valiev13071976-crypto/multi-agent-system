from workflow.models import Checkpoint


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
