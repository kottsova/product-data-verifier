# Stage 34 — Raw extraction from `DiscoveryDebugResult`

**Verdict: PASS on the acceptance gate.** 8/8 products run, 8/8 return real raw attributes from official sources,
extraction never searched, hidden DOM / JSON state / documents are read, provenance is kept per attribute.
No discovery code was changed. Secondary/dealer sources are never read.

Answer to the stage question — *can we stably pull raw characteristics from the official sources discovery found?*
**Yes for all 8 products** (3 extraction runs per saved discovery result: identical attribute counts every time).
The open weaknesses are in *discovery selection* (three wrongly/weakly accepted documents, one run-to-run variance),
not in extraction — see "Findings for discovery".

## What was built

| Piece | Path |
| --- | --- |
| Extractors (HTML, JSON-LD, JSON state, templates, PDF text, SKU scoping) | `core/raw_extraction.py` |
| Service: `DiscoveryDebugResult` → `ExtractionResult` (fetch only what discovery selected) | `services/raw_extraction.py` |
| Benchmark harness (`python -m diagnostics.stage34_extraction [--from-saved] [--only NAME]`) | `diagnostics/stage34_extraction.py` |
| Tests (14, offline fixtures) | `tests/test_stage34_raw_extraction.py` |
| Traces: `*-discovery.json` (input) + `*-extraction.json` (output) + `coverage.json` | `diagnostics/results/stage34/` |

**Input.** Only `official_pages`, `support_pages`, `documents` of the result (model_match `exact`/`probable`; `weak` is
skipped and listed in `skipped`). While extraction runs the harness patches every discovery entry point to raise
(`searching_forbidden`), so a passing run proves no re-search; `search_calls` is 0 in every result and a test
asserts the extraction modules do not reference search entry points.

**Readers** (generic, no per-site selectors): tables (incl. multi-model tables → the requested model's column only),
definition lists, label/value element pairs, `Label: value` lines, group headings ("Drilling capacity" → `- Steel`),
`<template>` and script templates, JSON-LD `Product` (`additionalProperty`, scalars), JSON state (whole-script JSON,
`window.X = {...}`, `data-*` JSON, **Next.js `__next_f.push` flight payloads**, **JS object literals with bare keys**, and
"grouped spec key list + record + label dictionary" records).

**Provenance per attribute:** `raw_label`, `raw_value`, `source_url`, `source_type`
(`official_product_page | official_support_page | official_document`), `location`
(`visible_dom | dom_hidden | template | json_ld | json_state | document`), `method`, plus `section`, `document_type`,
`page_ref` (PDF page), `sku_scope`, `seen_in` (other locations carrying the identical pair). No normalisation.

**Neighbouring SKUs.** Counted in `excluded`, never emitted: related/recommended/compare/carousel/store-locator blocks,
JSON product nodes and JSON-LD products whose identifier is a sibling, table columns of other models, and — for
documents — pages that name a sibling identifier (shared pages are excluded, ambiguous pages of multi-model documents too).
A document that **never names the requested model** is withheld entirely (discovery's URL/title match is not enough).
Manuals/instructions contribute only from their technical-data sections (prose `Note: …` is not an attribute).

## Comparison (fresh live discovery → extraction, 2026-09-19)

| Product | Official sources | Raw attributes | Source coverage | Extraction issues |
| --- | --- | --- | --- | --- |
| Gressel GAF-1825 | 1 page + 1 manual | **39** (page 25: dom_hidden 21, visible 4; manual 14) | 2/2 | none. Page specs sit in a hidden tab table (`spec_location=dom_hidden`, no click needed). |
| Bosch HBG7741B1 | 3 pages (ch/de, ch/fr, ch/it) + 1 DoC | **355** (json_ld 169, json_state 186; 121/114/120 per page) | 3/4 | DoC withheld: `HT6B60F0S…` declaration never names HBG7741B1 (46 lines not emitted). |
| DEWALT DCD796P2 | 1 page | **23** (json_ld 23) | 1/1 | DOM accordion shows 5 rows + "See more" (JS); the full list is in JSON-LD, so nothing lost. 3 neighbour pairs excluded. |
| Philips HX9992/12 | 2 pages (uk, de) + 3 docs | **107** (page 42 ×2: json_state 37 + json_ld 5; 2 DoCs 23) | 4/5 | `spec_location=unknown` although specs are in Next.js flight JSON (read). 2011 manual withheld (model not in its text, 217 lines). The 23 doc attributes are conformity standards, not characteristics. |
| Makita DHP484Z | 1 page | **15** (visible table, grouped e.g. `Drilling capacity > - Steel`) | 1/1 | none; 3 neighbour pairs excluded. |
| DeLonghi EC685M | 1 page (cs-cz) | **42** (json_state 33 keyed-spec, json_ld 2, DOM 7) | 1/1 | Discovery returned only the cs-cz page this run (see below). `spec_location=unknown` although specs are in page state. 2 neighbour JSON products excluded. |
| Logitech MX Master 3S | 1 page (en-us shop) | **36** (json_state 23 from JS-literal `inRiverTechSpecs`/`specs`, json_ld 11, DOM 2) | 1/1 | Specs are not JSON but a JS object literal (needed a tolerant reader). `spec_location=unknown`. json_ld includes two colour variants' identifiers of the same product. |
| The Ordinary Niacinamide | 2 pages (.com en-ge, .es) | **32** (.com 26: pH/free-from highlights etc.; .es 6) | 2/2 | Thin on characteristics (product page, not a spec sheet); promo/store-locator tables filtered (12 excluded). |

Stability: 2 extra extraction runs on the same discovery results gave identical counts for all 8
(355 / 42 / 23 / 39 / 36 / 15 / 107 / 32).
Blockers requiring JS interaction: **none demonstrated** — no product needed a click; no page ended with `js_required`.
(DEWALT "See more" and Philips "Show more" exist, but the data was already in JSON-LD / flight state.)

## Findings for discovery (not changed, per the stage rule)

1. **False-positive exact document:** Bosch `21400895_EU_DoC_Bosch_HT6B60F0S.pdf` was accepted as `exact` for HBG7741B1; the PDF
   text contains neither `HBG7741B1` nor `7741`. Extraction withholds it, but discovery should not call it exact.
2. **Weak document:** Philips `…/20211007/43e2…pdf` (generic Prestige manual) never names HX9992/12.
3. **Run-to-run variance:** DeLonghi discovery returned 2 pages + RU manual in the Stage 33.3 run and 1 page (cs-cz) in this run.
   The RU manual is a multi-model manual (`EC685 EC695 EC785`); extraction keeps it out (shared pages) when it is found.
4. `PageInspection.spec_location` said `unknown` for Philips, DeLonghi, Logitech although specs were present in JSON state /
   flight payload — the inspection heuristic does not see these formats (extraction does).

## Caveats

* Raw attributes include identifiers (name/sku/mpn/gtin/colour) from JSON-LD; the characteristic counts above are lower than the
  totals by roughly 5–11 per page. Nothing was normalised or de-duplicated across locales (Bosch ch/de/fr/it repeat the same product).
* PDF extraction is text-layer only (no OCR); `Label: value` and technical-section "label number unit" lines only.
* Values can carry site quirks (e.g. `50-60 HzHz`, i18n keys such as `specifications.translatedBoolean.no`) — kept raw on purpose.
