"""Composio OPTIONAL adapter — NOT the Panda Tool Platform."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.activation.errors import IntegrationCapabilityUnavailableError, IntegrationWriteDeniedError
from integrations.activation.models import (
    COMPOSIO_PROVIDER_CONFIGURED,
    COMPOSIO_USER_ACTIVE,
    COMPOSIO_USER_REQUIRED,
)


# Discovered tools are untrusted metadata until mapped to allowlisted capabilities.
COMPOSIO_TOOL_ALLOWLIST: dict[str, dict] = {
    "GMAIL_FETCH_EMAILS": {"capability": "email.read", "operation_class": "READ"},
    "GMAIL_SEND_EMAIL": {"capability": "email.send", "operation_class": "WRITE"},
    "GOOGLECALENDAR_LIST_EVENTS": {"capability": "calendar.read", "operation_class": "READ"},
    "SLACK_SEND_MESSAGE": {"capability": "slack.message.send", "operation_class": "WRITE"},
    "GOOGLEDRIVE_LIST_FILES": {"capability": "drive.file.read", "operation_class": "READ"},
}


@dataclass
class ComposioFixtureAdapter(FixtureProviderAdapter):
    """Fixture Composio broker: platform key ≠ user Gmail connection."""

    def __init__(self, *, state: FixtureAdapterState | None = None):
        super().__init__("composio", state=state)
        self.platform_configured = False
        self.user_connections: dict[str, str] = {}  # toolkit -> status
        self.discovered_tools: list[str] = list(COMPOSIO_TOOL_ALLOWLIST.keys()) + ["UNKNOWN_DANGEROUS_TOOL"]

    def configure_platform(self, *, credential_ref: str) -> dict:
        if not credential_ref.startswith("secret:"):
            raise ValueError("composio_credential_ref_required")
        self.platform_configured = True
        return {"status": COMPOSIO_PROVIDER_CONFIGURED, "live": False, "mode": "FIXTURE"}

    def user_connection_status(self, *, toolkit: str) -> str:
        if not self.platform_configured:
            return COMPOSIO_PROVIDER_CONFIGURED
        return self.user_connections.get(toolkit, COMPOSIO_USER_REQUIRED)

    def connect_user(self, *, toolkit: str) -> dict:
        if not self.platform_configured:
            return {"status": COMPOSIO_PROVIDER_CONFIGURED, "toolkit": toolkit}
        self.user_connections[toolkit] = COMPOSIO_USER_ACTIVE
        return {"status": COMPOSIO_USER_ACTIVE, "toolkit": toolkit, "live": False}

    def discover_tools(self) -> list[dict]:
        out = []
        for name in self.discovered_tools:
            mapped = COMPOSIO_TOOL_ALLOWLIST.get(name)
            out.append(
                {
                    "tool": name,
                    "mapped_capability": mapped["capability"] if mapped else None,
                    "operation_class": mapped["operation_class"] if mapped else None,
                    "allowed": mapped is not None,
                    "untrusted_metadata": True,
                }
            )
        return out

    def map_tool(self, tool_name: str) -> dict:
        mapped = COMPOSIO_TOOL_ALLOWLIST.get(tool_name)
        if mapped is None:
            raise IntegrationCapabilityUnavailableError(f"unknown_composio_tool:{tool_name}")
        return mapped

    def execute_mapped(
        self,
        *,
        tool_name: str,
        payload: dict,
        idempotency_key: str,
        approved_write: bool,
    ) -> dict:
        mapped = self.map_tool(tool_name)
        if mapped["operation_class"] == "WRITE" and not approved_write:
            raise IntegrationWriteDeniedError("composio_write_requires_governance")
        toolkit = "gmail" if ("GMAIL" in tool_name or mapped["capability"].startswith("email")) else "generic"
        if mapped["operation_class"] == "WRITE":
            if toolkit == "gmail" and self.user_connections.get("gmail") != COMPOSIO_USER_ACTIVE:
                return {"status": COMPOSIO_USER_REQUIRED, "blocked": True, "live": False}
            return self.write(capability=mapped["capability"], payload=payload, idempotency_key=idempotency_key)
        return self.read(capability=mapped["capability"], params=payload)
