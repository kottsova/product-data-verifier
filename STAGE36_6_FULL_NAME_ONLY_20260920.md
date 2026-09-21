# Stage 36.6: complete name-only measurement

This measurement supersedes the earlier decision to defer the full run. The
11 old transitions without saved response bodies remain **historically
unknown** (rows 3, 8, 10, 13, 24, 25, 26, 28, 32, 35, 36). They did not
remove products from the new denominator. Before launch, the
[preflight](diagnostics/baselines/stage36_6/full_name_only_20260920/preflight.json)
recorded exact HEAD `8d5865c7d4d51ae3fcaf5d56455306631f4ffaf3`, branch,
dirty harness/runner state and hashes, all 50 archived input strings,
provider chain and initial local circuit state, disabled persistent provider
health, global market, 75-second service budget, and empty/reset
official-domain caches. The harness called
`DiscoveryDebugService.discover_name(name)` without `product_category` for
every row, using a fresh service and the historical strings. Each result was
written immediately as `01.json` through `50.json` in a new local directory.
Stage 36.5 and earlier Stage 36.6 files were untouched.

The [50-row table](diagnostics/baselines/stage36_6/full_name_only_20260920/all_50.md),
[per-product CSV](diagnostics/baselines/stage36_6/full_name_only_20260920/product_results.csv),
[page-fetch attempts](diagnostics/baselines/stage36_6/full_name_only_20260920/page_fetches.csv),
[provider outcomes](diagnostics/baselines/stage36_6/full_name_only_20260920/provider_outcomes.csv),
[old/new comparison](diagnostics/baselines/stage36_6/full_name_only_20260920/comparison.csv),
[JSON summary](diagnostics/baselines/stage36_6/full_name_only_20260920/full_summary.json),
and [sanitized raw archive and hash manifest](diagnostics/baselines/stage36_6/full_name_only_20260920/manifest.json)
retain the measurement. The archive audit passed. The run took 4,345.6
seconds including pauses; service time totaled 4,241.4 seconds. No harness
row errored. Playwright printed an `EPIPE` during final cleanup after row
50 had been saved; the process exited 0 and the archive verified. Cleanup
still needs its own runtime fix. The
[process outcome](diagnostics/baselines/stage36_6/full_name_only_20260920/run_process_outcome.json)
records the post-result error and successful archive audit.

| Archived request contract | All rows | PASS | PARTIAL | FAIL |
| --- | ---: | ---: | ---: | ---: |
| Commercial SKU/article | 23 | 4 | 16 | 3 |
| Model | 19 | 3 | 13 | 3 |
| Family | 8 | 2 | 5 | 1 |
| **Total** | **50** | **9** | **34** | **7** |

The new run accepted **9 first-party product pages, all 9 exact main-product
pages**, and **0 support pages and 0 documents**. These are discovery counts,
not extracted specifications. The [accepted-source audit](diagnostics/baselines/stage36_6/full_name_only_20260920/accepted_sources_for_audit.csv)
and [manual primary/operator evidence](diagnostics/baselines/stage36_6/full_name_only_20260920/manual_checks.json)
cover **all 9/9** accepted URLs: Bosch, Siemens, Haier, Logitech, Razer,
Corsair RM850x, Nike FN4231-010, Oral-B iO Series 10 and CeraVe Hydrating
Facial Cleanser. Their current main-product content and brand/licensed
operator evidence were confirmed independently. **False first-party: 0/9;
false exact: 0/9** within this accepted set. This is observed precision of
the accepted URLs, not recall or proof that all 41 non-PASS products lack
official pages. The independent [PARTIAL sample](diagnostics/baselines/stage36_6/full_name_only_20260920/manual_partial_checks.json)
reviews nine SKU/model/family cases; the [FAIL check](diagnostics/baselines/stage36_6/full_name_only_20260920/manual_fail_checks.json)
records Braun's current exact page, which the run did not retrieve.

The 41 non-PASS rows remain in the denominator. Four had **no accepted
search candidate at all**: Makita, Milwaukee, Bosch Professional and Einhell
(rows 37–40); each recorded zero provider results and `search_status=timeout`.
Braun (14), ASUS graphics card (34) and STIHL (41) had secondary leads but
no verified first-party result, accounting for the other three FAILs.
Nineteen non-PASS rows had at least one selected page with a loading limit
(rows 3, 4, 5, 6, 8, 10, 12, 13, 15, 17, 20, 27, 28, 29, 30, 32, 34, 35,
46). Across all 50, selected page outcomes were **44 loaded, 58 budget
exhausted, 6 unavailable**; the latter recorded **five HTTP 403 attempts and
three timeouts**. One loaded page was a JavaScript shell. These are separate
observable outcomes, not manufacturer-negative evidence. Eleven non-PASS
rows had loaded pages whose main identity or category/operator scope remained
unproved (3, 9, 12, 15, 16, 17, 19, 24, 25, 26, 36). Roborock's main page
and LG's C4 page illustrate conservative scope refusal; Philips illustrates
a delivered page without primary product content. TP-Link's Czech distributor
and NETGEAR forum remained outside first-party manufacturer pages.

The run issued **309 search queries** and **1,424 provider attempts**,
with 3,449.2 seconds summed across provider attempt durations. Outcomes were
285 success, 154 empty, 130 low-value, 139 capped, 226 blocked, 90 timeout,
398 circuit-open and 2 error. The per-provider CSV retains counts and seconds
for Bing, browser discovery, direct-domain probe, DuckDuckGo HTML and Lite,
Google, Naver and Seznam. Provider attempts are not independent products.
**32/50 rows exceeded 75 seconds** in actual harness time; STIHL was longest
at **314.6 seconds**. The service reports that active external requests cannot
be interrupted. A managed worker with a hard deadline and cleanup handling
is a separate runtime defect; overruns were measured, not excluded.

The [seven saved name-only negative checks](diagnostics/baselines/stage36_6/negative_name_only_full_20260920.json)
retain 0 exact-official outcomes for the TP-Link Czech distributor, Roborock
accessory, Razer HyperSpeed, Corsair SHIFT, CeraVe refill, Oral-B twin pack
and NETGEAR forum. They are controlled saved-candidate replays through the
public method, not extra live products in the 50-row numerator. Current
[Tefal UK primary content](https://www.tefal.co.uk/Linen-Care/Steam-Irons/Professional-Results/Ultimate-Pure-FV9845-Steam-Iron-Black-%26-Rose-Gold/p/1830007280)
heads the iron `FV9845` but states commercial reference **FV9845G0**;
[Tefal's model collection](https://www.tefal.com/ultimate-pure) also lists
**FV9845E0**. `FV9845` is therefore a supported **base model**, not a
separately evidenced commercial SKU. Its independently checked first-party
model/family identity is reported in the PARTIAL notes, while original row
13 remains **SKU-level PARTIAL**: all four page checks in this full run were
budget exhausted. Neither variant is promoted to an exact unsuffixed SKU.

**Stage 36.6 verdict: PARTIAL.** The accepted authority and main-product
identity decisions survived independent review, and the seven targeted
negatives stayed negative. Full-run coverage is only 9/50 PASS, with material
search retrieval, provider blocking/timeout, page loading/rendering and
conservative category-scope limits; 0 support/document sources were recovered.
The next work should measure and improve those separate limits, plus a hard
interruptible budget and Playwright cleanup. This result does not justify
additional manual seeds inside Stage 36.6 solely to raise PASS. PR #1 stays
draft pending evaluation of this full run.
