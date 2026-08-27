"""OCR provider abstraction — no silent OCR; explicit availability."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from documents.errors import OCR_FAILED, OCR_UNAVAILABLE, DocumentError
from documents.intelligence.contracts import CONF_HIGH, CONF_LOW, CONF_MEDIUM, CONF_UNRESOLVED


@runtime_checkable
class OCRProvider(Protocol):
    provider_id: str
    available: bool

    def recognize(self, data: bytes, *, filename: str = "") -> dict: ...


class NullOCRProvider:
    provider_id = "null"
    available = False

    def recognize(self, data: bytes, *, filename: str = "") -> dict:
        raise DocumentError(OCR_UNAVAILABLE)


class FakeOCRProvider:
    """Deterministic test/provider stub — returns injected text."""

    provider_id = "fake"
    available = True

    def __init__(self, text: str = "OCR SAMPLE TEXT", *, confidence: float = 0.9):
        self._text = text
        self._confidence = float(confidence)

    def recognize(self, data: bytes, *, filename: str = "") -> dict:
        _ = data
        text = self._text
        # Allow page-tagged filename for multi-page tests: page-3.png
        if "page-" in (filename or ""):
            try:
                part = filename.split("page-", 1)[1]
                num = "".join(ch for ch in part if ch.isdigit())
                if num:
                    text = f"{self._text} [page {num}]"
            except Exception:
                pass
        level = CONF_HIGH if self._confidence >= 0.8 else CONF_MEDIUM if self._confidence >= 0.5 else CONF_LOW
        return {
            "text": text,
            "provider": self.provider_id,
            "confidence_raw": self._confidence,
            "confidence_level": level,
            "warnings": (),
        }


class TesseractOCRProvider:
    """Local Tesseract via pytesseract+Pillow when installed."""

    provider_id = "tesseract"

    def __init__(self):
        self.available = False
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False

    def recognize(self, data: bytes, *, filename: str = "") -> dict:
        if not self.available:
            raise DocumentError(OCR_UNAVAILABLE)
        try:
            import io

            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            # Basic orientation hint
            try:
                img = img.convert("L")
            except Exception:
                pass
            text = pytesseract.image_to_string(img) or ""
            conf = 0.7 if text.strip() else 0.0
            level = CONF_MEDIUM if conf >= 0.5 else CONF_LOW if conf > 0 else CONF_UNRESOLVED
            warnings = []
            if img.width < 200 or img.height < 200:
                warnings.append("low_resolution")
            return {
                "text": text,
                "provider": self.provider_id,
                "confidence_raw": conf,
                "confidence_level": level,
                "warnings": tuple(warnings),
                "width": img.width,
                "height": img.height,
            }
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError(OCR_FAILED) from exc


def build_ocr_provider(env: dict | None = None):
    import os

    source = env if env is not None else os.environ
    mode = str(source.get("DOCUMENT_OCR_PROVIDER", "auto") or "auto").strip().lower()
    if mode in {"null", "disabled", "off", "none"}:
        return NullOCRProvider()
    if mode == "fake":
        return FakeOCRProvider()
    if mode in {"tesseract", "auto"}:
        provider = TesseractOCRProvider()
        if provider.available:
            return provider
        if mode == "tesseract":
            return NullOCRProvider()  # explicit request but unavailable
        return NullOCRProvider()
    return NullOCRProvider()
