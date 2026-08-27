"""PDF page rasterization — PDF page → image bytes for OCR."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from documents.errors import PDF_RASTERIZATION_UNAVAILABLE, DocumentError


@dataclass(frozen=True)
class RasterizedPage:
    page: int  # 1-based
    image_bytes: bytes
    width: int = 0
    height: int = 0
    media_type: str = "image/png"


@runtime_checkable
class PdfRasterizer(Protocol):
    provider_id: str
    available: bool

    def rasterize(
        self,
        pdf_bytes: bytes,
        *,
        pages: tuple[int, ...] | None = None,
        scale: float = 2.0,
    ) -> tuple[RasterizedPage, ...]:
        """Rasterize pages (1-based). pages=None → all pages. Deterministic order."""
        ...


class NullPdfRasterizer:
    provider_id = "null"
    available = False

    def rasterize(
        self,
        pdf_bytes: bytes,
        *,
        pages: tuple[int, ...] | None = None,
        scale: float = 2.0,
    ) -> tuple[RasterizedPage, ...]:
        raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE)


class FakePdfRasterizer:
    """Deterministic test rasterizer — emits tiny PNG-like payloads per page."""

    provider_id = "fake"
    available = True

    def __init__(self, *, page_count: int | None = None):
        self._forced_pages = page_count

    def rasterize(
        self,
        pdf_bytes: bytes,
        *,
        pages: tuple[int, ...] | None = None,
        scale: float = 2.0,
    ) -> tuple[RasterizedPage, ...]:
        _ = scale
        total = self._forced_pages
        if total is None:
            # Prefer %PDF page hints via pypdf when present; else 1 page
            try:
                import io

                from pypdf import PdfReader

                total = max(1, len(PdfReader(io.BytesIO(pdf_bytes), strict=False).pages))
            except Exception:
                total = 1
        wanted = pages if pages is not None else tuple(range(1, total + 1))
        out = []
        for p in sorted(wanted):
            if p < 1 or p > total:
                continue
            # Minimal PNG header + page marker (not a real decodeable image for Pillow
            # in all cases — Fake OCR ignores bytes; for Pillow tests use real backend).
            payload = b"\x89PNG\r\n\x1a\n" + f"FAKE_PAGE_{p}".encode("ascii") + pdf_bytes[:8]
            out.append(
                RasterizedPage(
                    page=p,
                    image_bytes=payload,
                    width=100,
                    height=100,
                    media_type="image/png",
                )
            )
        return tuple(out)


class Pypdfium2Rasterizer:
    """Wheel-friendly PDF → PNG via pypdfium2 (no shell tools)."""

    provider_id = "pypdfium2"

    def __init__(self):
        self.available = False
        try:
            import pypdfium2  # noqa: F401
            from PIL import Image  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False

    def rasterize(
        self,
        pdf_bytes: bytes,
        *,
        pages: tuple[int, ...] | None = None,
        scale: float = 2.0,
    ) -> tuple[RasterizedPage, ...]:
        if not self.available:
            raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE)
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE) from exc
        try:
            doc = pdfium.PdfDocument(pdf_bytes)
            total = len(doc)
            wanted = pages if pages is not None else tuple(range(1, total + 1))
            results = []
            for p in sorted(wanted):
                if p < 1 or p > total:
                    continue
                page = doc[p - 1]
                bitmap = page.render(scale=float(scale))
                pil = bitmap.to_pil()
                import io

                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                results.append(
                    RasterizedPage(
                        page=p,
                        image_bytes=buf.getvalue(),
                        width=int(pil.width),
                        height=int(pil.height),
                        media_type="image/png",
                    )
                )
            return tuple(results)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(PDF_RASTERIZATION_UNAVAILABLE) from exc


def build_pdf_rasterizer(env: dict | None = None) -> PdfRasterizer:
    import os

    source = env if env is not None else os.environ
    mode = str(source.get("DOCUMENT_PDF_RASTERIZER", "auto") or "auto").strip().lower()
    if mode in {"null", "disabled", "off", "none"}:
        return NullPdfRasterizer()
    if mode == "fake":
        return FakePdfRasterizer()
    if mode in {"pypdfium2", "auto"}:
        provider = Pypdfium2Rasterizer()
        if provider.available:
            return provider
        if mode == "pypdfium2":
            return NullPdfRasterizer()
        return NullPdfRasterizer()
    return NullPdfRasterizer()
