# Stage 33.1 — stable official pages and official documents

Goal: `name -> official page(s) + official documents + discovery metadata`, with
no extraction. Input of every run is only the product name (no seed URL, no
fixture, no product-specific rule). Traces of all 12 live runs:
`diagnostics/results/stage33_1/<product>-run<N>.json`
(`python -m diagnostics.stage33_1_stability --runs 3`).

## Result (3 independent live runs per product, final code)

| Product | Run 1 | Run 2 | Run 3 | Stable |
| ------- | ----- | ----- | ----- | ------ |
| Gressel GAF-1825 | PASS ✓ (6s) | PASS ✓ (6s) | PASS ✓ (6s) | yes |
| Dreame G12 Pro HHR32A | PARTIAL ✓ (74s) | PARTIAL ✓ (76s) | PARTIAL ✓ (68s) | yes (proven PARTIAL) |
| DEWALT DCD796P2 | PASS ✓ (29s) | PASS ✓ (29s) | PASS ✓ (28s) | yes |
| Philips Sonicare 9900 Prestige HX9992/12 | PASS ✓ (5s) | PASS ✓ (5s) | PASS ✓ (4s) | yes |

`✓` = the run surfaced the official page/document that the maintainers know
exists. That oracle (`EXPECTED` in the harness) only *scores* a finished run; it
is never passed to discovery.

## Why the pages appeared in one run and vanished in the next

1. **Brand-root probing tried only `www.<brand>.com`.** `www.gressel.com`
   geo-redirects to an unrelated Swiss company (`gressel.ch`); the real site is
   `gressel.ru` and has **no `www.` host**. `philips.com` geo-redirects to
   `philips.com.ge`, which the registrable-domain helper mis-parsed as `com.ge`.
   DEWALT's UK sitemap was never reached. Fixed: brand-derived roots now cover
   regional suffixes (`.ru .co.uk .de .eu ...`), both `www.` and apex hosts,
   resolved in parallel with a bounded window; generic `<label>.<com|co|org|..>.<cc>`
   registrable-domain rule.
2. **DuckDuckGo bot-checks `site:` operator queries, and one block opened its
   circuit for the whole request.** Whether the exact page came from DDG
   depended on whether a `site:` query ran first. Fixed: `site:` becomes a plain
   domain keyword for DDG; a `blocked` circuit now half-opens after a cooldown
   (`timeout` still stays open); a browser probe that is empty twice in a row is
   retired for the product.
3. **Canonicalisation dropped the trailing slash.** Gressel answers 404 for
   `.../aerogril_gressel_gaf_1825` and 200 for `.../aerogril_gressel_gaf_1825/`.
   Displayed links now use the spelling that actually loads.
4. **Google failed with "Playwright Sync API inside the asyncio loop"** whenever
   the official-site browser probe had started first: two sync drivers in one
   thread. Providers now share one refcounted driver per thread.
5. **Multi-word models never matched.** Direct site search used the whole
   model string; it now uses the distinctive SKU (`HX9992/12`, `HX9992`, ...).

## Generic changes (no product/brand table anywhere)

* `core/sku.py` — base model / regional suffix / real variant. `/`, `_`, `-` are
  interchangeable (`HX9992/12 == HX9992_12`). `DCD796P2-GB` is the same model
  (`regional_suffix`); `DCD796P2T`, `DCD796D2`, `HX9992_21` (when `/12` is
  requested) are not.
* Direct official probing: multi-domain, apex+www, site search with SKU terms,
  learned `/<prefix>/<SKU>/<slug>` product-path template (Philips), sitemaps,
  page verification (real `<title>`, SKU in page), sibling-region grace window,
  `site:<verified domain>` queries answered by the probe itself. Early stop
  once a first-party page names the exact model.
* Authority: a direct-probe page whose `<title>` omits the brand (DEWALT UK)
  is verified through *fetched-page* brand confirmation, valid only for the
  direct probe on a brand-rooted domain.
* `core/official_documents.py` — classification (manual, user guide,
  instruction, quick start, datasheet, spec sheet, safety, declaration,
  certificate; en/ru/de/fr/es/it), canonical fields (`manual_url`,
  `datasheet_url`, `quick_start_guide_url`, `safety_document_url`,
  `declaration_url`, `certificate_url`), authority rules (files inherit the
  linking official page; HTML pages must be on an official host), model match
  (`exact` / `probable`, sibling-SKU and variant-word rejection), duplicate and
  locale collapsing (a page listing a manual in 50 languages yields one entry),
  JSON-state documents.
* `core/page_inspection.py` — DOM-only diagnosis: `has_expandable_specs`,
  `has_hidden_spec_content`, `requires_interaction` (hidden attr, `display:none`,
  closed `<details>`, `<template>`, inactive tab panes, `aria-expanded`, JSON
  state; JS shell reported `unknown`, never guessed). Nothing is clicked or
  extracted.
* `services/discovery_debug.py` + `bot/discovery_formatters.py` — groups:
  Official product pages / Official support pages / Official documents /
  Secondary / Discovery metadata (+ rejected, provider failures).

## Official URLs and documents (stable across 3 runs unless marked)

**Gressel GAF-1825** — PASS
* page (exact): https://gressel.ru/catalog/aerogril/aerogril_gressel_gaf_1825/
* document (instruction, exact): https://gressel.ru/upload/iblock/ddc/o5357402t7kcm1lsczwd58pflvfmnncp.pdf ("Инструкция GAF-1825")
* metadata: `has_expandable_specs=true` ("Все характеристики", tab target `#props` is in the DOM),
  `has_hidden_spec_content=true` (21 spec rows in an inactive tab pane), `requires_interaction=false`.

**Philips Sonicare 9900 Prestige HX9992/12** — PASS
* pages (exact, suffix `/12`): https://www.philips.co.uk/c-p/HX9992_12/sonicare-9900-prestige-power-toothbrush-with-senseiq ,
  https://www.philips.de/c-p/HX9992_12/9900-prestige-leistungsstarke-zahnbuerste-mit-senseiq-technologie
* documents (exact): EU and UK Declaration of conformity
  (`documents.philips.com/assets/EU%20Declaration%20of%20conformity/20250731/35e408fe...pdf`,
  `.../UK%20Declaration%20of%20Conformity/20250731/83e53b83...pdf`), User manual
  (`documents.philips.com/assets/20211007/43e2cf01...pdf`, English, one of ~50 locales).
* metadata: `false / false / false` (no expandable spec block detected in the delivered HTML).

**DEWALT DCD796P2** — PASS
* page (exact base SKU + regional suffix `GB`): https://www.dewalt.co.uk/en-gb/product/dcd796p2-gb/18v-xr-brushless-hammer-drill-driver-2-5ah-batteries
  (other regional sites `asia.dewalt.global`, `cee.dewalt.global` (`-QW`) were found in some earlier runs; not counted as stable).
* documents: **none found** — the delivered HTML carries only corporate PDFs.
  DEWALT exposes a manuals hub route (`/en-gb/operatorsmanuals`) that this stage does not search (open item).
* metadata: `has_expandable_specs=false`, `has_hidden_spec_content=true` (state/`<template>`), `requires_interaction=false`.

**Dreame G12 Pro HHR32A** — PARTIAL (proven)
* documents: Declaration of conformity, **exact** (`HHR32A` in the anchor and file name), 3/3:
  https://cdn.shopify.com/s/files/1/0302/5276/1220/files/DOC-W2545E_HHR32A.pdf , linked from
  https://global.dreametech.com/pages/declaration-of-conformity
* document/page: https://global.dreametech.com/pages/g12-pro ("G12 Pro User Manual", *probable*, family name only) — 2 of 3 runs (found through search results).
* no official product page carrying `HHR32A` was found: the SKU occurs in no official URL/title/page checked (family pages `global.dreametech.com/pages/g12-pro`, `ge.dreametech.com/en/product/dreame-g12-pro-wet-and-dry/`) except the declaration list.

Proof that generic official paths were exhausted (Dreame, run traces): brand-derived roots (12 suffixes × www/apex,
17 resolution requests) → `dreame.com` family; official domain `dreametech.com` verified from search evidence and probed
through `site:` queries (site search 20, sitemap 17, crawl 2 requests); official sitemaps of `www.`/`global.`/`de.dreametech.com`
scanned by hand (`www.` 229 product / 89 page URLs, `global.` 154 / 624, `de.` 671 / 176): no `hhr32a`; on-site search for `HHR32A` returns
"0 results"; 11 identity/domain-restricted queries × 8 providers (426–539 raw results, ~150 unique, 0 exact official page);
the declaration page and manual page scanned for documents (288 links classified, 40 rejected documents recorded).
**Not automated:** the Wayback CDX archive lookup timed out (60 s) when tried by hand; an archived official page is therefore
*not* ruled out, and no archive provider was added (too slow/unreliable for a bounded run).

## Rejected important candidates (examples from the traces)

* sibling SKU / variant: `philips.co.kr/c-p/HX9992_21/...` and `HX9990_12`, `HX9994_12` (different SKU); `dcd796p2t-*`, `dcd796d2-*`, `dcd85mp2t-*` (different DEWALT variants); `manualslib.com/.../Dreame-G12.html`, Korean `dreame/g12` pages (different commercial-model variant)
* documents: `DOC_HHR10D.pdf` "G12 Pro **Flex**" (sibling SKU + variant word); `fastenersdop.com` DoP link on the DEWALT page (non-official host);
  `user-manuals-and-faqs` (hub, not a document); hash-named PDFs whose type cannot be identified; DEWALT `safety-notices-and-recalls` (not model-specific)
* dealers/manual sites (`manualslib`, `zbozi.cz`, `bscom.cz`, `screwfix`, ...) stay in **Secondary**, never in official groups

## Trace summary (final runs)

| Product | Queries | Provider attempts (status mix) | Raw → unique → accepted / rejected | Pages | Docs |
| --- | --- | --- | --- | --- | --- |
| Gressel | 1 | 1 (direct: success) | 11 → 1 → 1 / 0 | 1 | 1 |
| Philips | 1 | 1 (direct: success) | 49 → 2 → 2 / 0 | 2 | 3 |
| DEWALT | 3 | 9 (2 success, 4 blocked, 2 low_value, 1 timeout) | 47 → 1 → 1 / 0 | 1 | 0 |
| Dreame | 11–12 | 59–76 (12–13 success, 6–9 blocked, 12–17 empty, 8–11 low_value, 1–2 timeout, 13–31 circuit_open) | 426–539 → 94–155 → 18–22 / 76–144 | 0–1 (weak) | 1–2 |

Nothing is dropped silently: every raw result has a `raw_and_normalized` trace entry (accepted / rejected + reason /
merged duplicate); every provider timeout/error is in `provider_attempts` and `provider_errors`; rejected documents carry reasons.

## Known limits

* Dreame runs use the whole ~75 s wall-clock budget because no exact page exists and every provider is asked.
* DEWALT has no official document (see above); Dreame's manual page is 2/3.
* DuckDuckGo/Naver/Bing/Seznam/Google remain unreliable in this environment (blocked, low-value, timeout); Stage 33.1 does not depend on them
  for the three PASS products (direct official surfaces answered 3/3), and they still back up the search path.
* `has_expandable_specs` for Philips/Dreame is `false` from delivered HTML only; JS-only controls would show as `unknown`.
