"""Stage 36 canonical mapping service: NormalizationResult -> CanonicalMappingResult.

Pure transformation over saved Stage 35 results: no network, no discovery, no extraction.
"""

from __future__ import annotations

from core.canonical_mapping import CanonicalMappingResult, map_result
from core.category import CategoryResult
from core.normalization import NormalizationResult


class CanonicalMappingService:
    def map(self, result: NormalizationResult, category: CategoryResult | None = None) -> CanonicalMappingResult:
        return map_result(result, category)
