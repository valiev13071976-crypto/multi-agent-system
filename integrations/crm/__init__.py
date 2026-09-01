"""Real CRM integration layer."""

from integrations.crm.config import CrmIntegrationConfig, crm_live_active, crm_live_verified, load_crm_config
from integrations.crm.fixture_adapter import CrmFixtureAdapter, CrmFixtureState
from integrations.crm.live_adapter import LiveCrmAdapter

__all__ = [
    "CrmFixtureAdapter",
    "CrmFixtureState",
    "CrmIntegrationConfig",
    "LiveCrmAdapter",
    "crm_live_active",
    "crm_live_verified",
    "load_crm_config",
]
