"""Excel / Data Intelligence — tabular data processing layer."""

from data_intel.contracts import (
    ColumnDescriptor,
    DataIssue,
    DataTransformation,
    DatasetDescriptor,
    MatchResult,
    TableDescriptor,
)
from data_intel.errors import DataIntelError
from data_intel.service import DataIntelligenceService

__all__ = [
    "ColumnDescriptor",
    "DataIssue",
    "DataIntelligenceService",
    "DataIntelError",
    "DataTransformation",
    "DatasetDescriptor",
    "MatchResult",
    "TableDescriptor",
]
