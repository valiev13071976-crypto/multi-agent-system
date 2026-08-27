"""Prompt-injection practical controls — data vs policy separation."""

from __future__ import annotations

UNTRUSTED_SOURCE_TAG = "untrusted_external_content"


def tag_untrusted_content(text: str, *, source: str = "external") -> str:
    """Wrap retrieved/external text so downstream treats it as data, not policy."""
    body = str(text or "")
    return f"[{UNTRUSTED_SOURCE_TAG} source={source}]\n{body}"


def instruction_from_untrusted_content(_text: str) -> bool:
    """Retrieved content must never grant capabilities or override policy."""
    return False


def validate_model_output_for_capability_grant(_output: str) -> bool:
    """Model output cannot manufacture capability tokens."""
    return False
