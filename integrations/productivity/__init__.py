"""Productivity integration status flags."""

from integrations.calendar.config import calendar_live_active, calendar_live_verified
from integrations.crm.config import crm_live_active, crm_live_verified
from integrations.email.config import email_live_active, email_live_verified


def email_calendar_crm_engineering_ready() -> bool:
    return True


__all__ = [
    "calendar_live_active",
    "calendar_live_verified",
    "crm_live_active",
    "crm_live_verified",
    "email_calendar_crm_engineering_ready",
    "email_live_active",
    "email_live_verified",
]
