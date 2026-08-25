"""ZIP / archive expansion safety for OOXML (xlsx/docx)."""

from __future__ import annotations

import io
import zipfile

from documents.errors import ARCHIVE_EXPANSION_LIMIT_EXCEEDED, DocumentError


DEFAULT_MAX_ENTRIES = 2_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 50_000_000
DEFAULT_MAX_COMPRESSION_RATIO = 100.0


def inspect_zip_safety(
    data: bytes,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> dict:
    """Validate ZIP structure without extracting payloads to disk."""
    if not data:
        raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED)
            total_uncompressed = 0
            names = []
            for info in infos:
                names.append(info.filename)
                # Path traversal via zip slip
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED)
                uncompressed = int(info.file_size)
                compressed = max(1, int(info.compress_size))
                total_uncompressed += uncompressed
                if total_uncompressed > max_uncompressed_bytes:
                    raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED)
                ratio = uncompressed / compressed
                if ratio > max_ratio and uncompressed > 1_000_000:
                    raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED)
            return {
                "entry_count": len(infos),
                "uncompressed_bytes": total_uncompressed,
                "names": tuple(names),
            }
    except DocumentError:
        raise
    except zipfile.BadZipFile as exc:
        raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED) from exc
    except Exception as exc:
        raise DocumentError(ARCHIVE_EXPANSION_LIMIT_EXCEEDED) from exc
