from autonomy.models import (
    ACTION_DELETE,
    ACTION_EXECUTE_CODE,
    ACTION_EXTERNAL_PUBLISH,
    ACTION_FINANCIAL_CHANGE,
    ACTION_PERMISSION_CHANGE,
    ACTION_PURCHASE,
    ACTION_READ,
    ACTION_SEND_MESSAGE,
    ACTION_TYPES,
    ACTION_WRITE,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
)


DESTRUCTIVE_OPERATIONS = frozenset(
    {"delete", "drop", "destroy", "purge", "wipe", "unlink", "truncate"}
)


class ActionRiskClassifier:
    """Deterministic risk classification. No LLM."""

    def classify(
        self,
        action_type: str,
        operation: str = "",
        resource: str = "",
        metadata=None,
    ) -> str:
        meta = dict(metadata or {})
        reversible = bool(meta.get("reversible", False))
        destructive = bool(meta.get("destructive", False))
        unknown = bool(meta.get("unknown", False))
        op = str(operation or "").strip().lower()

        if action_type == ACTION_READ:
            return RISK_LOW
        if action_type in {
            ACTION_PURCHASE,
            ACTION_FINANCIAL_CHANGE,
            ACTION_PERMISSION_CHANGE,
        }:
            return RISK_CRITICAL
        if action_type in {ACTION_SEND_MESSAGE, ACTION_EXTERNAL_PUBLISH}:
            return RISK_HIGH
        if action_type == ACTION_DELETE:
            if reversible and not unknown and not destructive:
                return RISK_HIGH
            return RISK_CRITICAL
        if action_type == ACTION_EXECUTE_CODE:
            if meta.get("filesystem") or meta.get("network_access"):
                return RISK_CRITICAL
            return RISK_HIGH
        if action_type == ACTION_WRITE:
            if destructive or unknown:
                return RISK_CRITICAL if destructive else RISK_HIGH
            if reversible:
                return RISK_MEDIUM
            return RISK_HIGH
        if action_type not in ACTION_TYPES or unknown:
            if destructive or op in DESTRUCTIVE_OPERATIONS:
                return RISK_CRITICAL
            return RISK_HIGH
        if destructive or op in DESTRUCTIVE_OPERATIONS:
            return RISK_CRITICAL
        return RISK_HIGH
