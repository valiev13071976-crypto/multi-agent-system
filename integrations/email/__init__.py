"""Real Email integration layer."""

from integrations.email.config import EmailIntegrationConfig, email_live_active, email_live_verified, load_email_config
from integrations.email.fixture_adapter import EmailFixtureAdapter, EmailFixtureState
from integrations.email.live_adapter import LiveEmailAdapter

__all__ = [
    "EmailFixtureAdapter",
    "EmailFixtureState",
    "EmailIntegrationConfig",
    "LiveEmailAdapter",
    "email_live_active",
    "email_live_verified",
    "load_email_config",
]
