"""Feature flag for the managed-agent-runtime POC.

Default MUST remain false: existing production behavior (RouterV2,
WorkflowPandaConversationGateway, the existing Data Intelligence
service, the existing Bitrix workflow, ToolGateway, existing durable
execution) is completely unaffected regardless of this flag's value,
because nothing in ``main.py`` or any production module imports this
package. The flag exists as this POC's OWN internal safety gate, so
that even a direct, deliberate invocation of ``ManagedAgentPOC`` refuses
to run unless explicitly opted in -- mirroring the "keep it behind a
separate feature flag, default false" requirement even though this POC
is not wired into any production entry point.
"""

from __future__ import annotations

import os

FLAG_ENV_VAR = "PANDA_MANAGED_AGENT_POC_ENABLED"


def is_enabled() -> bool:
    raw = str(os.environ.get(FLAG_ENV_VAR, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class ManagedAgentPocDisabledError(RuntimeError):
    """Raised by ``ManagedAgentPOC`` when invoked while the POC flag is
    off (the default). Never raised by, or visible to, any existing
    Panda production code path."""

    def __init__(self):
        super().__init__(
            f"Managed Agent Runtime POC is disabled (default). "
            f"Set {FLAG_ENV_VAR}=true to opt in explicitly -- this is an "
            f"experimental POC module, never invoked by production routing."
        )
