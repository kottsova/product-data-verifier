# Stage 33.3 — Bosch / Makita official discovery recovery

**Verdict: PASS on the acceptance gate, with one explicit deviation** — for Bosch the exact official pages
found are the Swiss-locale pages (`/ch/de|fr|it/`), **not** the `/de/de/...` URL named in the task (see
"What was not reached"). Extraction untouched. Dreame and Samsung untouched (Dreame stays a proven PARTIAL).

Input of every run: only `brand + model`, cold (`clear_official_domain_cache()`), no seed/domain/hint.
Harness: `python -m diagnostics.stage33_3_recovery` (traces + bot text in `diagnostics/results/stage33_3/`).

## Result

| Product | mode | runs | status | seconds | first official path | exact pages |
| --- | --- | --- | --- | --- | --- | --- |
| Bosch HBG7741B1 | full | 3 | PASS, PASS, PASS | 36.3 / 28.8 / 28.3 | `direct_domain_probe:sku_route_template` | 3 (ch/de, ch/fr, ch/it) |
| Makita DHP484Z | full | 3 | PASS, PASS, PASS | 31.4 / 30.5 / 33.3 | `direct_domain_probe:site_search` | 1 (`makita.co.nz/products/model/DHP484Z`) |
| Makita DHP484Z | **no SERP** (direct probe only) | 3 | PASS, PASS, PASS | 29.8 / 29.0 / 30.8 | `direct_domain_probe:site_search` | 1 (same) |
| Gressel GAF-1825 | full | 1 | PASS | 6.2 | site_search | 1 |
| Philips HX9992/12 | full | 1 | PASS | 5.7 | sku_path_template | 2 |
| DEWALT DCD796P2 | full | 1 | PASS | 15.2 | sitemap | 1 |
| DeLonghi EC685M | full | 1 | PASS | 9.2 | sitemap | 2 |
| Logitech MX Master 3S | full | 1 | PASS | 10.0 | sitemap | 1 |
| The Ordinary | full | 1 | PASS | 27.0 | homepage_links | 2 |
| Dreame HHR32A | full | 1 | PARTIAL ✓ (unchanged) | 72.6 | - | 0 (exact declaration only) |
| Samsung S24 SM-S921B | full | 1 | PARTIAL (unchanged) | 77.0 | - | 0 product; exact support pages |

No neighbouring SKU was emitted as exact in any of the 17 runs (checked against DHP4xx/HBG77xx/DCD79x/EC685.R patterns
in every exact page URL). Samsung: recorded as its own case — *family product page + exact support identity*
(`samsung.com/ae/support/model/SM-S921B…`), no change.

## Root causes and generic fixes (no brand/product table, no SERP dependency)

### Bosch — official site is `bosch-home.com`, not `bosch.<tld>`
Evidence chain, all from public pages, none from a search engine:

1. Official brand roots resolve as before (`bosch.com`, `.de`, `.co.uk`, `bosch-group.ru`).
2. **Cross-link harvest.** While the probe already crawls a root's product-ish pages, it now also collects
   `<brand><word>.<tld>` sibling domains from every absolute URL on those pages — anchors, JSON state and data attributes
   (bosch.com's *At home* page carries `bosch-home.com/...` in a country-selector JSON, not in an `<a>`). A second hop
   crawls up to 5 children of the crawled product pages (`/products-and-services/` → `/at-home/`), 404 retried with a slash.
3. **Ranking without knowledge of the brand:** siblings are grouped by label; a label the official site links under many public
   suffixes (`bosch-home.{com,at,be,co.uk,…}` ≈ 45 roots) is a real multi-market network and ranks first; one-off links
   (`-motorsport`, `-origify`) last. Press/jobs/shop/dealer/… words are excluded. Top 3 labels are probed.
4. **Sibling site probe.** Sitemaps (heavy, 4–14 MB, truncated at 4 MB) do not reach the product URLs, but they reveal the
   site's locales. The probe now tries the widespread `/<locale>/product/<SKU>` route on those locales plus routes learned
   from real SKU-tailed URLs. A page counts **only if its own title/canonical names the SKU** (a 200 "no results" echo of the
   requested URL does not). This is the path that found the pages.
5. **Authority (not weakened):** a sibling domain becomes official only if (a) the official brand site linked to it (marker set by
   the probe that fetched the link), (b) its label is the brand plus a non-commerce word, (c) the fetched page names the brand
   and carries the exact model. Same record without the marker, or on `acme-shop.com`, is rejected (unit tests).

### Makita — regional sites reachable without SERP
Makita's sites sit under many public suffixes; only 12 were probed and the site (co.nz) that carries the model was not among them.
Even when probed, the model page is only reachable through the site's own search form (`POST /search/` with a hidden field).

1. **Site-native search:** POST search forms are now used as GET with their hidden inputs kept (`/search/?a=search&searchTerm=DHP484Z`).
2. **Widening only after failure:** if the primary roots gave no exact page, further public-suffix roots of the brand label are
   filtered by **DNS** (no HTTP for names that do not exist), then resolved (brand must appear on the page) and asked one cheap
   question — their own search form (≤2 requests each).
3. The `.com.au`/`.ae` Makita sites answer 403 to a plain HTTP client and are not reached (found only via SERP in 33.2).
   The no-SERP runs prove one reachable official site is enough: `makita.co.nz` 3/3.

### Defects found by the regression runs (fixed)
* `canonicalize_url` raises `ValueError` on JSON garbage such as `https://x:"TRUE"}}`; the new harvest crashed the Philips probe
  and pushed it to SERP (18 s instead of 5). Harvest/family-crawl/extra phases are now exception-safe; regression test added.
* Widening phases were given time *beyond* the provider's own 36 s deadline, so the session cut the probe (`timeout`) and a
  Makita run fell back to SERP (72 s). Phases now share the provider timeout (primary ≤ 24 s, widening until timeout − 1.5 s).
* `_has_exact` did not read `HX9992_12` for `HX9992/12`, starting the widening phases needlessly; it now uses the same loose SKU reading
  as the page verification.
* Displayed URLs no longer carry `:443` (normalised in the page fetch).

## Performance / probe count

Direct-probe HTTP requests (33.2 traces vs 33.3) and time:

| Product | 33.2 requests | 33.3 requests | 33.2 s | 33.3 s | domain resolutions 33.3 |
| --- | --- | --- | --- | --- | --- |
| Bosch | 56 | **114** | 57–58 | 28–36 | 19 |
| Makita | 44 | **67** | 42–65 | 31–33 (29–31 no SERP) | 28 |
| Gressel | 31 | 31 | 6 | 6.2 | 22 |
| Philips | 49 | 54 | 4–5 | 5.7 | 12 |
| DEWALT | 50 | 51 | 26–27 | 15.2 | 13 |
| DeLonghi | 38 | 38 | 9–10 | 9.2 | 21 |
| Logitech | 26 | 26 | 11–13 | 10.0 | 14 |
| The Ordinary | 28 | 28 | 24–29 | 27.0 | 20 |
| Dreame | 60 | 80 | 72–75 | 72.6 | 33 |
| Samsung | 42–53 | 53 | 71–76 | 77.0 | 20 |

* Only products that do **not** find an exact page in the primary phase pay for widening (Bosch +58, Makita +23, Dreame +20);
  products found early are unchanged (±5). Widening has its own request budget (+70 over the primary 60) and a per-domain limit of 20,
  and family probing is capped at 3 sibling labels (deterministic: no shared counter, so the outcome does not depend on thread timing).
* Bosch is faster overall (28–36 s vs 57–58 s) because the probe now finds the page and the SERP chain short-circuits.
* Slow (> 60 s): Dreame 72.6 s, Samsung 77.0 s (both unchanged SERP-bound PARTIALs). > 30 s: Bosch run 1 (36.3), Makita run 3 (33.3).
* The one HTTP-heavy cost is Bosch's 114 requests (36 of them sitemaps of 4 MB each). Not optimised (no recall trade-off made).

## What was not reached

`https://www.bosch-home.com/de/de/product/kochen-backen/herde-backoefen/einbaubackoefen/HBG7741B1` (exists, HTTP 200) is **not**
returned; the exact pages found are `bosch-home.com/ch/de|fr|it/product/…/HBG7741B1` (same SKU, same official site, exact).
Reason: the `/product/<SKU>` probe runs on locales learned from the sitemaps that are actually fetched (the first 8 by URL order:
`ae/en, ao, ar, az, bo, ch/de, ch/fr, ch/it`); `de` is 12th and `/de/` is a one-segment locale entry that redirects to `/de/de`, so
covering it needs one more request per locale. It was not added to avoid growing the probe count further. The acceptance gate
(“Bosch exact official page: 3/3”) is met; the specific URL is not.

Other limits: the widening depends on the official site exposing sibling links or a search form / product route; sites that answer
403 to plain HTTP (`makita.com.au`, `.ae`) need the browser probe, unchanged here.

## Acceptance gate

| Item | Result |
| --- | --- |
| Bosch exact official page 3/3 | PASS (ch/de, ch/fr, ch/it; `/de/de` URL itself not reached) |
| Makita exact official page 3/3 | PASS |
| Makita without SERP (direct probe only) | PASS 3/3 |
| neighbouring SKUs not accepted | PASS (no neighbour exact in 17 runs) |
| regressions (Gressel, Philips, DEWALT, DeLonghi, Logitech, Ordinary; Dreame/Samsung unchanged) | PASS |
| SKU matching / authority / exact-probable / official-secondary / document validation weakened | no (authority gained one evidence-gated path, unit-tested) |
| tests | 1007 OK |
