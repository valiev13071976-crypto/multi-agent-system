"""Deterministic bounded chunking with structural boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.platform_models import KNOWLEDGE_CHUNKER_VERSION, chunk_content_hash

DEFAULT_MAX_CHUNK_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 100
MAX_CHUNKS_PER_SOURCE = 10_000


@dataclass(frozen=True)
class ChunkSpec:
    sequence: int
    content: str
    content_hash: str
    char_start: int
    char_end: int
    section_ref: str | None
    overlap_prev: int
    token_estimate: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_sections(text: str) -> list[tuple[str, str | None]]:
    """Split on headings / double newlines / page markers."""
    parts: list[tuple[str, str | None]] = []
    heading_re = re.compile(r"(?m)^(#{1,6}\s+.+|={3,}|-{3,}|\f|Page \d+)\s*$")
    last = 0
    section = None
    for match in heading_re.finditer(text):
        chunk = text[last : match.start()].strip()
        if chunk:
            parts.append((chunk, section))
        section = match.group(0).strip()[:120]
        last = match.end()
    tail = text[last:].strip()
    if tail:
        parts.append((tail, section))
    if not parts and text.strip():
        parts.append((text.strip(), None))
    return parts


def _split_paragraphs(block: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
    return paras or ([block.strip()] if block.strip() else [])


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
    profile_version: str = KNOWLEDGE_CHUNKER_VERSION,
) -> list[ChunkSpec]:
    _ = profile_version
    normalized = str(text or "").strip()
    if not normalized:
        return []

    specs: list[ChunkSpec] = []
    cursor = 0
    seq = 0

    for section_block, section_ref in _split_sections(normalized):
        for para in _split_paragraphs(section_block):
            if len(para) <= max_chars:
                start = normalized.find(para, cursor)
                if start < 0:
                    start = cursor
                end = start + len(para)
                specs.append(
                    ChunkSpec(
                        sequence=seq,
                        content=para,
                        content_hash=chunk_content_hash(para),
                        char_start=start,
                        char_end=end,
                        section_ref=section_ref,
                        overlap_prev=0,
                        token_estimate=_estimate_tokens(para),
                    )
                )
                seq += 1
                cursor = end
                if seq >= MAX_CHUNKS_PER_SOURCE:
                    return specs
                continue

            # Hard split long paragraphs
            pos = 0
            while pos < len(para):
                piece = para[pos : pos + max_chars]
                if not piece:
                    break
                overlap_prev = overlap if pos > 0 else 0
                if overlap_prev and pos >= overlap:
                    piece = para[pos - overlap : pos + max_chars - overlap]
                start = normalized.find(piece[: min(32, len(piece))], cursor)
                if start < 0:
                    start = cursor
                end = start + len(piece)
                specs.append(
                    ChunkSpec(
                        sequence=seq,
                        content=piece,
                        content_hash=chunk_content_hash(piece),
                        char_start=start,
                        char_end=end,
                        section_ref=section_ref,
                        overlap_prev=overlap_prev,
                        token_estimate=_estimate_tokens(piece),
                    )
                )
                seq += 1
                pos += max_chars - overlap if overlap else max_chars
                cursor = end
                if seq >= MAX_CHUNKS_PER_SOURCE:
                    return specs

    return specs
