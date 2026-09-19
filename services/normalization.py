"""Stage 35 normalisation service: RawExtractionResult -> NormalizationResult.

Pure transformation.  It reads the raw records of an already produced extraction and never
touches the network, discovery or the sources themselves.
"""

from __future__ import annotations

from core.normalization import CLASSES, NormalizationResult, normalize_attributes
from services.raw_extraction import RawExtractionResult


class NormalizationService:
    def normalize(self, extraction: RawExtractionResult) -> NormalizationResult:
        attributes, nodes, fixes = normalize_attributes(extraction.attributes, extraction.model)
        raw_by_class = {cls: 0 for cls in CLASSES}
        for attribute in attributes:
            raw_by_class[attribute.cls] += len(attribute.evidence)
        return NormalizationResult(
            product_name=extraction.product_name, brand=extraction.brand, model=extraction.model,
            attributes=attributes, identity_nodes=nodes, raw_count=len(extraction.attributes),
            raw_by_class=raw_by_class, fixes=fixes,
        )
