class AutonomyError(Exception):
    pass


class AutonomyDeniedError(AutonomyError):
    def __init__(self, reason_code: str = "denied"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class TokenInvalidError(AutonomyError):
    def __init__(self, reason_code: str = "token_invalid"):
        self.reason_code = reason_code
        super().__init__(reason_code)


class TokenExpiredError(TokenInvalidError):
    def __init__(self):
        super().__init__("token_expired")


class IdempotencyConflictError(AutonomyError):
    def __init__(self, key: str, reason_code: str = "duplicate_execution"):
        self.key = key
        self.reason_code = reason_code
        super().__init__(reason_code)
