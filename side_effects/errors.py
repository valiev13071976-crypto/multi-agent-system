class SideEffectError(Exception):
    def __init__(self, error_code: str = "side_effect_error"):
        self.error_code = error_code
        super().__init__(error_code)


class SideEffectExecutionDeniedError(SideEffectError):
    def __init__(self, error_code: str = "side_effect_execution_denied"):
        super().__init__(error_code)


class SideEffectActivationDeniedError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "side_effect_activation_denied"):
        super().__init__(error_code)


class SideEffectAdapterNotFoundError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "adapter_not_found"):
        super().__init__(error_code)


class SideEffectAdapterMismatchError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "adapter_mismatch"):
        super().__init__(error_code)


class SideEffectAuthorizationError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "authorization_required"):
        super().__init__(error_code)


class SideEffectIdempotencyError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "side_effect_idempotency_conflict"):
        super().__init__(error_code)


class SideEffectAlreadyCompletedError(SideEffectIdempotencyError):
    def __init__(self, error_code: str = "already_completed"):
        super().__init__(error_code)


class SideEffectExecutionError(SideEffectError):
    def __init__(self, error_code: str = "adapter_failed"):
        super().__init__(error_code)


class RollbackNotSupportedError(SideEffectExecutionDeniedError):
    def __init__(self, error_code: str = "rollback_not_supported"):
        super().__init__(error_code)


class RollbackExecutionError(SideEffectExecutionError):
    def __init__(self, error_code: str = "rollback_failed"):
        super().__init__(error_code)


class SideEffectAdapterAlreadyRegisteredError(SideEffectError):
    def __init__(self, error_code: str = "adapter_already_registered"):
        super().__init__(error_code)


class ReconciliationError(SideEffectError):
    def __init__(self, error_code: str = "reconciliation_error"):
        super().__init__(error_code)


class ReconciliationNotEligibleError(ReconciliationError):
    def __init__(self, error_code: str = "reconciliation_not_eligible"):
        super().__init__(error_code)


class ReconciliationConflictError(ReconciliationError):
    def __init__(self, error_code: str = "reconciliation_conflict"):
        super().__init__(error_code)


class ReconciliationNotFoundError(ReconciliationError):
    def __init__(self, error_code: str = "reconciliation_not_found"):
        super().__init__(error_code)
