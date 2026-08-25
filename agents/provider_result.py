from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


USAGE_INT_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "promptTokenCount",
    "candidatesTokenCount",
    "totalTokenCount",
)


@dataclass(frozen=True)
class ProviderResult:
    text: str
    provider_id: str
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    raw_usage: Mapping[str, int] | None = None

    def __post_init__(self):
        if self.raw_usage is not None:
            object.__setattr__(self, "raw_usage", MappingProxyType(dict(self.raw_usage)))


def as_token_count(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _safe_usage_dict(usage) -> dict:
    if not isinstance(usage, dict):
        return {}
    cleaned = {}
    for key in USAGE_INT_KEYS:
        parsed = as_token_count(usage.get(key))
        if parsed is not None:
            cleaned[key] = parsed
    return cleaned


def _pack(
    provider_id: str,
    model_id: str,
    text: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    raw: dict,
) -> ProviderResult:
    if (
        total_tokens is None
        and input_tokens is not None
        and output_tokens is not None
    ):
        total_tokens = input_tokens + output_tokens
    return ProviderResult(
        text=text or "",
        provider_id=provider_id,
        model_id=model_id or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        raw_usage=raw or None,
    )


def usage_from_openai_response(data: dict, *, provider_id: str, model_id: str, text: str) -> ProviderResult:
    usage = _safe_usage_dict(data.get("usage") if isinstance(data, dict) else None)
    return _pack(
        provider_id,
        model_id,
        text,
        usage.get("input_tokens", usage.get("prompt_tokens")),
        usage.get("output_tokens", usage.get("completion_tokens")),
        usage.get("total_tokens"),
        usage,
    )


def usage_from_anthropic_response(data: dict, *, provider_id: str, model_id: str, text: str) -> ProviderResult:
    usage = _safe_usage_dict(data.get("usage") if isinstance(data, dict) else None)
    return _pack(
        provider_id,
        model_id,
        text,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        usage,
    )


def usage_from_gemini_response(data: dict, *, provider_id: str, model_id: str, text: str) -> ProviderResult:
    meta = data.get("usageMetadata") if isinstance(data, dict) else None
    usage = _safe_usage_dict(meta)
    return _pack(
        provider_id,
        model_id,
        text,
        usage.get("promptTokenCount"),
        usage.get("candidatesTokenCount"),
        usage.get("totalTokenCount"),
        usage,
    )


def usage_from_chat_completions_response(data: dict, *, provider_id: str, model_id: str, text: str) -> ProviderResult:
    usage = _safe_usage_dict(data.get("usage") if isinstance(data, dict) else None)
    return _pack(
        provider_id,
        model_id,
        text,
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
        usage.get("total_tokens"),
        usage,
    )


def provider_result_from_text(provider_id: str, model_id: str, text: str) -> ProviderResult:
    return ProviderResult(
        text=text,
        provider_id=provider_id,
        model_id=model_id or "",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        raw_usage=None,
    )
