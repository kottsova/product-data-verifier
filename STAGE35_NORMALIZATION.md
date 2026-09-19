# Stage 35 — Normalization of raw attributes

**Verdict: PASS** (acceptance gate below). Only a new layer was added; discovery, extraction and sources are untouched.

## What was built

| File | Role |
| --- | --- |
| `core/normalization.py` | pure functions: text/value/unit/label normalization, classifier, dedup, provenance |
| `services/normalization.py` | `NormalizationService.normalize(RawExtractionResult)`; `RawExtractionResult` is an alias of Stage 34 `ExtractionResult` |
| `diagnostics/stage35_normalization.py` | offline benchmark over the 8 saved Stage 34 extractions, every socket call patched to raise |
| `tests/test_stage35_normalization.py` | 25 tests (values, labels, classes, dedup, variants, provenance, 8 saved products) |

Input: `diagnostics/results/stage34/*-extraction.json` (Stage 34 output). No fetch, no discovery, no search
(`network_calls = 0`; the harness runs under `network_forbidden()`). Output traces: `diagnostics/results/stage35/`.

## Rules (all generic, no product/site lists)

* **Text**: HTML entities, literal `\uXXXX`, mojibake (`IntelÂ® Evoâ„¢` → `Intel® Evo™`), Unicode spaces/dashes/quotes, repeated punctuation, `en: ` locale prefixes in lists.
* **Values**: `50-60 HzHz` → `50–60 Hz`; `2000-rpm` → `2000 rpm`; `0-2,000rpm` → `0–2000 rpm`; `12,6 мб` → `12.6 МБ`; `149x330x305` → `149 × 330 × 305`; `3.32 in (84.3 mm)` keeps both measures; `1.5Ah 2Ah` → `1.5 Ah 2 Ah`.
  yes/no/true/false/да/нет/ja/nein/oui… → boolean; `specifications.translatedBoolean.no` → `false`; unresolved i18n keys are flagged, not guessed. Identifiers such as `910-007500` are never treated as ranges. Values are not translated.
* **Placeholders**: empty, `-`, `N/A`, `Nicht zutreffend`… → `unknown/placeholder_value`; lorem-ipsum blocks → `template_text` and the rest of that section → `template_block`.
* **Labels**: bullets, colons, footnote `*`, UPPER_SNAKE/camelCase keys, unit in label (`Power (W)`, `Длина, см`) moved to `unit` when the value is measurable (else label kept as written). `Drilling capacity > - Steel` → label `Steel`, qualified `Drilling capacity > Steel`, group path kept.
* **Classes**: `identity_metadata` (JSON-LD scalars, id/brand/colour vocabularies, value naming the model), `product_spec`, `marketing_content` (prose, download rows, offers/recycling sections), `unknown` (placeholders, standards lists, unstructured pairs, headings).
* **Dedup** (values that differ are never merged): same qualified label + same value + compatible unit (a missing unit joins the single known unit); plus *value-order alignment* of streams — sibling locales of one page and JSON-LD vs JSON state of one page — blocks of ≥4 identical values with ≥2 distinct values. Every raw record stays in `evidence[]`; different labels become `label_aliases`.
* **Variants**: JSON-LD nodes are split into `product | variant | parent`; variant identity (sku/mpn/colour/name) is kept as `identity_metadata` with `variant=<Black|Graphite>`, never as spec.
* **Provenance**: each attribute has label, value, unit, class, `evidence[]` (full raw record + fixes), source URLs/types, locations (incl. `seen_in`), methods. Each raw record appears in exactly one attribute (tested).

## Results (8/8)

| Product | Raw | Product specs | Metadata | Marketing/other | Normalized unique specs | Dedup ratio | Issues |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Gressel GAF-1825 | 39 | 33 | 2 | 4 | 32 | 3% | - |
| Bosch HBG7741B1 | 355 | 301 | 14 | 40 | 107 | 64% | 15 placeholder/template |
| DEWALT DCD796P2 | 23 | 18 | 5 | 0 | 18 | 0% | - |
| Philips Sonicare 9900 Prestige HX9992/12 | 107 | 77 | 12 | 18 | 76 | 1% | 3 multi-valued spec labels |
| Makita DHP484Z | 15 | 15 | 0 | 0 | 15 | 0% | - |
| DeLonghi EC685M | 42 | 32 | 5 | 5 | 31 | 3% | - |
| Logitech MX Master 3S | 36 | 23 | 11 | 2 | 23 | 0% | 4 multi-valued spec labels; 2 variant nodes (kept apart) |
| The Ordinary Niacinamide 10% + Zinc 1% | 32 | 9 | 8 | 15 | 9 | 0% | 4 placeholder/template |

Product specs / Metadata / Marketing-other count raw records by the class of the attribute they ended in (they sum to Raw).
Dedup ratio = 1 − unique specs / raw specs.

**Explaining the counts.** Bosch is the only large collapse: 3 sibling-locale pages (de/fr/it) × 2 structures (JSON-LD, JSON state) ≈ 6× per attribute
(evidence per attribute: 6×17, 3×24, 2×48, 1×14, plus a few 4–5 → 107 unique specs from 301 raw). Every other product has one source language and one structure, so 0–3% is expected
(Gressel/DeLonghi: the same pair seen in DOM and JSON). Multi-valued labels are counted only *inside one source*: Logitech = mouse vs USB receiver
dimensions (same label, different real values, deliberately not merged); Philips = `csChapter` benefit blurbs reusing a word (`Gum health`).

## raw → normalized (20 of the run; full list in `diagnostics/results/stage35/examples.json`)

| Site | Raw | Normalized |
| --- | --- | --- |
| delonghi.com | `Jmenovitá frekvence` = `50-60 HzHz` | `50–60 Hz` |
| delonghi.com | `Maximální výška šálku (mm)` = `120` | `Maximální výška šálku` = `120 mm` |
| gressel | `Длина сетевого шнура, см` = `70` | `Длина сетевого шнура` = `70 см` |
| gressel | `Тип` = `да` | `Тип` = `true` |
| gressel | `Книга рецептов` = `12,6 мб` | marketing_content (`download_entry`) |
| bosch-home.com | `HOBS_CONTROL_INTEGRATED` = `specifications.translatedBoolean.no` | `false` |
| bosch-home.com | `Verfügbarer Temperaturbereich` / `Niveau de température possible` / `Gamma temperature` `30-300 °C` | one attribute `30–300 °C`, 6 evidence, 2 aliases |
| bosch-home.com | `Bedienkonzept` (JSON-LD) + `DISPLAY_CONTROL` (JSON state) | one attribute, both kept |
| bosch-home.com | `Sicherheitsvorrichtungen` = `,` | unknown (`placeholder_value`) |
| bosch-home.com | `HOMECONNECT_TYPE` = `en: iService Remote, en: Remote Monitoring…` | `iService Remote, Remote Monitoring…` |
| dewalt.com | `Maximum Speed` = `2000-rpm` | `2000 rpm` |
| dewalt.com | `Barcode` = `5035050000000` / `sku` = `DCD796P2-GB` | identity_metadata |
| makita | `- Steel` = `13mm` (group `Drilling capacity`) | `Drilling capacity > Steel` = `13 mm` |
| makita | `- Hi` = `0-2,000rpm` | `No load speed > Hi` = `0–2000 rpm` (a separate `Impacts per minute > Hi` is not merged) |
| makita | `LXT battery compatibility` = `1.5Ah 2Ah 3Ah…` | `1.5 Ah 2 Ah 3 Ah…` |
| logitech.com | `Width` = `3.32 in (84.3 mm)` | `3.32 in`, alternate `84.3 mm` |
| logitech.com | `sku`/`mpn`/`color` of the Black and Graphite nodes | identity_metadata, `variant=Black|Graphite`, roles `parent, variant, variant` |
| logitech.com | `Compatibility` = `…Engineered for IntelÂ® Evoâ„¢` | `…Intel® Evo™` |
| philips.com | `Power supply` = `100-240 V` | `100–240 V` |
| theordinary.com | `Client Name` = `Lorem Ipsum` (+ Email/Phone/Price in the same block) | unknown (`template_text`, `template_block`) |
| theordinary.com | `ph` = `5.00-6.50` | `Highlights > ph` = `5.00–6.50` |

## Known limits (left for later stages)

* Philips `csChapter` mixes real specs with benefit blurbs; short blurbs still classify as `product_spec` (3 labels flagged multi-valued). Needs the schema/category layer.
* Locale alignment needs identical value order; if a locale omits/reorders rows only the matching blocks (≥4) merge. Translated *text* values in different languages stay separate attributes (same label→ alias only when aligned).
* Thousands-vs-decimal comma is a heuristic (`1,250` = 1250; `12,6` = 12.6).
* Colour vocabulary is a small multilingual list; unlisted languages keep colour as spec.
* Document rows (Gressel manual `дом` = `65, каб. 519.`, Philips declaration lists) contain non-spec lines; standards are marked `unknown`, addresses are not detected.
* Bosch DoC/Philips 2021 manual identity issues from Stage 34 are unchanged (input is the accepted Stage 34 trace).

## Acceptance gate

| Criterion | Result |
| --- | --- |
| 8/8 processed | yes |
| no network/discovery | yes (sockets patched to raise; module imports checked in tests) |
| JSON-LD service fields separated | yes (`identity_metadata`) |
| duplicated units / i18n booleans fixed | yes (`HzHz`, `translatedBoolean.*`: 0 left in specs — tested) |
| obvious duplicates collapsed | yes (Bosch 301 → 107; DOM/JSON pairs elsewhere) |
| provenance kept | yes (each raw record in exactly one `evidence[]`, tested) |
| variants not in specs | yes (Logitech, plus unit test) |
| counts explainable | yes (see above) |
| no product-specific hardcode | yes |
| tests | 1057 OK (25 new) |
