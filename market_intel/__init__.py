"""Telegram Wholesale / Market Intelligence.

Read-only market observation from the owner's OWN Telegram account
(MTProto user client), kept strictly separate from the existing Telegram
Bot API interface in ``telegram_interface`` (that package is the
conversational Panda channel and is not touched by this one).

This subsystem CONSUMES the existing normalized product/catalog output
(``product_intel.platform_models.Product``) through a thin read-only
adapter and reuses the existing deterministic matcher
(``product_intel.matching``). It never parses spreadsheets, never
ingests files and never writes to the catalog.
"""
