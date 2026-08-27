"""Built-in workflow definitions for platform demos/tests (not product verticals)."""

from workflow.definition import (
    BranchCondition,
    BranchRule,
    FAILURE_FAIL_WORKFLOW,
    FAILURE_RETRY,
    FAILURE_SKIP,
    STEP_TYPE_BRANCH,
    STEP_TYPE_HANDLER,
    StepRetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)


def linear_demo_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="demo.linear",
        version="1",
        steps=(
            WorkflowStep(step_id="a", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="b",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("a",),
            ),
            WorkflowStep(
                step_id="c",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("b",),
            ),
        ),
    )


def parallel_demo_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="demo.parallel",
        version="1",
        steps=(
            WorkflowStep(step_id="root", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="left",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("root",),
            ),
            WorkflowStep(
                step_id="right",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("root",),
            ),
            WorkflowStep(
                step_id="join",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("left", "right"),
            ),
        ),
    )


def branch_demo_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="demo.branch",
        version="1",
        steps=(
            WorkflowStep(step_id="decide", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="gate",
                step_type=STEP_TYPE_BRANCH,
                dependencies=("decide",),
                branch=BranchRule(
                    condition=BranchCondition(
                        source_step_id="decide",
                        field="path",
                        op="eq",
                        value="left",
                    ),
                    then_steps=("left_path",),
                    else_steps=("right_path",),
                ),
            ),
            WorkflowStep(
                step_id="left_path",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("gate",),
            ),
            WorkflowStep(
                step_id="right_path",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("gate",),
            ),
        ),
    )


def retry_demo_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="demo.retry",
        version="1",
        steps=(
            WorkflowStep(
                step_id="flaky",
                step_type=STEP_TYPE_HANDLER,
                retry_policy=StepRetryPolicy(
                    max_attempts=3,
                    base_delay_seconds=0.01,
                    backoff_mode="fixed",
                ),
                failure_policy=FAILURE_RETRY,
            ),
        ),
    )


def register_builtin_definitions(registry) -> None:
    for factory in (
        linear_demo_definition,
        parallel_demo_definition,
        branch_demo_definition,
        retry_demo_definition,
    ):
        registry.register(factory())
