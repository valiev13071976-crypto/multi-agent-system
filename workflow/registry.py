"""In-process WorkflowDefinition registry (versioned references only)."""

from __future__ import annotations

from workflow.dag import validate_definition
from workflow.definition import WorkflowDefinition
from workflow.errors import WorkflowDefinitionError


class DefinitionRegistry:
    def __init__(self):
        self._defs: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition, *, validate: bool = True) -> str:
        if validate:
            validate_definition(definition)
        key = definition.key
        self._defs[key] = definition
        return key

    def get(self, workflow_type: str, version: str) -> WorkflowDefinition:
        key = f"{workflow_type}@{version}"
        definition = self._defs.get(key)
        if definition is None:
            raise WorkflowDefinitionError(
                "definition_not_found",
                f"Unknown workflow definition: {key}",
            )
        return definition

    def get_by_key(self, key: str) -> WorkflowDefinition:
        definition = self._defs.get(key)
        if definition is None:
            raise WorkflowDefinitionError(
                "definition_not_found",
                f"Unknown workflow definition: {key}",
            )
        return definition

    def list_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._defs))
