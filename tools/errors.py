"""ToolGateway / ToolRegistry error types."""


class ToolError(Exception):
    def __init__(self, error_code: str = "tool_execution_failed"):
        self.error_code = error_code
        super().__init__(error_code)


class ToolNotFoundError(ToolError):
    def __init__(self, error_code: str = "tool_not_found"):
        super().__init__(error_code)


class ToolDisabledError(ToolError):
    def __init__(self, error_code: str = "tool_disabled"):
        super().__init__(error_code)


class ToolOperationNotAllowedError(ToolError):
    def __init__(self, error_code: str = "tool_operation_not_allowed"):
        super().__init__(error_code)


class ToolCapabilityError(ToolError):
    def __init__(self, error_code: str = "missing_tool_capability"):
        super().__init__(error_code)


class ToolArgumentInvalidError(ToolError):
    def __init__(self, error_code: str = "tool_argument_invalid"):
        super().__init__(error_code)


class ToolTimeoutError(ToolError):
    def __init__(self, error_code: str = "tool_timeout"):
        super().__init__(error_code)


class ToolPolicyDeniedError(ToolError):
    def __init__(self, error_code: str = "tool_policy_denied"):
        super().__init__(error_code)


class ToolApprovalRequiredError(ToolError):
    def __init__(self, error_code: str = "tool_approval_required"):
        super().__init__(error_code)


class ToolPermitInvalidError(ToolError):
    def __init__(self, error_code: str = "tool_permit_invalid"):
        super().__init__(error_code)


class ToolSideEffectUncertainError(ToolError):
    def __init__(self, error_code: str = "tool_side_effect_uncertain"):
        super().__init__(error_code)


class ToolPersistenceUnavailableError(ToolError):
    def __init__(self, error_code: str = "tool_persistence_unavailable"):
        super().__init__(error_code)


class ToolRegistryFrozenError(ToolError):
    def __init__(self, error_code: str = "tool_registry_frozen"):
        super().__init__(error_code)


class ToolRegistryConflictError(ToolError):
    def __init__(self, error_code: str = "tool_already_registered"):
        super().__init__(error_code)


class ToolIdempotencyRequiredError(ToolError):
    def __init__(self, error_code: str = "idempotency_key_required"):
        super().__init__(error_code)
