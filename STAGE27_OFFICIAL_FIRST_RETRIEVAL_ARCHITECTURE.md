# Stage 27 — Official-First Retrieval Architecture

Date: 2026-09-17

Dataset: `regression/datasets/stage23_blind_v1.json` (frozen 15 products)

Verdict: **PARTIAL (acceptance gate not passed)**

## 1. Repository precondition

After `git fetch origin --prune` and before Stage 27 work:

- `HEAD = origin/main = b313fe2bb708f07f2c36daee1c28f3f8254af8dc`
- working tree clean
- no pending changes required a preparatory commit

## 2. Root cause

Stage 26 had a SERP-free `DirectDomainProbeProvider`, but it was a final
fallback and its site traversal was too shallow:

- only the first few sitemap declarations were inspected;
- gzip sitemap indexes were not decoded;
- nested sitemap traversal spent its cap on top-level siblings;
- only one hard-coded conventional search shape was parsed;
- embedded structured URLs and canonical links were ignored;
- official-site failure reasons were hidden behind an empty provider result;
- the six-second provider deadline could discard an exact candidate already
  found by the direct path.

The Stage 27 pre-change smoke reached manufacturer hosts but found **0/5**
exact URLs. This isolated the failure from SERP availability.

## 3. Official-first architecture

The live path is now:

`product identity`
→ mechanically resolve and verify a brand-consistent root
→ inspect official discovery surfaces under strict request/time/byte limits
→ emit exact-model candidates
→ existing normalization/deduplication
→ existing identity matching
→ existing authority classification
→ existing fetch/extraction/validation
→ SERP only when official discovery does not produce an exact-model result

`DirectDomainProbeProvider` is first in the provider tuple. It short-circuits
later providers only for an exact-model result; a homepage-only result is kept
as authority corroboration and still falls through to SERP.

No product URL, brand-domain table, or blind-dataset answer was added.
Authority, validation, and identity thresholds were not relaxed.

## 4. Supported official discovery surfaces

- brand-derived `.com` and market-root domains, with redirect consistency and
  on-page brand verification;
- `robots.txt` sitemap declarations;
- `sitemap.xml`, sitemap indexes, nested sitemap files, and `.xml.gz` decoding;
- market/product-aware sitemap prioritization;
- public GET search forms plus generic platform search routes;
- ordinary same-domain links;
- structured/embedded URL fields in public search payloads;
- canonical links;
- a small bounded crawl from product/catalog/support links;
- process-local structure cache for sitemap/search/crawl metadata only.

Bounds per product/provider instance:

- 14 total requests;
- 12-second provider deadline inside the unchanged 60-second workflow budget;
- at most 8 sitemap documents and depth 2;
- at most 2 crawl pages;
- at most 4 MiB read per response;
- stop after sufficient exact candidates.

## 5. Telemetry

Provider attempts now expose:

- official discovery method;
- request count by method;
- official candidate and exact-model candidate counts;
- accepted verified official URL after existing ranking/authority logic;
- official failure reason;
- existing duration and budget before/after values.

The same fields are present in workflow metadata and live diagnostic artifacts.

## 6. Smoke diagnosis (five brands, no SERP)

### Pre-change baseline

| Product | Requests | Exact official URL |
|---|---:|---:|
| Samsung Galaxy Z Flip6 | 7 | 0 |
| Dell XPS 13 9340 | 6 | 0 |
| Miele KM 7464 FL | 2 | 0 |
| Smeg SI2M7953D | 7 | 0 |
| PFAFF ambition 620 | 4 | 0 |

### Post-change result

| Product | Official domain(s) reached | Surfaces observed | Result / blocker |
|---|---|---|---|
| Samsung Galaxy Z Flip6 | `samsung.com` | homepage, 94 robots sitemap declarations, market index, nested support/product sitemaps | **Exact found** through sitemap: `https://samsung.com/us/support/mobile/phones/galaxy-z/galaxy-z-flip6` (7 direct requests in the successful provider smoke). |
| Dell XPS 13 9340 | `dell.com` | homepage, robots, gzip product/support indexes, site search, bounded crawl | No exact candidate. Product sitemap children reached the 4 MiB bound; public search returned HTTP 403. 14-request cap enforced. |
| Miele KM 7464 FL | `miele.de`, `miele.com` | regional/global homepages, robots, sitemap index, search, bounded crawl | No exact candidate. Regional root returned HTTP 403; global sitemap/search routes did not expose the model. 9 requests. |
| Smeg SI2M7953D | `smeg.co.uk` → `smeguk.com`, `smeg.com` | redirect verification, robots, sitemaps, public search forms, bounded crawl | No exact candidate. UK routes redirected to a generic homepage; `.com` sitemap requests timed out. 14-request cap enforced. |
| PFAFF ambition 620 | `pfaff.com` | minimal homepage, robots, default sitemap, generic search | No exact candidate. Homepage was a small JS shell; robots/sitemap/search returned HTTP 404. 5 requests. |

Result: **1/5 brands** produced an exact official page without SERP. This does
not meet the instruction to demonstrate viability across several brands.

## 7. End-to-end SERP-disabled guard

Only the successful Samsung product was carried through the complete existing
pipeline. All six SERP providers were disabled.

Artifact (ignored runtime output):
`diagnostics/results/stage27-samsung-official-only-run3.json`

| Metric | Result |
|---|---:|
| completed / service success | 1 / 1 |
| discovery method | sitemap |
| method requests | domain resolution 1, robots 1, sitemap 5 |
| official candidates / exact candidates | 4 / 3 |
| accepted verified official URL | Samsung Galaxy Z Flip6 support URL above |
| accepted authority role | `official_document`, `verified` |
| workflow runtime | 20.24 s |
| remaining 60 s budget | 39.76 s |
| quality | insufficient |
| confirmed / coverage | 2 / 33.3% |

The exact support page fetch redirected to a model-code endpoint and was
classified as captcha-blocked. A separate first-party accessory page fetched
successfully, so the non-zero confirmed count is not evidence of complete
product-page extraction. The guard proves retrieval and authority acceptance,
but not robust downstream evidence conversion.

## 8. Tests

- full suite: **735/735 PASS** (`python -m unittest discover -s tests`);
- new deterministic tests cover:
  - exact official result short-circuiting SERP;
  - homepage-only fall-through to SERP;
  - gzip nested sitemap traversal;
  - public structured search payload extraction;
  - structure cache not retaining product answers;
  - preserved Stage 26 and earlier behavior.

## 9. Full gate decision

The 15-product, three-cold-run full gate was **not run**. The requested smoke
gate explicitly requires several brands to find exact pages before spending
the full live budget. The post-change result was only 1/5.

## 10. Comparison with Stage 26

| Capability | Stage 26 | Stage 27 |
|---|---|---|
| default order | SERP first, direct last | official first, SERP fallback |
| nested/gzip sitemap traversal | shallow / no gzip support | bounded depth-2 gzip/index traversal |
| search/structured/canonical discovery | one conventional route / anchors | detected GET forms, generic routes, embedded URLs, canonicals |
| official method telemetry | provider-level only | method request counts, exact count, accepted URL, failure reason |
| live exact official result from direct infrastructure | 0 in final blind gate | 1/5 smoke (Samsung) |
| systemic viability without SERP | failed | still not established |

## 11. Remaining blockers

1. Large sitemap shards can place the requested URL beyond the bounded byte
   window; downloading them completely would violate the bounded design.
2. Several manufacturers expose catalog search only behind WAF/JS or private
   application calls that cannot be used generically without site adapters.
3. Brand-derived root resolution cannot generically infer unrelated consumer
   domains while preserving the no-brand-table rule.
4. Official product/support fetches can still be captcha-blocked after their
   URL is correctly discovered and authority-verified.
5. The successful Samsung sitemap also exposed accessory URLs containing the
   phone model; the unchanged downstream relevance rules selected one of them.
   Stage 27 did not relax or rewrite identity matching to hide that limitation.

## 12. Changed files

- `core/discovery.py`
- `core/workflow.py`
- `diagnostics/live_quality_diagnosis.py`
- `diagnostics/stage27_official_smoke.py` (new)
- `tests/test_discovery.py`
- `tests/test_stage27_official_first.py` (new)
- `STAGE27_OFFICIAL_FIRST_RETRIEVAL_ARCHITECTURE.md` (new)

## 13. Verdict

**PARTIAL.** The architecture is genuinely official-first, bounded, generic,
and can find and authority-verify an exact official URL without any SERP.
However, only one of five smoke brands succeeded, so the multi-brand viability
gate and every subsequent full-gate PASS criterion remain unmet.
