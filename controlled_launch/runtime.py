"""Runtime wiring for controlled launch."""

from __future__ import annotations

from controlled_launch.service import ControlledLaunchService


_runtime: ControlledLaunchService | None = None


def get_controlled_launch_runtime() -> ControlledLaunchService:
    global _runtime
    if _runtime is None:
        _runtime = ControlledLaunchService()
    return _runtime


def configure_controlled_launch_runtime(service: ControlledLaunchService) -> ControlledLaunchService:
    global _runtime
    _runtime = service
    return _runtime
