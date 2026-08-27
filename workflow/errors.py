class WorkflowError(Exception):
    pass


class WorkflowTransitionError(WorkflowError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Invalid workflow transition: {current} -> {target}")


class WorkflowNotFoundError(WorkflowError):
    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        super().__init__(f"Workflow not found: {workflow_id}")


class WorkflowConflictError(WorkflowError):
    pass


class WaitingApprovalError(WorkflowError):
    pass


class WorkflowDefinitionError(WorkflowError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        step_id: str | None = None,
        dependency: str | None = None,
    ):
        self.error_code = error_code
        self.step_id = step_id
        self.dependency = dependency
        super().__init__(message)


class WorkflowTimeoutError(WorkflowError):
    def __init__(self, error_code: str = "execution_timeout", *, scope: str = "step"):
        self.error_code = error_code
        self.scope = scope
        super().__init__(error_code)


class WorkflowCancelledError(WorkflowError):
    def __init__(self, error_code: str = "workflow_cancelled"):
        self.error_code = error_code
        super().__init__(error_code)


class WorkflowDeadlineExceededError(WorkflowTimeoutError):
    def __init__(self):
        super().__init__("workflow_deadline_exceeded", scope="workflow")
