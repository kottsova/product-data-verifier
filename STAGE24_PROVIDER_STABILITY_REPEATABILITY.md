# Stage 24 — Provider Stability & Repeatability Gate

Date: 2026-09-17

Baseline commit: `f9aec2c46f4990fc00a1a06e716a55a7c1222bd7`

Verdict: **PARTIAL**

The gate is not passed. Generic orchestration changes reduced Google head-of-line
cost, added explicit low-value SERP handling, broadened strict exact-brand root
recovery to regional public suffixes, and made budget exhaustion return a bounded
degraded result instead of a service failure. However, the final third cold run
again experienced a provider-wide access collapse and returned zero confirmed
facts for all 15 products. The result is therefore not repeatable.

Telegram, UI, CSV, Excel, authority thresholds, and validation semantics were not
changed.

## A. Baseline repeatability

Three independent cold runs were completed before code changes against
`regression/datasets/stage23_blind_v1.json`:

- `force_refresh=True`;
- `repository=None`;
- `max_sources=5`;
- workflow budget 90 seconds;
- process timeout 95 seconds;
- concurrency 3;
- identical 15-product dataset.

An initial sandbox-denied series was retained under `.cache/stage24/sandbox-denied-*`
but excluded from measurement because every socket failed with `WinError 10013`.
The baseline below is the subsequent network-enabled live series.

| Metric | Baseline 1 | Baseline 2 | Baseline 3 |
|---|---:|---:|---:|
| Confirmed > 0 | 9/15 (60.0%) | 8/15 (53.3%) | 0/15 (0.0%) |
| Partial/verified | 2/15 (13.3%) | 3/15 (20.0%) | 0/15 (0.0%) |
| Exact official discovery | 7/15 (46.7%) | 6/15 (40.0%) | 0/15 (0.0%) |
| Exact official fetch/use | 4/7 (57.1%) | 3/6 (50.0%) | n/a |
| Median coverage | 21.7% | 11.1% | 0.0% |
| Median runtime | 44.42 s | 42.74 s | 29.35 s |
| Max runtime | 59.13 s | 51.93 s | 29.95 s |

Baseline stability:

- confirmed-rate spread: 60.0 percentage points;
- median-coverage spread: 21.7 percentage points;
- quality changed: 8/15 (53.3%);
- confirmed count jumped more than 50%: 10/15 (66.7%);
- repeatability score: **49.58/100**.

## B. Provider diagnostics

Baseline aggregate across three runs:

| Provider | Requests | Success | Failure | Blocked | Timeout | Exact-official contribution | Runtime |
|---|---:|---:|---:|---:|---:|---:|---:|
| Google | 45 | 0.0% | 100.0% | 0 | 45 | 0 | 1125.86 s |
| DuckDuckGo HTML | 99 | 54.6% | 45.5% | 26 | 1 | 13 product-runs | 140.77 s |
| DuckDuckGo Lite | 47 | 4.3% | 95.7% | 26 | 0 | 0 | 89.87 s |
| Naver | 45 | 0.0% | 100.0% | 0 | 0 | 0 | 53.47 s |

Final aggregate across three gate runs:

| Provider | Requests | Success | Failure | Blocked | Timeout | Low-value | Exact-official contribution | Runtime |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DuckDuckGo HTML | 94 | 52.1% | 47.9% | 43 | 0 | 0 | 19 product-runs | 121.49 s |
| Naver | 52 | 11.5% | 88.5% | 45 | 0 | 1 | 0 | 68.01 s |
| DuckDuckGo Lite | 47 | 4.3% | 95.7% | 45 | 0 | 0 | 2 product-runs | 42.48 s |
| Bing | 262 | 3.1% | 97.0% | 0 | 0 | 245 | 0 | 131.12 s |
| Google | 45 | 0.0% | 100.0% | 0 | 45 | 0 | 0 | 380.95 s |

Findings:

1. Baseline Google consumed 25 seconds per product before fallbacks despite a
   0/45 success record. Its slice was reduced to 8 seconds and it was moved behind
   the observed-reliable HTTP routes.
2. DuckDuckGo HTML remained the main exact-official contributor. When it became
   blocked together with Naver/Lite in gate run 3, the dataset collapsed.
3. Bing was transport-reliable but usually semantically low-value: 245/262
   requests returned nonempty results without sufficient query identity signal.
   Explicit `low_value` classification correctly yielded to later providers but
   did not supply independent exact-official coverage.
4. Naver changed from useful in tuning runs to 45 blocked requests in the final
   series. Provider circuits prevented repeated blocked calls inside a product,
   but request-local circuits cannot protect the next product from an external
   provider-wide outage.
5. Useful results found before a later provider failure are preserved. Candidate
   merge, canonical dedupe, ranking, and URL tie-breaking are deterministic.
6. A bounded sitemap recovery experiment produced 0 candidates in 22 live
   attempts and was removed rather than shipping unproductive complexity.

## C. Root causes

### Provider instability

- In gate run 3, each product's first DuckDuckGo HTML, Naver, DuckDuckGo Lite,
  and Google attempt failed (15 blocked, 15 blocked, 15 blocked, and 15 timeout,
  respectively), after which request-local circuits skipped later queries.
- Bing stayed reachable but 92 requests were low-value; three nominal successes
  and three late Naver successes contributed no exact-official candidate.
- The system therefore still lacks an independently useful keyless discovery
  route during simultaneous SERP WAF/rate-limit windows.

### Fetch/WAF instability

- In successful discovery runs, exact official use was only 60.0–63.6% because
  official pages still returned captcha, access denied, not found, timeout, or
  malformed content.
- One tuning run encountered HTML served under a PDF URL. It remained a fetch/
  extraction failure and was not promoted to evidence.

### Mapping and category limitations

- Partial/verified quality remained only 0–2 products per final run.
- HP, Ninja, Fisher & Paykel, and several sewing/cooktop cases remained
  insufficient even when lower-authority exact-model pages existed.
- Sparse retrieval can collapse category detection to `unknown`, reducing schema
  size and downstream coverage without manufacturing confirmations.

### Architecture limitations

- Circuit breaker state is request-local. It bounds one product but cannot share
  an external provider outage across the concurrent 15-product run.
- Repository cache is deliberately unavailable in cold runs, so previously found
  official URLs cannot stabilize a later outage.
- The frozen source limit and 90-second workflow budget leave no room for an
  unbounded recovery crawl; the budget was not increased.

## D. Changes

Only generic stability changes were retained:

- Added an independent bounded Bing HTML provider and deterministic decoding of
  Bing redirect URLs.
- Added provider-neutral `low_value` SERP classification using query identity
  signals; a nonempty irrelevant SERP no longer starves fallbacks.
- Classified HTTP 403/429 responses as blocked for circuit diagnostics.
- Reduced Google timeout from 25 to 8 seconds.
- Ordered default providers by measured usefulness:
  DuckDuckGo HTML → Naver → DuckDuckGo Lite → Bing → Google.
- Extended strict exact-brand-root official recovery from `.com` only to regional
  public suffixes. It still requires exact registrable brand label, brand in the
  title, and exact model evidence in title/URL. Fuzzy, parent-company, reserved,
  and wrong-model domains remain unverified.
- Added `BudgetExhaustedError` as an internal control signal so an initial or
  targeted fetch that loses the budget-start race returns the best bounded
  degraded result instead of `workflow_failure`.
- Added a cross-run audit that reports per-run metrics, per-product variance,
  provider status/runtime/contribution, and a transparent repeatability score.
- Added an isolated versioned Apple iPhone 15 dataset.

No product-specific URL, provider rule, or budget increase was added.

## E. After repeatability

The final three cold runs used the exact baseline configuration.

| Metric | Gate run 1 | Gate run 2 | Gate run 3 | Required |
|---|---:|---:|---:|---:|
| Service success | 15/15 | 15/15 | 15/15 | bounded result |
| Confirmed > 0 | 11/15 (73.3%) | 10/15 (66.7%) | 0/15 (0.0%) | ≥12/15 each |
| Partial/verified | 2/15 (13.3%) | 2/15 (13.3%) | 0/15 (0.0%) | ≥9/15 each |
| Exact official discovery | 11/15 (73.3%) | 10/15 (66.7%) | 0/15 (0.0%) | diagnostic |
| Exact official fetch/use | 7/11 (63.6%) | 6/10 (60.0%) | n/a | ≥70% each |
| Median coverage | 21.7% | 21.7% | 0.0% | ≥25% each |
| Median runtime | 33.84 s | 32.81 s | 16.40 s | diagnostic |
| Max runtime | 44.18 s | 44.60 s | 33.64 s | ≤90 s |

The graceful budget fix eliminated the repeated HP service failure observed in
the immediately preceding tuning series: all 45 final product-runs returned a
structured service result.

## F. Stability metrics

- confirmed-rate spread: **73.3 percentage points** (required ≤10);
- median-coverage spread: **21.7 percentage points** (required ≤10);
- products changing quality class: **7/15 (46.7%)** (required ≤20%);
- products with confirmed jump >50%: **11/15 (73.3%)** (must not be a majority);
- repeatability score: **46.24/100**.

Score formula:

`mean(1-confirmed-rate-spread, 1-coverage-spread/100, 1-quality-change-rate, 1-confirmed-jump-rate) × 100`

Per-product final trajectories (`run 1 / run 2 / run 3`):

| Product | Confirmed | Coverage % | Quality | Confirmed variance | Coverage variance |
|---|---|---|---|---:|---:|
| Google Pixel 9 Pro | 5 / 5 / 0 | 30.4 / 30.4 / 0.0 | conflicted / conflicted / insufficient | 5.5556 | 205.3689 |
| Samsung Galaxy Z Flip6 | 3 / 3 / 0 | 21.7 / 21.7 / 0.0 | conflicted / conflicted / insufficient | 2.0000 | 104.6422 |
| Sony Xperia 1 VI | 3 / 3 / 0 | 21.7 / 39.1 / 0.0 | conflicted / conflicted / insufficient | 2.0000 | 255.8289 |
| Dell XPS 13 9340 | 3 / 3 / 0 | 23.5 / 41.2 / 0.0 | insufficient / insufficient / insufficient | 2.0000 | 284.7756 |
| Lenovo ThinkPad X1 Carbon Gen 12 | 5 / 5 / 0 | 29.4 / 29.4 / 0.0 | partial / partial / insufficient | 5.5556 | 192.0800 |
| HP Spectre x360 14-eu0000 | 0 / 0 / 0 | 23.5 / 0.0 / 0.0 | insufficient / insufficient / insufficient | 0.0000 | 122.7222 |
| Miele KM 7464 FL | 2 / 2 / 0 | 11.1 / 11.1 / 0.0 | insufficient / insufficient / insufficient | 0.8889 | 27.3800 |
| Smeg SI2M7953D | 9 / 9 / 0 | 61.1 / 61.1 / 0.0 | conflicted / conflicted / insufficient | 18.0000 | 829.6022 |
| Fisher & Paykel CI604DTB4 | 0 / 0 / 0 | 5.6 / 5.6 / 0.0 | insufficient / insufficient / insufficient | 0.0000 | 6.9689 |
| Tineco FLOOR ONE S7 Stretch | 5 / 5 / 0 | 25.0 / 25.0 / 0.0 | partial / partial / insufficient | 5.5556 | 138.8889 |
| Ninja AF400UK | 0 / 0 / 0 | 0.0 / 0.0 / 0.0 | insufficient / insufficient / insufficient | 0.0000 | 0.0000 |
| Philips NA342/00 | 2 / 5 / 0 | 11.8 / 29.4 / 0.0 | insufficient / insufficient / insufficient | 4.2222 | 145.9289 |
| PFAFF ambition 620 | 2 / 2 / 0 | 10.0 / 10.0 / 0.0 | insufficient / insufficient / insufficient | 0.8889 | 22.2222 |
| Husqvarna VIKING OPAL 650 | 0 / 0 / 0 | 5.0 / 0.0 / 0.0 | insufficient / insufficient / insufficient | 0.0000 | 5.5556 |
| Elna eXperience 550 | 4 / 0 / 0 | 30.0 / 0.0 / 0.0 | conflicted / insufficient / insufficient | 3.5556 | 200.0000 |

Full cross-run rows, including category, official discovery/provider, selection,
fetch statuses, raw/mapped/confirmed/unresolved/conflicts, runtime, and dominant
failure, are reproducibly emitted by
`diagnostics/stability_repeatability_audit.py` into
`.cache/stage24/gate-repeatability.json`.

## G. Apple iPhone 15

Three isolated cold runs on the final code used
`regression/datasets/stage24_apple_v1.json`.

| Metric | Apple 1 | Apple 2 | Apple 3 |
|---|---:|---:|---:|
| Exact official discovered | yes | yes | yes |
| Selected/fetched successfully | yes/yes | yes/yes | yes/yes |
| Official raw / expected mapped | 52 / 4 | 52 / 4 | 79 / 4 |
| Confirmed / unresolved / conflicts | 3 / 20 / 0 | 3 / 20 / 0 | 5 / 17 / 1 |
| Coverage | 13.0% | 13.0% | 30.4% |
| Quality | insufficient | insufficient | conflicted |
| Runtime | 64.52 s | 90.15 s | 68.50 s |

The special Stage 24 condition passed: Apple never returned to zero confirmed
facts while exact official evidence was available. Apple itself was not fully
repeatable, and run 2 exceeded the configured workflow budget by 0.15 seconds;
both remain explicit blockers rather than being hidden by the nonzero guard.

## H. Safety

- No hardcoded product URL or product-specific provider rule was added.
- `core/authority.py` and `core/validation.py` are unchanged.
- Exact-model and wrong-model guards were not weakened.
- Regional exact-brand root recovery requires mechanical domain equality plus
  exact title/URL evidence; fuzzy and parent-company domains are not promoted.
- Search-query text and low-value SERPs are not page evidence.
- Unknown/retailer evidence is not promoted to first-party authority.
- Candidate, provider, fetch, extraction, mapping, and validation provenance is
  preserved in diagnostic artifacts.
- Runtime remains bounded; the overall workflow budget was not increased.

## I. Tests

- Full suite: **690/690 PASS**
- Focused discovery/workflow/targeted/fetch/repeatability suite: **170/170 PASS**
- Compile/import for all changed Python modules: PASS
- `git diff --check`: PASS
- Final 15-product live runs: 45/45 processes and 45/45 service results completed.
- Final Apple live runs: 3/3 processes and 3/3 service results completed.

## J. Git

- Baseline: `f9aec2c46f4990fc00a1a06e716a55a7c1222bd7`
- Implementation/report commit: recorded in the final handoff after commit.
- Push target: `origin/main`.
- Final handoff records the post-push HEAD equality and clean working tree.

## K. Verdict

**Stage 24 PARTIAL — provider orchestration is more bounded and budget failures
now degrade safely, but the repeatability gate remains blocked by simultaneous
SERP provider/WAF collapse, insufficient independent exact-official discovery,
official fetch instability, low partial/verified quality, and residual mapping/
category limitations.**

This is not a FAIL regression because the final workflow returned structured
results for all 45 product-runs, preserved safety/authority/validation semantics,
and good provider windows improved exact-official discovery. It is not a PASS
because none of the required repeatability or quality thresholds held across all
three final runs.
