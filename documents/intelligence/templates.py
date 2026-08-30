"""Document template validation before generation."""

from __future__ import annotations

from typing import Mapping

from documents.errors import GENERATED_DOCUMENT_INVALID, GENERATION_FAILED, DocumentError
from documents.platform_models import DocumentTemplate


def validate_template_fields(
    template: DocumentTemplate,
    fields: Mapping[str, object] | None,
) -> None:
    """Fail closed when required fields are missing/empty/corrupt."""
    data = dict(fields or {})
    missing = []
    for name in template.required_fields:
        if name not in data:
            missing.append(name)
            continue
        val = data[name]
        if val is None:
            missing.append(name)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(name)
            continue
        if isinstance(val, (list, tuple, dict)) and len(val) == 0:
            missing.append(name)
    if missing:
        raise DocumentError(GENERATED_DOCUMENT_INVALID)

    # Corrupt markers
    for name, val in data.items():
        if isinstance(val, str) and "\x00" in val:
            raise DocumentError(GENERATED_DOCUMENT_INVALID)


def assert_generation_inputs(
    *,
    title: str | None,
    paragraphs: list | None,
    template: DocumentTemplate | None = None,
    fields: Mapping[str, object] | None = None,
) -> None:
    if template is not None:
        validate_template_fields(template, fields)
        return
    if not (title or "").strip() and not any(str(p).strip() for p in (paragraphs or [])):
        raise DocumentError(GENERATION_FAILED)
