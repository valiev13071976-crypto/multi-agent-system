"""Block 4.28/4.29 — user personalization (response style/tone/length/
language) and voice-selection preferences.

Canonical, single preference source per (tenant_id, owner_id). Reused
identically by the text chat pipeline (business_assistant_api.service) and
the realtime voice pipeline (realtime.bridge) -- never a parallel
personality/voice system per channel.
"""
