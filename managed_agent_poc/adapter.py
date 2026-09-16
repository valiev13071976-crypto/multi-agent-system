"""Parent-side POC adapter — runs entirely in Panda's normal process/
environment. NEVER imports the OpenAI Agents SDK directly (that import
only ever happens inside the isolated subprocess, see
``runtime_subprocess.py`` and ``managed_agent_poc/__init__.py`` for why).

Conceptually:

    Panda -> ManagedAgentPOC -> (isolated subprocess: semantic tool
             selection over the 3 safe Panda tools) -> structured result

This class is never imported by ``main.py`` or any existing Panda
runtime module. It exists only for this POC's own tests/demo scripts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from managed_agent_poc import isolated_env
from managed_agent_poc.flags import ManagedAgentPocDisabledError, is_enabled

_SUBPROCESS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_subprocess.py")


class ManagedAgentPocUnavailableError(RuntimeError):
    """Raised when the isolated OpenAI Agents SDK install is missing."""


class ManagedAgentPocNoApiKeyError(RuntimeError):
    """Raised when a REAL-model turn (``test_scripted_plan`` omitted) is
    requested but ``OPENAI_API_KEY`` is not present in the environment.

    Deliberately checked and raised HERE, before the subprocess is even
    launched -- never lets a real-model request silently attempt (and
    fail) a network call, and never invents a workaround (no hardcoded
    key, no silent fallback to a scripted/mocked decision)."""

    def __init__(self):
        super().__init__(
            "OPENAI_API_KEY is not set in this environment -- refusing to "
            "attempt a real-model managed-agent turn. Add it via Cloud "
            "Agents \u2192 Secrets to run this live; no live evaluation "
            "result may be claimed without it."
        )


@dataclass
class ManagedAgentTurnResult:
    status: str
    final_output: str = ""
    tool_calls: list = field(default_factory=list)
    dataset_id: str = ""
    shown_identifiers: list = field(default_factory=list)
    current_identifier: str = ""
    error: str = ""
    model: str = ""
    usage: dict | None = None
    # Business-task-ownership/workset-continuation defect closure (PR #90
    # correction): set only after a successful ``apply_scoped_price_
    # rules`` tool call this turn -- see ``runtime_subprocess.
    # ConversationState.scoped_rules_workbook`` for why this never passes
    # through the model's own context/tool-output.
    scoped_rules_workbook: dict | None = None


class ManagedAgentPOC:
    """Runs ONE managed-agent turn in the isolated subprocess.

    ``dataset_store_path``/``session_db_path`` are dedicated SQLite files
    (never Panda's production dataset/session stores) so this POC can
    never read or write real tenant data merely by being invoked --
    callers (tests/demo scripts) point these at their own scratch files.
    """

    def __init__(self, *, dataset_store_path: str, session_db_path: str, state_store_path: str | None = None):
        self.dataset_store_path = dataset_store_path
        self.session_db_path = session_db_path
        # Durable Panda-owned business state (dataset_id / shown-product
        # history / current selection) -- separate from the SDK's own
        # session transcript store. Defaults to sharing the session's file
        # (a distinct table) when not given its own path.
        self.state_store_path = state_store_path or session_db_path

    @staticmethod
    def availability() -> tuple[bool, str]:
        if not is_enabled():
            return False, f"disabled ({isolated_env.PKGS_DIR_ENV_VAR.replace('PKGS_DIR', 'ENABLED')} is not set to true)"
        if not isolated_env.is_installed():
            return False, isolated_env.describe_unavailable()
        return True, ""

    @staticmethod
    def real_model_available() -> tuple[bool, str]:
        """Whether a REAL (non-scripted) model turn can even be attempted.
        Checks presence only -- never reads, logs, or returns the key
        value itself."""
        available, reason = ManagedAgentPOC.availability()
        if not available:
            return False, reason
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY is not set in this environment"
        return True, ""

    def run_turn(
        self,
        *,
        text: str,
        tenant_id: str,
        owner_id: str = "",
        conversation_id: str,
        dataset_id: str = "",
        artifact_bytes_path: str = "",
        artifact_filename: str = "",
        test_scripted_plan: list[dict] | None = None,
        timeout_s: float = 60.0,
    ) -> ManagedAgentTurnResult:
        if not is_enabled():
            raise ManagedAgentPocDisabledError()
        if not isolated_env.is_installed():
            raise ManagedAgentPocUnavailableError(isolated_env.describe_unavailable())
        if test_scripted_plan is None and not os.environ.get("OPENAI_API_KEY"):
            raise ManagedAgentPocNoApiKeyError()

        request = {
            "text": text,
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "dataset_store_path": self.dataset_store_path,
            "dataset_id": dataset_id,
            "artifact_bytes_path": artifact_bytes_path,
            "artifact_filename": artifact_filename,
            "session_db_path": self.session_db_path,
            "state_store_path": self.state_store_path,
        }
        if test_scripted_plan is not None:
            request["test_scripted_plan"] = test_scripted_plan

        env = dict(os.environ)
        env["PANDA_MANAGED_AGENT_POC_PKGS_DIR"] = isolated_env.pkgs_dir()

        proc = subprocess.run(
            [sys.executable, _SUBPROCESS_SCRIPT],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            return ManagedAgentTurnResult(status="ERROR", error=proc.stderr[-4000:] or "subprocess failed with no output")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ManagedAgentTurnResult(status="ERROR", error=f"non-JSON subprocess output: {proc.stdout!r} / stderr: {proc.stderr[-2000:]!r}")

        return ManagedAgentTurnResult(
            status=str(payload.get("status") or "ERROR"),
            final_output=str(payload.get("final_output") or ""),
            tool_calls=list(payload.get("tool_calls") or []),
            dataset_id=str(payload.get("dataset_id") or ""),
            shown_identifiers=list(payload.get("shown_identifiers") or []),
            current_identifier=str(payload.get("current_identifier") or ""),
            error=str(payload.get("error") or ""),
            model=str(payload.get("model") or ""),
            usage=payload.get("usage"),
            scoped_rules_workbook=payload.get("scoped_rules_workbook"),
        )
