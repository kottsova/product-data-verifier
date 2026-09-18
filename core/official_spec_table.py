"""Stage 31.5/32: model-column-bound spec-table extraction.

Official spec pages often show several models side by side ("Pixel 9 Pro" and
"Pixel 9 Pro XL") in one accessible comparison table: ``th scope=col`` names
the model columns, ``th scope=colgroup`` names a section ("Display",
"Battery and Charging", ...), and each ``td`` states that section for one
model. The generic label/value extractors cannot bind a value to a column, so
on such a page they emit misaligned junk.

This module:

1. identifies the model columns from the header context;
2. selects the column whose header *is* the requested model (an exact match --
   "Pixel 9 Pro XL" is never the "Pixel 9 Pro" column; no exact column = no
   facts; unlabeled columns contribute only values they all agree on);
3. hands that one column's sections to ``core.official_atomic``, which
   decomposes every parent section into atomic canonical facts and records a
   ledger outcome for every line (accepted / merged / rejected + reason).

Nothing here searches or fetches, and no brand, domain or product value is
hardcoded. Facts carry a language-neutral ``canonical`` name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from core.extract import RawAttribute
from core.identity import ProductIdentity
from core.official_atomic import (
    Fact,
    Line,
    dedupe_facts,
    read_section,
    section_roles,
)

__all__ = [
    "extract_official_section_specs",
    "select_model_column",
    "section_roles",
]

_DROP_TAGS = ("script", "style", "noscript", "template", "svg")
_BOLD_TAGS = {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"}
METHOD = "html_table"
# Facts that only exist to be composed into one dimension value by mapping.
_COMPONENT_CANONICALS = {"product_height", "product_width", "product_depth"}


def _clean(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.replace("®", "").replace("™", "").replace(" ", " ").strip()


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _clean(value).casefold())


# ---------------------------------------------------------------------------
# Table structure: columns, sections, bound cells
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Table:
    columns: list[str] = field(default_factory=list)  # header text per column
    column_ids: dict[str, int] = field(default_factory=dict)  # th id -> column index
    # (heading, {column index: [lines]}) in document order
    sections: list[tuple[str, dict[int, list[Line]]]] = field(default_factory=list)


def _followed_by_footnote_marker(node: NavigableString) -> bool:
    sibling = node.next_sibling
    if not isinstance(sibling, Tag):
        return False
    if sibling.name == "sup" or sibling.find("sup"):
        return True
    return any(str(a.get("href", "")).startswith(("#footnote", "#fn")) for a in sibling.find_all("a"))


def _cell_lines(cell: Tag, start: int = 0) -> list[Line]:
    """Text nodes of a cell as lines; bold text marks a sub-heading.

    Footnote markers (``<sup>``) are page furniture and are dropped.
    """
    lines: list[Line] = []
    for node in cell.descendants:
        if not isinstance(node, NavigableString) or node.find_parent("sup") is not None:
            continue
        text = _clean(node)
        if _followed_by_footnote_marker(node):
            # inline footnote number glued to a word/unit ("camera12", "30x12"), never to
            # a hyphenated designator such as "Sub-6"
            text = re.sub(r"(?<=[A-Za-z)\]x\"%.])\d{1,2}$", "", text).strip()
        if not text:
            continue
        bold = False
        parent = node.parent
        while parent is not None and parent is not cell:
            if parent.name in _BOLD_TAGS:
                bold = True
                break
            parent = parent.parent
        lines.append(Line(text, bold, start + len(lines)))
    return lines


def _is_section_row(row: Tag) -> bool:
    ths = row.find_all("th", recursive=False)
    if not ths or row.find("td"):
        return False
    return any(th.get("scope") in {"colgroup", "rowgroup"} for th in ths) or len(ths) == 1


def _parse_table(table: Tag) -> _Table | None:
    parsed = _Table()
    current: dict[int, list[Line]] | None = None
    counter = 0
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        col_headers = [c for c in cells if c.name == "th" and c.get("scope") == "col"]
        if col_headers and not row.find("td"):
            if not parsed.columns:
                for index, header in enumerate(col_headers):
                    parsed.columns.append(_clean(header.get_text(" ", strip=True)))
                    if header.get("id"):
                        parsed.column_ids[str(header["id"])] = index
            continue
        if _is_section_row(row):
            heading = _clean(cells[0].get_text(" ", strip=True))
            current = {}
            parsed.sections.append((heading, current))
            continue
        if current is None:
            continue
        data = [c for c in cells if c.name == "td"]
        for position, cell in enumerate(data):
            index = position
            headers = cell.get("headers") or []
            if isinstance(headers, str):
                headers = headers.split()
            for header_id in headers:
                if header_id in parsed.column_ids:
                    index = parsed.column_ids[header_id]
                    break
            lines = _cell_lines(cell, counter)
            counter += len(lines)
            if lines:
                current.setdefault(index, []).extend(lines)
    if not parsed.columns or not parsed.sections:
        return None
    return parsed


def _model_names(identity: ProductIdentity) -> list[str]:
    names = [identity.base_model, identity.commercial_model]
    return [name for name in dict.fromkeys(filter(None, names))]


def _column_matches(header: str, identity: ProductIdentity) -> bool:
    """Exact: the header is the requested model, optionally with the brand."""
    header_tokens = _tokens(header)
    brand_tokens = _tokens(identity.brand or "")
    stripped = header_tokens
    if brand_tokens and header_tokens[:len(brand_tokens)] == brand_tokens:
        stripped = header_tokens[len(brand_tokens):]
    return any(
        _tokens(name) in (header_tokens, stripped) for name in _model_names(identity)
    )


def select_model_column(columns: list[str], identity: ProductIdentity) -> int | None:
    """Index of the column that *is* the requested model, or None.

    A sibling header that merely extends the requested model ("... XL") is not
    a match; several columns need an exact header.
    """
    exact = [index for index, header in enumerate(columns) if _column_matches(header, identity)]
    if len(exact) == 1:
        return exact[0]
    return None


def _table_facts(table: _Table, index: int) -> tuple[list[Fact], list[dict[str, object]]]:
    facts: list[Fact] = []
    ledger: list[dict[str, object]] = []
    for heading, by_column in table.sections:
        lines = by_column.get(index)
        if not lines:
            continue
        section_facts, section_ledger = read_section(heading, lines)
        facts.extend(section_facts)
        ledger.extend(section_ledger)
    kept, duplicates = dedupe_facts(facts)
    for duplicate in duplicates:
        for entry in ledger:
            if duplicate.canonical in entry.get("attributes", ()) and entry["outcome"] == "accepted" \
                    and entry["section"] == duplicate.section:
                entry.setdefault("merged_into_earlier", []).append(duplicate.canonical)
    return kept, ledger


def _shared_facts(table: _Table) -> tuple[list[Fact], list[dict[str, object]]]:
    """Facts on which *every* column agrees, for tables with no model naming.

    Used only when the columns carry no model identity at all: a value is
    model-independent exactly when all columns state the same thing.
    """
    per_column = [_table_facts(table, index) for index in range(len(table.columns))]
    if not per_column or not all(facts for facts, _ in per_column):
        return [], []
    first = {(f.canonical, f.value, f.unit) for f in per_column[0][0]}
    common = first.intersection(*({(f.canonical, f.value, f.unit) for f in facts} for facts, _ in per_column[1:]))
    facts = [f for f in per_column[0][0] if (f.canonical, f.value, f.unit) in common]
    ledger = per_column[0][1]
    return facts, ledger


def _model_like(header: str, identity: ProductIdentity) -> bool:
    """Whether a column header names some product (any word of the model)."""
    header_tokens = set(_tokens(header))
    model_tokens = {t for name in _model_names(identity) for t in _tokens(name)}
    return bool(header_tokens & model_tokens)


def extract_official_section_specs(
    source: Mapping[str, object],
    identity: ProductIdentity,
) -> tuple[list[RawAttribute], dict[str, object]]:
    """Model-column-bound, atomic canonical facts from an official spec table.

    Returns ``(attributes, diagnostics)``. ``diagnostics`` records the tables,
    columns, section roles, atomic facts and the per-line ledger, so a missing
    field can be traced to discovery, column binding, section meaning or value
    shape -- never a silent drop.
    """
    diagnostics: dict[str, object] = {
        "tables": 0, "columns": [], "selected_column": None,
        "sections": [], "facts": [], "ledger": [], "outcome": "not_applicable",
    }
    from core.official_source import is_official  # local: avoids an import cycle

    if source.get("status") != "success" or not is_official(source):
        return [], diagnostics
    if source.get("model_relevance") != "exact_base_model":
        return [], diagnostics
    if source.get("identity_relation") not in {"same_base_model", "exact_variant"}:
        return [], diagnostics
    html = str(source.get("html") or "")
    if not html:
        return [], diagnostics
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()

    url = str(source.get("final_url") or source.get("source_url") or "")
    facts: list[Fact] = []
    ledger: list[dict[str, object]] = []
    selected: str | None = None
    for table in soup.find_all("table"):
        parsed = _parse_table(table)
        if parsed is None:
            continue
        diagnostics["tables"] = int(diagnostics["tables"]) + 1  # type: ignore[arg-type]
        diagnostics["columns"] = list(parsed.columns)
        diagnostics["sections"] = [
            {"heading": heading, "roles": sorted(section_roles(heading)),
             "columns_with_content": sorted(by_column)}
            for heading, by_column in parsed.sections
        ]
        index = select_model_column(parsed.columns, identity)
        if index is not None:
            selected = parsed.columns[index]
            facts, ledger = _table_facts(parsed, index)
        elif len(parsed.columns) == 1 or not any(
            _model_like(header, identity) for header in parsed.columns
        ):
            selected = "shared" if len(parsed.columns) > 1 else parsed.columns[0]
            facts, ledger = (
                _table_facts(parsed, 0) if len(parsed.columns) == 1 else _shared_facts(parsed)
            )
        else:
            diagnostics["outcome"] = "no_exact_model_column"
            continue
        if facts:
            break
    diagnostics["selected_column"] = selected
    diagnostics["facts"] = [
        {"canonical": f.canonical, "label": f.label, "value": f.value, "unit": f.unit, "section": f.section}
        for f in facts
    ]
    diagnostics["ledger"] = ledger
    if facts:
        diagnostics["outcome"] = "extracted"
    elif diagnostics["outcome"] == "not_applicable" and diagnostics["tables"]:
        diagnostics["outcome"] = "no_canonical_facts"

    attributes = [
        RawAttribute(
            name=fact.label,
            value=fact.value,
            unit=fact.unit,
            source_url=url,
            source_type=str(source.get("source_type") or "manufacturer"),
            evidence=f"Official spec table, column {selected!r}, {fact.section}: {fact.snippet!r}."[:300],
            extraction_method=METHOD,
            confidence="high",
            raw_value=f"{fact.value} {fact.unit}" if fact.unit else fact.value,
            attribute_kind="product",
            context=f"official spec table for {selected}",
            model_wide=True,
            canonical=None if fact.canonical in _COMPONENT_CANONICALS else fact.canonical,
        )
        for fact in facts
    ]
    return attributes, diagnostics


# ---------------------------------------------------------------------------
# Completeness: official atomic facts vs the final profile
# ---------------------------------------------------------------------------

_OFFICIAL_TYPES = {"manufacturer", "official_document"}


def completeness_diff(
    facts: list[Mapping[str, object]],
    profile_by_name: Mapping[str, object],
) -> list[dict[str, object]]:
    """Outcome of every official atomic fact in the final profile.

    ``accepted``: the profile holds the attribute as Confirmed with official
    support. ``rejected``: it does not, with the concrete reason (missing from
    the profile, unresolved/conflicting, or outranked). There is no third,
    silent state; extraction-time duplicates/merges are in the ledger.
    """
    result: list[dict[str, object]] = []
    for fact in facts:
        canonical = str(fact["canonical"])
        target = "product_dimensions" if canonical in _COMPONENT_CANONICALS else canonical
        attribute = profile_by_name.get(target)
        entry: dict[str, object] = {
            "canonical": target, "label": fact.get("label"), "value": fact.get("value"),
            "unit": fact.get("unit"), "section": fact.get("section"),
        }
        if attribute is None:
            entry.update(outcome="rejected", reason="not present in the final profile (mapping/schema dropped it)")
        elif getattr(attribute, "status", None) != "Confirmed":
            entry.update(
                outcome="rejected",
                reason=f"{getattr(attribute, 'status', '?')}: {getattr(attribute, 'resolution_reason', '')}",
            )
        else:
            supported = any(
                getattr(item, "source_type", None) in _OFFICIAL_TYPES
                for item in getattr(attribute, "supporting_sources", ())
            )
            if supported:
                entry.update(outcome="accepted", reason="")
            else:
                entry.update(outcome="rejected", reason="outranked: confirmed value comes from a non-official source")
        result.append(entry)
    return result
