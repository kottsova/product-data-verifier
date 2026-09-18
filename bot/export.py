"""Stage 30/31.1: deterministic CSV for the Telegram ``/export`` command.

Built directly from the stable services.product_verifier DTOs
(ServiceAttribute/ServiceEvidence) -- never from FinalProductProfile -- so
this module respects the same bot-layer boundary as bot/formatters.py and
bot/jobs.py (see tests.test_bot.ArchitectureBoundaryTests): the bot layer
must depend only on services.product_verifier, never on core.profile/
core.workflow/core.quality.

Stage 31.1: the primary ``/export`` CSV is now *wide* -- one row per
product, one column per canonical attribute -- matching what the Telegram
message already shows (bot.attribute_filter's allowlist + usable-value
guard), not a long per-evidence-item dump. The old long/evidence-per-row
shape (``build_export_rows``) is kept as an internal helper for a future
multi-product "Sources" sheet (see STAGE31_1_XLSX_CONTRACT.md) and any
caller that still wants full per-evidence provenance rows.
"""

from __future__ import annotations

import csv
from io import StringIO

from bot.attribute_filter import filter_user_facing, has_usable_value
from bot.formatters import _OFFICIAL_SOURCE_TYPES, format_attribute_value
from bot.i18n import DEFAULT_LANGUAGE, Language, export_header, ui
from bot.i18n import display_name as localized_display_name
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
    """Long format: one row per evidence item. Internal/future-sheet use only
    -- see the module docstring; ``/export`` itself uses the wide format."""
    rows: list[dict[str, object]] = []
    for attribute in filter_user_facing(result.attributes):
        rows.extend(_attribute_rows(attribute, language))
    return rows


def _identity_fields(result: VerifyProductResult) -> tuple[str, str, str, str]:
    identity = result.identity
    brand = (identity.brand if identity else result.request.brand) or ""
    model = (
        ((identity.commercial_model or identity.base_model) if identity else None)
        or result.request.model
        or ""
    )
    article = (identity.manufacturer_article if identity else None) or result.request.article or ""
    category = result.category.category_name if result.category else ""
    return brand, model, article, category


def _source_summary(attributes: list[ServiceAttribute]) -> tuple[str, str]:
    official: list[str] = []
    secondary: list[str] = []
    for attribute in attributes:
        for evidence in attribute.supporting_sources:
            if not evidence.source:
                continue
            bucket = official if evidence.source_type in _OFFICIAL_SOURCE_TYPES else secondary
            if evidence.source not in bucket:
                bucket.append(evidence.source)
    return "; ".join(official), "; ".join(secondary)


def build_wide_export_row(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> dict[str, str]:
    """One product = one row; one canonical attribute = one column.

    Every canonical attribute the pipeline produced for this result (already
    narrowed to the category schema by bot.attribute_filter.filter_user_facing
    -- extraction leftovers never become columns) gets exactly one cell:
    its confirmed value, or the localized "not found" placeholder when it's
    Unresolved/Conflicted or its value failed the usability guard
    (bot.attribute_filter.has_usable_value). Column order follows the
    result's own attribute order (core.profile sorts expected-first, then by
    schema priority), so it stays stable across repeat verifications of the
    same category.
    """
    brand, model, article, category = _identity_fields(result)
    row: dict[str, str] = {
        export_header("brand", language): brand,
        export_header("model", language): model,
        export_header("article", language): article,
        export_header("category", language): category,
    }
    not_found = ui("not_found", language)
    attributes = filter_user_facing(result.attributes)
    for attribute in attributes:
        header = localized_display_name(
            attribute.canonical_name, language, fallback=attribute.display_name,
        )
        if attribute.status == "Confirmed" and has_usable_value(attribute):
            row[header] = format_attribute_value(attribute.value, attribute.unit)
        else:
            row[header] = not_found
    official, secondary = _source_summary(attributes)
    row[export_header("official_sources", language)] = official
    row[export_header("secondary_sources", language)] = secondary
    return row


def export_result_csv(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> str:
    """The wide, one-row-per-product CSV sent by ``/export``."""
    row = build_wide_export_row(result, language=language)
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(row.keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()
