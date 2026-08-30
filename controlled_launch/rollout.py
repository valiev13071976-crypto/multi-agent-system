"""Progressive rollout step management."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from controlled_launch.errors import INVALID_TRANSITION, ROLLOUT_STEP_BLOCKED, ControlledLaunchError
from controlled_launch.models import ROLLOUT_ORDER, RolloutState, RolloutStep


class RolloutManager:
    def __init__(self, state: RolloutState | None = None, *, candidate_id: str = ""):
        self.state = state or RolloutState(candidate_id=candidate_id, current_step=RolloutStep.INTERNAL.value)

    def _index(self, step: str) -> int:
        for i, item in enumerate(ROLLOUT_ORDER):
            if item.value == step:
                return i
        raise ControlledLaunchError(INVALID_TRANSITION, details={"step": step})

    def complete_current(self) -> RolloutState:
        if self.state.hold or self.state.abort or self.state.rolled_back:
            raise ControlledLaunchError(ROLLOUT_STEP_BLOCKED, details={"state": self.state.as_dict()})
        step = self.state.current_step
        if step not in self.state.completed_steps:
            self.state.completed_steps.append(step)
        idx = self._index(step)
        if idx + 1 < len(ROLLOUT_ORDER):
            self.state.current_step = ROLLOUT_ORDER[idx + 1].value
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        return self.state

    def advance_to(self, target: RolloutStep) -> RolloutState:
        if self.state.hold or self.state.abort:
            raise ControlledLaunchError(ROLLOUT_STEP_BLOCKED)
        current_idx = self._index(self.state.current_step)
        target_idx = self._index(target.value)
        if target_idx > current_idx + 1:
            raise ControlledLaunchError(ROLLOUT_STEP_BLOCKED, details={"skip_to": target.value})
        if target_idx == current_idx + 1 and self.state.current_step not in self.state.completed_steps:
            raise ControlledLaunchError(ROLLOUT_STEP_BLOCKED, details={"current_not_complete": self.state.current_step})
        self.state.current_step = target.value
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        return self.state

    def hold(self) -> RolloutState:
        self.state.hold = True
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        return self.state

    def abort(self) -> RolloutState:
        self.state.abort = True
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        return self.state

    def rollback(self) -> RolloutState:
        self.state.rolled_back = True
        self.state.abort = True
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        return self.state
