class HITLError(Exception):
    pass


class ApprovalNotFoundError(HITLError):
    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__("approval_not_found")


class ApprovalConflictError(HITLError):
    def __init__(self, reason_code: str = "approval_conflict"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ApprovalInvalidStateError(HITLError):
    def __init__(self, reason_code: str = "approval_invalid_state"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ApprovalUnauthorizedResolverError(HITLError):
    def __init__(self):
        super().__init__("approval_unauthorized_resolver")


class ApprovalExpiredError(HITLError):
    def __init__(self):
        super().__init__("approval_expired")


class ApprovalSelfApprovalError(HITLError):
    def __init__(self):
        super().__init__("self_approval_not_allowed")


class ExecutionPermitNotFoundError(HITLError):
    def __init__(self, permit_id: str):
        self.permit_id = permit_id
        super().__init__("permit_not_found")


class ExecutionPermitExpiredError(HITLError):
    def __init__(self):
        super().__init__("permit_expired")


class ExecutionPermitConsumedError(HITLError):
    def __init__(self):
        super().__init__("permit_consumed")


class ExecutionPermitMismatchError(HITLError):
    def __init__(self, reason_code: str = "permit_mismatch"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ExecutionPermitRevokedError(HITLError):
    def __init__(self):
        super().__init__("permit_revoked")


class ExecutionPermitConflictError(HITLError):
    def __init__(self, reason_code: str = "permit_conflict"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ActionIntegrityError(HITLError):
    def __init__(self):
        super().__init__("action_changed_after_approval")
