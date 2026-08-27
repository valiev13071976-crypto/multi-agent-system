"""Safe filesystem tool adapter."""

from __future__ import annotations

import os
from pathlib import Path

from tools.errors import ToolArgumentInvalidError, ToolPermissionDeniedError
from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE


class FilesystemAdapter:
    adapter_id = "filesystem"

    def __init__(self, *, allowed_roots: tuple[str, ...] = ()):
        self._roots = tuple(str(Path(r).resolve()) for r in allowed_roots if r)

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("filesystem.")

    def health(self) -> str:
        if not self._roots:
            return ADAPTER_UNAVAILABLE
        return ADAPTER_HEALTHY

    def _resolve(self, raw_path: str) -> Path:
        if not raw_path or "\x00" in raw_path:
            raise ToolArgumentInvalidError()
        candidate = Path(str(raw_path))
        parts = candidate.as_posix().replace("\\", "/").split("/")
        if ".." in parts:
            raise ToolPermissionDeniedError()
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ToolPermissionDeniedError() from exc
        if resolved.is_symlink():
            raise ToolPermissionDeniedError()
        if not self._roots:
            raise ToolPermissionDeniedError()
        ok = False
        for root in self._roots:
            try:
                resolved.relative_to(root)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise ToolPermissionDeniedError()
        return resolved

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        op = request.operation
        if op == "list":
            path = self._resolve(str(args.get("path") or self._roots[0]))
            if not path.is_dir():
                raise ToolArgumentInvalidError()
            entries = []
            for entry in sorted(path.iterdir())[:256]:
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": entry.stat().st_size if entry.is_file() else None,
                    }
                )
            return {"entries": entries, "path": str(path)}
        if op == "read":
            path = self._resolve(str(args.get("path") or ""))
            if not path.is_file():
                raise ToolArgumentInvalidError()
            max_bytes = min(int(args.get("max_bytes") or 65536), 65536)
            data = path.read_bytes()[:max_bytes]
            return {
                "path": str(path),
                "size": path.stat().st_size,
                "content_text": data.decode("utf-8", errors="replace"),
                "truncated": path.stat().st_size > max_bytes,
            }
        if op == "metadata":
            path = self._resolve(str(args.get("path") or ""))
            st = path.stat()
            return {
                "path": str(path),
                "is_file": path.is_file(),
                "is_dir": path.is_dir(),
                "size": st.st_size,
                "modified": st.st_mtime,
            }
        raise ToolArgumentInvalidError()

    async def execute_write(self, request, context) -> dict:
        args = dict(request.arguments or {})
        op = request.operation
        if op == "write":
            path = self._resolve(str(args.get("path") or ""))
            content = str(args.get("content") or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"path": str(path), "written_bytes": len(content.encode("utf-8"))}
        if op == "mkdir":
            path = self._resolve(str(args.get("path") or ""))
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path), "created": True}
        raise ToolArgumentInvalidError()
