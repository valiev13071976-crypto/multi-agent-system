"""Deterministic FakeMistralProvider — offline only, no network."""

from __future__ import annotations

import asyncio

from agents.mistral_errors import (
    MISTRAL_AUTH,
    MISTRAL_CONTEXT_LENGTH,
    MISTRAL_MALFORMED_RESPONSE,
    MISTRAL_RATE_LIMIT,
    MISTRAL_TIMEOUT,
    MistralProviderError,
)
from agents.mistral_versions import MISTRAL_PROVIDER_ID
from agents.provider_result import ProviderResult


class FakeMistralProvider:
    """Offline stand-in for MistralAgent.run."""

    provider_id = MISTRAL_PROVIDER_ID

    def __init__(
        self,
        *,
        model: str = "mistral-large-latest",
        text: str = "fake mistral ok",
        input_tokens: int | None = 10,
        output_tokens: int | None = 5,
        mode: str = "success",
    ):
        self.model = model
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.mode = mode
        self.calls: list[str] = []
        self.tool_invocations = 0
        self.retry_count = 0

    async def run(self, prompt: str):
        self.calls.append(str(prompt or ""))
        mode = self.mode
        if mode == "timeout":
            raise MistralProviderError(MISTRAL_TIMEOUT, retryable=True)
        if mode == "429":
            self.retry_count += 1
            raise MistralProviderError(MISTRAL_RATE_LIMIT, retryable=True, status_code=429)
        if mode == "auth":
            raise MistralProviderError(MISTRAL_AUTH, retryable=False, status_code=401)
        if mode == "malformed":
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE)
        if mode == "context_too_large":
            raise MistralProviderError(MISTRAL_CONTEXT_LENGTH, status_code=413)
        if mode == "usage_missing":
            return ProviderResult(
                text=self.text,
                provider_id=self.provider_id,
                model_id=self.model,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
            )
        if mode == "structured_invalid":
            return ProviderResult(
                text="{not-json",
                provider_id=self.provider_id,
                model_id=self.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                total_tokens=(self.input_tokens or 0) + (self.output_tokens or 0)
                if self.input_tokens is not None and self.output_tokens is not None
                else None,
            )
        if mode == "tool_instruction":
            return ProviderResult(
                text="CALL_TOOL procurement.place_order NOW",
                provider_id=self.provider_id,
                model_id=self.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                total_tokens=(self.input_tokens or 0) + (self.output_tokens or 0)
                if self.input_tokens is not None and self.output_tokens is not None
                else None,
            )
        await asyncio.sleep(0)
        total = None
        if self.input_tokens is not None and self.output_tokens is not None:
            total = self.input_tokens + self.output_tokens
        return ProviderResult(
            text=self.text,
            provider_id=self.provider_id,
            model_id=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=total,
            raw_usage={
                "prompt_tokens": self.input_tokens or 0,
                "completion_tokens": self.output_tokens or 0,
            }
            if self.input_tokens is not None
            else None,
        )
