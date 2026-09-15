"""Stage 9 deterministic quality gate over a finalized product profile.

This module reads a :class:`~core.profile.FinalProductProfile` and aggregates
an explainable ``verified`` / ``partial`` / ``insufficient`` / ``conflicted``
verdict. It is read-only with respect to facts: it never re-validates an
individual attribute, never resolves a conflict, never repairs a mapping, and
never changes a Stage 6 status. "Critical" attributes are not hardcoded here;
they are whichever schema fields the category's own schema already marks with
``priority="critical"`` (see core/schema.py), so adding or changing critical
fields for a category is a schema change, not a change to this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.profile import FinalProductProfile, ProfileAttribute


QualityStatus = Literal["verified", "partial", "insufficient", "conflicted"]
QUALITY_STATUSES = {"verified", "partial", "insufficient", "conflicted"}

_RATIO_FIELDS = (
    "insufficient_max_coverage",
    "insufficient_max_critical_found_ratio",
    "verified_min_coverage",
    "verified_min_critical_confirmed_ratio",
    "verified_min_critical_found_ratio",
    "verified_min_high_authority_ratio",
)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """Centralized, testable policy thresholds. Ratios are 0.0-1.0.

    A profile is never judged on overall coverage alone: the ``critical_*``
    ratios (over the category's own critical schema fields) dominate the
    verified/insufficient decision, so a category with a naturally lower
    achievable coverage can still be verified on strong critical evidence,
    and a category with high coverage but weak critical evidence cannot.
    """

    insufficient_max_coverage: float = 0.25
    insufficient_max_critical_found_ratio: float = 0.34
    verified_min_coverage: float = 0.40
    verified_min_critical_confirmed_ratio: float = 0.75
    verified_min_critical_found_ratio: float = 1.0
    # Stage 6 only ever marks a fact "Confirmed" through strong evidence, but
    # "strong" spans two authority tiers: attribute.confidence == "high" means
    # an official/manufacturer-verified source, "medium" means a specialized
    # reference source that was never independently verified. This ratio is
    # the share of Confirmed critical fields that must come from the "high"
    # (manufacturer-verified) tier for the profile to be verified.
    verified_min_high_authority_ratio: float = 0.5

    def __post_init__(self) -> None:
        for name in _RATIO_FIELDS:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1: {value}")


DEFAULT_THRESHOLDS = QualityThresholds()


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """Serializable, read-only aggregation over an existing final profile."""

    status: QualityStatus
    coverage_percent: float
    schema_total: int
    schema_found: int
    confirmed_count: int
    unresolved_count: int
    conflict_count: int
    critical_total: int
    critical_found: int
    critical_confirmed: int
    critical_conflict: int
    critical_high_authority_confirmed: int
    category_confidence: str
    identity_confidence: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in QUALITY_STATUSES:
            raise ValueError(f"unsupported quality status: {self.status}")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "coverage_percent": self.coverage_percent,
            "schema_total": self.schema_total,
            "schema_found": self.schema_found,
            "confirmed_count": self.confirmed_count,
            "unresolved_count": self.unresolved_count,
            "conflict_count": self.conflict_count,
            "critical_total": self.critical_total,
            "critical_found": self.critical_found,
            "critical_confirmed": self.critical_confirmed,
            "critical_conflict": self.critical_conflict,
            "critical_high_authority_confirmed": self.critical_high_authority_confirmed,
            "category_confidence": self.category_confidence,
            "identity_confidence": self.identity_confidence,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def _has_evidence(attribute: ProfileAttribute) -> bool:
    return bool(attribute.supporting_sources or attribute.conflicting_values)


def _ratio(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def assess_product_quality(
    profile: FinalProductProfile,
    *,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
) -> QualityAssessment:
    """Aggregate Stage 6/7 output into one explainable quality verdict.

    Only ``expected`` schema attributes are scored: discovered/extra fields
    are useful context but were never promised by the category schema, so
    they neither help nor hurt coverage or critical-field ratios.
    """
    expected = [item for item in profile.attributes if item.expected]
    schema_total = len(expected)
    found = [item for item in expected if _has_evidence(item)]
    schema_found = len(found)
    coverage = _ratio(schema_found, schema_total)

    confirmed = [item for item in expected if item.status == "Confirmed"]
    unresolved = [item for item in expected if item.status == "Unresolved"]
    conflicts = [item for item in expected if item.status == "Conflict"]

    critical = [item for item in expected if item.priority == "critical"]
    critical_total = len(critical)
    critical_found_items = [item for item in critical if _has_evidence(item)]
    critical_confirmed_items = [item for item in critical if item.status == "Confirmed"]
    critical_conflict_items = [item for item in critical if item.status == "Conflict"]
    critical_found_ratio = _ratio(len(critical_found_items), critical_total)
    critical_confirmed_ratio = _ratio(len(critical_confirmed_items), critical_total)

    # Source authority / evidence quality: a Confirmed fact's own confidence
    # already encodes which authority tier resolved it (see core/validate.py
    # _resolve_field: "high" only for a verified manufacturer/official
    # source, "medium" for an unverified specialized-reference source). This
    # reads that existing signal rather than re-deriving authority here.
    critical_high_authority_items = [
        item for item in critical_confirmed_items if item.confidence == "high"
    ]
    critical_high_authority_ratio = _ratio(
        len(critical_high_authority_items), len(critical_confirmed_items),
    )

    non_critical_conflicts = [item for item in conflicts if item.priority != "critical"]

    category_unknown = profile.category.category_id == "unknown"
    category_confidence = profile.category.confidence
    identity_confidence = profile.identity.confidence
    by_name = profile.by_name
    identity_names_missing_evidence = [
        name for name in ("brand", "model")
        if name not in by_name or not _has_evidence(by_name[name])
    ]

    reasons: list[str] = []
    warnings: list[str] = []

    if critical_conflict_items:
        status: QualityStatus = "conflicted"
        reasons.append(
            "Critical attribute(s) have conflicting evidence that a useful "
            f"profile cannot ignore: {', '.join(item.canonical_name for item in critical_conflict_items)}."
        )
    elif category_unknown:
        status = "insufficient"
        reasons.append(
            "Category could not be determined, so category-specific critical "
            "fields cannot be evaluated."
        )
    elif identity_names_missing_evidence:
        status = "insufficient"
        reasons.append(
            "Core identity field(s) have no confirming evidence: "
            f"{', '.join(identity_names_missing_evidence)}."
        )
    elif identity_confidence == "low":
        status = "insufficient"
        reasons.append("Product identity was resolved with low confidence.")
    elif (
        coverage < thresholds.insufficient_max_coverage
        or critical_found_ratio < thresholds.insufficient_max_critical_found_ratio
    ):
        status = "insufficient"
        reasons.append(
            f"Coverage ({coverage:.0%}) and/or critical-field discovery "
            f"({critical_found_ratio:.0%} of {critical_total}) fall below the "
            "minimum useful threshold."
        )
    elif (
        coverage >= thresholds.verified_min_coverage
        and critical_confirmed_ratio >= thresholds.verified_min_critical_confirmed_ratio
        and critical_found_ratio >= thresholds.verified_min_critical_found_ratio
        and critical_high_authority_ratio >= thresholds.verified_min_high_authority_ratio
        and category_confidence != "low"
    ):
        status = "verified"
        reasons.append(
            f"{len(critical_confirmed_items)} of {critical_total} critical field(s) are "
            f"Confirmed ({len(critical_high_authority_items)} from manufacturer-verified "
            f"sources) and overall coverage is {coverage:.0%}."
        )
    else:
        status = "partial"
        reasons.append(
            f"Category and identity are resolved, {len(confirmed)} of {schema_total} "
            f"expected field(s) are Confirmed, but coverage ({coverage:.0%}), critical "
            f"confirmation ({critical_confirmed_ratio:.0%} of {critical_total}), or "
            f"evidence authority ({critical_high_authority_ratio:.0%} manufacturer-verified) "
            "has not reached the verified threshold."
        )

    if critical_confirmed_items and critical_high_authority_ratio < 1.0:
        warnings.append(
            f"{len(critical_confirmed_items) - len(critical_high_authority_items)} of "
            f"{len(critical_confirmed_items)} confirmed critical field(s) rely on "
            "specialized-reference evidence rather than a manufacturer-verified source."
        )
    if non_critical_conflicts:
        warnings.append(
            f"{len(non_critical_conflicts)} non-critical attribute(s) have conflicting "
            f"evidence: {', '.join(item.canonical_name for item in non_critical_conflicts)}."
        )
    if unresolved:
        warnings.append(f"{len(unresolved)} expected attribute(s) remain unresolved.")
    if profile.identity.candidate_identifiers:
        warnings.append(
            "Identity carries an unresolved candidate code that was not assigned "
            "model/SKU semantics."
        )
    if not category_unknown and category_confidence == "low":
        warnings.append("Category confidence is low.")

    return QualityAssessment(
        status=status,
        coverage_percent=round(coverage * 100, 1),
        schema_total=schema_total,
        schema_found=schema_found,
        confirmed_count=len(confirmed),
        unresolved_count=len(unresolved),
        conflict_count=len(conflicts),
        critical_total=critical_total,
        critical_found=len(critical_found_items),
        critical_confirmed=len(critical_confirmed_items),
        critical_conflict=len(critical_conflict_items),
        critical_high_authority_confirmed=len(critical_high_authority_items),
        category_confidence=category_confidence,
        identity_confidence=identity_confidence,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )


# Concise alias mirroring the Stage naming convention used elsewhere.
assess_quality = assess_product_quality
