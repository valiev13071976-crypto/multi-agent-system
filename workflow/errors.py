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
