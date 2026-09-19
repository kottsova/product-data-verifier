# Stage 33.2 — official discovery: 10-product blind benchmark

**Verdict: NOT PASS.** 8/10 products are stable; **Bosch HBG7741B1 is a stable FAIL** (a known
exact official page exists and is never found) and **Makita DHP484Z is unstable** (PARTIAL in 1 of 3
runs). Root causes and traces below. Extraction was not touched.

Every run received only `brand + model` text (no URL, domain, seed, category, expectation), cold
(`clear_official_domain_cache()` before each run). 10 products x 3 runs = 30 live runs.
Harness: `python -m diagnostics.stage33_2_benchmark --runs 3` (`--rescore` re-scores saved traces).
Traces: `diagnostics/results/stage33_2/<product>-run<N>.json`, bot text: `<product>-bot.txt`,
tables: `summary.md`. The `EXPECTED` oracle is filled with hand-verified facts *after* the runs and only
scores saved traces; discovery never imports it.

## Stability

| Product (category) | Run 1 | Run 2 | Run 3 | Stable |
| --- | --- | --- | --- | --- |
| Gressel GAF-1825 (air fryer) | PASS ✓ 6s | PASS ✓ 6s | PASS ✓ 6s | yes |
| Dreame G12 Pro HHR32A (vacuum) | PARTIAL ✓ 75s | PARTIAL ✓ 72s | PARTIAL ✓ 72s | yes (proven PARTIAL) |
| DEWALT DCD796P2 (power tool) | PASS ✓ 26s | PASS ✓ 27s | PASS ✓ 27s | yes |
| Philips Sonicare HX9992/12 (personal care) | PASS ✓ 5s | PASS ✓ 4s | PASS ✓ 4s | yes |
| Samsung Galaxy S24 SM-S921B (smartphone) | PARTIAL 73s | PARTIAL 71s | PARTIAL 76s | yes (PARTIAL) |
| Bosch HBG7741B1 (built-in oven) | PARTIAL ✗ 58s | PARTIAL ✗ 57s | PARTIAL ✗ 57s | **FAIL, stable** |
| Makita DHP484Z (power tool) | PARTIAL ✗ 65s | PASS ✓ 42s | PASS ✓ 51s | **NO** |
| The Ordinary Niacinamide 10% + Zinc 1% (skincare) | PASS ✓ 29s | PASS ✓ 29s | PASS ✓ 24s | yes |
| DeLonghi EC685M (coffee machine) | PASS ✓ 10s | PASS ✓ 9s | PASS ✓ 9s | yes |
| Logitech MX Master 3S (PC peripheral) | PASS ✓ 13s | PASS ✓ 12s | PASS ✓ 11s | yes |

`✓/✗` = the run did / did not surface the hand-verified official page/document. Samsung has no oracle
(see below).

## Official sources found (run 3; identical in the other stable runs)

* **Gressel** — page https://gressel.ru/catalog/aerogril/aerogril_gressel_gaf_1825/ (exact), instruction PDF
  (exact). Metadata: expandable=yes, hidden=yes, interaction=no, `dom_hidden`.
* **Dreame** — exact declaration `DOC-W2545E_HHR32A.pdf`; family page `ge.dreametech.com/.../dreame-g12-pro-wet-and-dry/`
  (weak, in 2/3 runs); probable manual page. No page carries `HHR32A` (same proof as 33.1).
* **DEWALT** — https://www.dewalt.co.uk/en-gb/product/dcd796p2-gb/... (exact, regional suffix `-GB`); hidden=yes,
  interaction=no, `json_state`. No documents (manuals hub route not searched; open since 33.1).
* **Philips** — `philips.co.uk` + `philips.de` `/c-p/HX9992_12/...` (exact), EU/UK declarations + user manual (exact).
* **The Ordinary** — https://theordinary.com/en-ge/niacinamide-10-zinc-1-serum-100436.html and `theordinary.es/...`
  (exact); hidden=yes, interaction=no (`json_state` / `dom_hidden`).
* **DeLonghi** — `delonghi.com/cs-cz/.../EC685.M+EX:4.html` and `delonghi.ru/product/...ec685-m/` (exact),
  RU instruction PDF (exact). `EC685.M` (dot) == `EC685M`.
* **Logitech** — https://www.logitech.com/en-us/shop/p/mx-master-3s (exact; the `:443` in the stored URL comes
  from the direct probe and is cosmetic). Metadata: expandable=yes, hidden=no, interaction=unknown (JS shell).
* **Makita** (runs 2-3) — `makita.ae/product/...-dhp484z`, `makita.co.nz/products/model/DHP484Z`,
  `makita.com.au/...dhp484z-...` (exact); manual (exact) in run 2. (`makita.ae` answers 403 to a plain curl; the
  `.co.nz` page was fetched by hand: 200, SKU present.)
* **Samsung** — only family pages `samsung.com/{de,it}/smartphones/galaxy-s24/` (probable/weak) and 11-12 exact
  support pages `samsung.com/ae/support/model/SM-S921B.../` (support). No documents. Samsung product URLs/titles do
  not carry `SM-S921B`, so an exact *product page* cannot be proven from discovery alone -> PARTIAL.
* **Bosch** — nothing official found (18-21 dealer pages as secondary).

Documents by type across runs: instruction, declaration x2, manual, manual (probable). General hubs / FAQ /
category pages / unknown-purpose PDFs are not documents (see fixes).

## Hidden / expandable metadata (per exact official page)

| Page | expandable_specs | hidden_spec_content | interaction_required | location |
| --- | --- | --- | --- | --- |
| gressel.ru | yes | yes | no | dom_hidden |
| dewalt.co.uk | no | yes | no | json_state |
| philips.co.uk / .de | no | no | no | unknown (no spec block delivered) |
| theordinary.com / .es | no | yes | no | json_state / dom_hidden |
| delonghi.com (cs-cz) | no | no | no | unknown |
| delonghi.ru | yes | no | unknown | unknown |
| logitech.com | yes | no | unknown | unknown (JS shell) |
| makita.co.nz | no | no | no | visible_dom |
| dreame (family page) | no | yes | no | visible_dom |

`spec_location` distinguishes the four cases: `visible_dom`, `dom_hidden` (hidden by CSS/tab/`<details>`),
`json_state` (JSON/`<template>`), `js_required`, else `unknown`. A control that only reveals content already in
the DOM gives `interaction_required = no`. Nothing was clicked.

## SKU / variant safety — rejected near neighbours (from traces)

| Rejected | found | requested | reason |
| --- | --- | --- | --- |
| onlineshop.cz/...hbg7742b1-serie-8... | HBG7742B1 | HBG7741B1 | neighbouring SKU |
| cdn.shopify.com/...DOC-W2517_HHR20A... | HHR20A | HHR32A | other Dreame model declaration |
| cdn.shopify.com/...DOC-W2306_HHR31A... | HHR31A | HHR32A | other Dreame model declaration |
| kream.co.kr/products/688258 | SM-S921N | SM-S921B | regional Samsung variant |
| makitauk.com/product/dhp484.html, makita.in/product/dhp484 | DHP484 | DHP484Z | base family, not the bare-tool SKU |
| logitech.com/.../mx-master-3s-business-wireless-mouse | 3S Business | MX Master 3S | different commercial variant |
| (33.1) DCD796P2T / DCD796D2 / HX9992_21 | - | - | still rejected |

No neighbouring SKU was emitted as exact in any of the 30 runs. `/`, `_`, `-` and now `.`
(`EC685.M`) are equivalent delimiters; a different dotted suffix (`EC685.R`) is still a mismatch.

## Performance

Full per-run table: `diagnostics/results/stage33_2/summary.md` (runtime, queries, provider attempts, raw / unique /
accepted / rejected, blocked, timeout, circuit_open).

* **> 60 s:** Dreame (72-75 s x3), Samsung (71-76 s x3), Makita run 1 (65 s).
* **> 30 s:** Bosch (57-58 s x3), Makita runs 2-3 (42 s, 51 s).
* <= 30 s: the rest (Gressel 6, Philips 4-5, DeLonghi 9-10, Logitech 11-13, Ordinary 24-29, DEWALT 26-27).
* Slow products are the ones where the direct probe returns nothing and the run degrades to the SERP chain
  (about 50-60 provider attempts, 10-22 `circuit_open`, 2-10 `blocked`, 1-3 `timeout`). No optimisation was done.

## Root causes for the non-PASS products

1. **Bosch (stable FAIL).** The exact page exists:
   https://www.bosch-home.com/de/de/product/kochen-backen/herde-backoefen/einbaubackoefen/HBG7741B1 (HTTP 200,
   title "HBG7741B1 Einbau-Backofen | Bosch Hausgeräte DE"; also in `bosch-home.com/de/de/sitemap.xml`). The brand's
   appliance site is `bosch-home.com`, **not** a brand-derived root (`bosch.<tld>`), so the direct probe never
   probes it. SERP saw only `bosch-home.com/cz/cs/contact-form/...` (brand-only, rejected) — the exact product page
   never appeared in DDG/Bing/Google results (DDG blocked/reset, Google timeout) and the domain was never verified.
   Trace: `bosch-hbg7741b1-run*.json` (`official_paths`: 17 domain-resolution requests, all `bosch.*`).
   Candidate generic fix (not done, changes recall/latency): probe registrable domains seen in results whose label is
   `<brand>-<word>` (`bosch-home`) with the same sitemap/robots/site-search steps.
2. **Makita (unstable).** The direct probe is `empty` in every run (brand-root `makita.<tld>` sites answer with tiny/JS
   shells; `_brand_root_domains(limit=12)` also truncates the TLD list, dropping `com.au`, ...), so the result depends
   on which SERP provider answers before DDG opens its circuit. Run 1: DDG blocked, 0 official candidates -> PARTIAL.
   Runs 2-3: SERP returned `makita.ae / .co.nz / .com.au` product pages -> PASS.
3. **Samsung (PARTIAL, stable).** Exact-SKU support pages found; product pages are model-family pages without the SKU.
   Not a discovery defect that a generic rule can fix without accepting family pages as exact.
4. **Dreame** — unchanged from 33.1 (proven PARTIAL; Wayback lookup still not automated).

## Generic fixes made during this stage (found by blind scout runs; no brand/product code)

* `core/match.py::_parts_forming_identifier` + `core/discovery.py::_path_names_complete_model`: `EC685.M` /
  `ec685-m` in a URL is `EC685M` (was "conflicting model identifier" -> all De'Longhi official pages rejected).
* `core/discovery.py::_brand_evidence`: an exact-brand `.com` host is brand evidence when the SERP title is empty or
  URL-derived (Logitech official `/shop/p/mx-master-3s` stayed "secondary").
* `services/discovery_debug.py`: a PDF whose purpose is unknown is no longer defaulted to `manual` (a Samsung
  sustainability "LCA Results" PDF had been reported as an exact manual); per-page metadata with `spec_location`,
  locale/region, SKU-neighbour rejection records (found vs requested), performance counters;
  `bot/discovery_formatters.py` prints them.
* Tests: `tests/test_stage33_2_benchmark_support.py` (5 tests); full suite 999 tests OK.

## Bot output

`format_discovery_result` (the real Telegram formatter) output for run 1 of every product is saved as
`diagnostics/results/stage33_2/<product>-bot.txt` with groups Official product pages / Official support pages /
Official documents / Secondary / Discovery metadata, per-page `expandable_specs / hidden_spec_content /
interaction_required [location]`, rejected SKU neighbours, and a Performance line. URLs are plain links.

## Acceptance gate

| Item | Result |
| --- | --- |
| 10/10 products run blind, 3 cold runs each | PASS |
| no product/brand hardcode (oracle only scores traces) | PASS |
| Stage 33.1 products unchanged (Gressel, Dreame, DEWALT, Philips) | PASS |
| known exact official sources found stably | **FAIL** (Bosch; Makita unstable) |
| neighbouring SKUs never exact | PASS |
| official / secondary separated; documents typed | PASS |
| hidden/expandable metadata | PASS |
| full trace saved | PASS |
| tests, `git diff --check` | PASS |

**Stage 33.2 is not passed.** Not masked: Bosch needs non-brand-derived official-domain discovery; Makita needs a
provider-independent probe (or a wider TLD/host set). Neither was tuned here.
