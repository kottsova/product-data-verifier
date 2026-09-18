"""Deterministic JSON and CSV exports for the Stage 7 profile contract."""

from __future__ import annotations

import csv
from io import StringIO
import json
from typing import Iterable, Mapping

from core.mapping import DimensionValue
from core.profile import (
    EvidenceRecord,
    FinalProductProfile,
    ProfileAttribute,
    RawEvidenceRecord,
    validate_final_profile,
)


CSV_COLUMNS = (
    "Attribute",
    "Value",
    "Status",
    "Source",
    "Evidence",
    "DisplayName",
    "Confidence",
    "Authority",
    "IdentityRelation",
    "ResolutionReason",
    "Expected",
    "Priority",
    "Scope",
    "ValueType",
    "UnitFamily",
    "EvidenceRole",
    "SourceType",
)


def _value_to_data(value: str | DimensionValue | None) -> object:
    if isinstance(value, DimensionValue):
        return {
            "height": value.height,
            "width": value.width,
            "depth": value.depth,
            "unit": value.unit,
        }
    return value


def _value_to_text(value: str | DimensionValue | None, unit: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, DimensionValue):
        return f"{value.height} × {value.width} × {value.depth} {value.unit}".strip()
    text = str(value)
    if unit and not text.casefold().rstrip().endswith(unit.casefold()):
        return f"{text} {unit}"
    return text


def _raw_evidence_to_dict(record: RawEvidenceRecord) -> dict[str, object]:
    return {
        "name": record.name,
        "value": record.value,
        "unit": record.unit,
        "raw_value": record.raw_value,
        "source": record.source,
        "source_type": record.source_type,
        "evidence": record.evidence,
        "extraction_method": record.extraction_method,
        "confidence": record.confidence,
        "attribute_kind": record.attribute_kind,
        "context": record.context,
    }


def _evidence_to_dict(record: EvidenceRecord) -> dict[str, object]:
    return {
        "canonical_name": record.canonical_name,
        "value": _value_to_data(record.value),
        "unit": record.unit,
        "source": record.source,
        "source_type": record.source_type,
        "evidence": record.evidence,
        "authority_status": record.authority_status,
        "identity_relation": record.identity_relation,
        "confidence": record.confidence,
        "origin": record.origin,
        "raw_label": record.raw_label,
        "raw_value": record.raw_value,
        "mapping_reason": record.mapping_reason,
        "derived": record.derived,
        "contributors": [
            _raw_evidence_to_dict(item) for item in record.contributors
        ],
    }


def _attribute_to_dict(attribute: ProfileAttribute) -> dict[str, object]:
    return {
        "canonical_name": attribute.canonical_name,
        "display_name": attribute.display_name,
        "value": _value_to_data(attribute.value),
        "unit": attribute.unit,
        "status": attribute.status,
        "confidence": attribute.confidence,
        "source": attribute.source,
        "evidence": attribute.evidence,
        "authority_status": attribute.authority_status,
        "identity_relation": attribute.identity_relation,
        "resolution_reason": attribute.resolution_reason,
        "supporting_sources": [
            _evidence_to_dict(item) for item in attribute.supporting_sources
        ],
        "conflicting_values": [
            _evidence_to_dict(item) for item in attribute.conflicting_values
        ],
        "expected": attribute.expected,
        "priority": attribute.priority,
        "attribute_scope": attribute.attribute_scope,
        "value_type": attribute.value_type,
        "unit_family": attribute.unit_family,
        "discovered": attribute.discovered,
    }


def profile_to_dict(profile: FinalProductProfile) -> dict[str, object]:
    """Return the canonical machine export used by every serializer."""
    validate_final_profile(profile)
    identity = profile.identity
    return {
        "identity": {
            "brand": identity.brand,
            "raw_name": identity.raw_name,
            "base_model": identity.base_model,
            "commercial_model": identity.commercial_model,
            "manufacturer_article": identity.manufacturer_article,
            "product_code": identity.product_code,
            "sku": identity.sku,
            "gtin": identity.gtin,
            "color": identity.color,
            "configuration": {
                key: identity.configuration[key] for key in sorted(identity.configuration)
            },
            "variant_suffix": identity.variant_suffix,
            "market_hint": identity.market_hint,
            "market_scope": identity.market_scope,
            "confidence": identity.confidence,
            "evidence": list(identity.evidence),
            "candidate_identifiers": list(identity.candidate_identifiers),
        },
        "category": {
            "category_id": profile.category.category_id,
            "category_name": profile.category.category_name,
            "parent_category": profile.category.parent_category,
            "confidence": profile.category.confidence,
            "evidence": list(profile.category.evidence),
            "source": profile.category.source,
        },
        "attributes": [_attribute_to_dict(item) for item in profile.attributes],
        "unresolved": [item.canonical_name for item in profile.unresolved],
        "conflicts": [item.canonical_name for item in profile.conflicts],
        "discovered": [item.canonical_name for item in profile.discovered],
        "metadata": dict(profile.metadata),
        "diagnostics": [
            {
                "code": item.code,
                "canonical_name": item.canonical_name,
                "message": item.message,
                "facts": [_evidence_to_dict(fact) for fact in item.facts],
            }
            for item in profile.diagnostics
        ],
    }


def export_profile_json(profile: FinalProductProfile, *, pretty: bool = False) -> str:
    """Serialize the canonical profile dict without ASCII escaping."""
    data = profile_to_dict(profile)
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _row(
    attribute: ProfileAttribute,
    *,
    value: str | DimensionValue | None,
    unit: str | None,
    source: str | None,
    evidence: str | None,
    source_type: str,
    authority: str,
    identity_relation: str,
    role: str,
) -> dict[str, str]:
    return {
        "Attribute": attribute.canonical_name,
        "Value": _value_to_text(value, unit),
        "Status": attribute.status,
        "Source": source or "",
        "Evidence": evidence or "",
        "DisplayName": attribute.display_name,
        "Confidence": attribute.confidence,
        "Authority": authority,
        "IdentityRelation": identity_relation,
        "ResolutionReason": attribute.resolution_reason,
        "Expected": "true" if attribute.expected else "false",
        "Priority": attribute.priority,
        "Scope": attribute.attribute_scope,
        "ValueType": attribute.value_type,
        "UnitFamily": attribute.unit_family or "",
        "EvidenceRole": role,
        "SourceType": source_type,
    }


def _evidence_rows(
    attribute: ProfileAttribute,
    records: Iterable[tuple[EvidenceRecord, str]],
) -> list[dict[str, str]]:
    return [
        _row(
            attribute,
            value=record.value,
            unit=record.unit,
            source=record.source,
            evidence=record.evidence,
            source_type=record.source_type,
            authority=record.authority_status,
            identity_relation=record.identity_relation,
            role=role,
        )
        for record, role in records
    ]


def profile_rows(profile: FinalProductProfile) -> list[dict[str, str]]:
    """Flatten provenance as one row per evidence item, never one row per field value."""
    validate_final_profile(profile)
    rows: list[dict[str, str]] = []
    for attribute in profile.attributes:
        if attribute.status == "Conflict":
            rows.extend(_evidence_rows(attribute, (
                *((item, "supporting_candidate") for item in attribute.supporting_sources),
                *((item, "conflicting_candidate") for item in attribute.conflicting_values),
            )))
        elif attribute.supporting_sources:
            role = "confirmed_support" if attribute.status == "Confirmed" else "unresolved_evidence"
            rows.extend(_evidence_rows(
                attribute,
                ((item, role) for item in attribute.supporting_sources),
            ))
        else:
            rows.append(_row(
                attribute,
                value=attribute.value,
                unit=attribute.unit,
                source=attribute.source,
                evidence=attribute.evidence,
                source_type="",
                authority=attribute.authority_status,
                identity_relation=attribute.identity_relation,
                role="unresolved_empty" if attribute.status == "Unresolved" else "primary",
            ))
    return rows


def export_profile_csv(profile: FinalProductProfile) -> str:
    """Serialize deterministic UTF-8-compatible rows using the universal five columns."""
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(profile_rows(profile))
    return output.getvalue()


# ---------------------------------------------------------------------------
# Stage 30: /export CSV for the stable service boundary (VerifyProductResult).
#
# Deliberately independent of FinalProductProfile/ProfileAttribute: rows are
# plain dicts so this stays safe to call from the bot layer, which must never
# import core.profile/core.workflow/core.quality (see
# tests.test_bot.ArchitectureBoundaryTests). bot/export.py builds these dicts
# from services.product_verifier's ServiceAttribute/ServiceEvidence.
# ---------------------------------------------------------------------------

CSV_RESULT_COLUMNS = (
    "Brand",
    "Model",
    "Category",
    "Attribute",
    "DisplayName",
    "Value",
    "Status",
    "Source",
    "Evidence",
    "Confidence",
    "Authority",
    "SourceType",
)


def _result_row(brand: str, model: str, category: str, attribute: Mapping[str, object]) -> dict[str, str]:
    return {
        "Brand": brand,
        "Model": model,
        "Category": category,
        "Attribute": str(attribute.get("canonical_name", "")),
        "DisplayName": str(attribute.get("display_name", "")),
        "Value": str(attribute.get("value_text", "")),
        "Status": str(attribute.get("status", "")),
        "Source": str(attribute.get("source") or ""),
        "Evidence": str(attribute.get("evidence") or ""),
        "Confidence": str(attribute.get("confidence", "")),
        "Authority": str(attribute.get("authority_status", "")),
        "SourceType": str(attribute.get("source_type", "")),
    }


def export_result_csv(
    *, brand: str, model: str, category: str, rows: Iterable[Mapping[str, object]],
) -> str:
    """Deterministic CSV over already-flattened, plain-dict attribute rows."""
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_RESULT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_result_row(brand, model, category, row))
    return output.getvalue()
