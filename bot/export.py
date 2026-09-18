"""Stage 30: deterministic CSV for the Telegram ``/export`` command.

Built directly from the stable services.product_verifier DTOs
(ServiceAttribute/ServiceEvidence) -- never from FinalProductProfile -- so
this module respects the same bot-layer boundary as bot/formatters.py and
bot/jobs.py (see tests.test_bot.ArchitectureBoundaryTests): the bot layer
must depend only on services.product_verifier, never on core.profile/
core.workflow/core.quality.
"""

from __future__ import annotations

from bot.attribute_filter import filter_user_facing
from bot.formatters import format_attribute_value
from bot.i18n import DEFAULT_LANGUAGE, Language, display_name as localized_display_name
from core.export import export_result_csv as _export_result_csv
from services.product_verifier import ServiceAttribute, VerifyProductResult


def _attribute_rows(attribute: ServiceAttribute, language: Language) -> list[dict[str, object]]:
    value_text = format_attribute_value(attribute.value, attribute.unit)
    provenance = [
        (item.source, item.evidence, item.source_type, item.authority_status, item.confidence)
        for item in (*attribute.supporting_sources, *attribute.conflicting_values)
    ]
    if not provenance:
        provenance = [(attribute.source or "", attribute.evidence or "", "", "", attribute.confidence)]
    return [
        {
            "canonical_name": attribute.canonical_name,
            "display_name": localized_display_name(
                attribute.canonical_name, language, fallback=attribute.display_name,
            ),
            "value_text": value_text,
            "status": attribute.status,
            "source": source,
            "evidence": evidence,
            "source_type": source_type,
            "authority_status": authority_status,
            "confidence": confidence,
        }
        for source, evidence, source_type, authority_status, confidence in provenance
    ]


def build_export_rows(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> list[dict[str, object]]:
    """Flatten every user-facing attribute's provenance into plain, DTO-agnostic rows.

    Extraction noise and semantic duplicates (see bot.attribute_filter) are
    excluded, matching what the Telegram message itself shows -- the export
    is a download of what the user already saw, not the raw internal profile.
    """
    rows: list[dict[str, object]] = []
    for attribute in filter_user_facing(result.attributes):
        rows.extend(_attribute_rows(attribute, language))
    return rows


def export_result_csv(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> str:
    """Deterministic CSV for a completed VerifyProductResult, brand/model/category included."""
    identity = result.identity
    brand = (identity.brand if identity else result.request.brand) or ""
    model = (
        ((identity.commercial_model or identity.base_model) if identity else None)
        or result.request.model
        or ""
    )
    category = result.category.category_name if result.category else ""
    return _export_result_csv(
        brand=brand, model=model, category=category, rows=build_export_rows(result, language=language),
    )
