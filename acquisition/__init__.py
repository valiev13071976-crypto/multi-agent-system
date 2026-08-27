"""Data Acquisition & Parsing Platform."""

from acquisition.models import (
    AcquisitionRequest,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
)
from acquisition.runtime import build_acquisition_runtime
from acquisition.service import AcquisitionService

__all__ = [
    "AcquisitionRequest",
    "AcquisitionService",
    "ParsedRecord",
    "RawArtifact",
    "SourceDescriptor",
    "build_acquisition_runtime",
]
