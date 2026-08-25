import re

from security.secrets import SecretProvider


REDACTED = "[REDACTED]"

_BEARER_HEADER_RE = re.compile(
    r"(?i)(authorization:\s*bearer\s+)\S+"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)(\bbearer\s+)[A-Za-z0-9._\-+=/]+"
)
_CREDENTIAL_FIELD_RE = re.compile(
    r'(?i)([\'"]?(?:password|secret|token|api_key|access_token|refresh_token)'
    r'[\'"]?\s*[:=]\s*)([\'"]?)([^\'"\s,;]+)(\2)'
)


def _longest_first(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    if text is None:
        return text
    rendered = str(text)
    known = extra_secrets + SecretProvider().known_secret_values()
    for secret in _longest_first(known):
        rendered = rendered.replace(secret, REDACTED)
    rendered = _BEARER_HEADER_RE.sub(rf"\1{REDACTED}", rendered)
    rendered = _BEARER_TOKEN_RE.sub(rf"\1{REDACTED}", rendered)
    rendered = _CREDENTIAL_FIELD_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(4)}",
        rendered,
    )
    return rendered
