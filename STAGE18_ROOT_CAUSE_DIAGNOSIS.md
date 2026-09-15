# Stage 18.1 — Live Quality Root Cause Diagnosis

Date: 2026-09-15

Compared revisions: Stage 9 `95b6495` and Stage 17 `56b8420`

Verdict: **PASS as diagnosis.** The failure modes are localized and supported by
controlled runs. No production behavior was changed. Phase 18.2 was not started.

## Executive finding

There is no deterministic production-code regression in the core pipeline after
Stage 9. The Git tree object for `core/` is identical at both revisions:
`efffdb73cbb3771d1f1f65b1fb95ef1684370806`. Stages 10–17 added a service
boundary, cache, bot, operations, deployment, and regression harness, but did not
change discovery, ranking, fetch, extraction, category, schema, mapping, targeted
search, validation, or quality code.

The primary demonstrated regression is live discovery availability and
variability. In valid network runs Google and DuckDuckGo Lite were repeatedly
reported as bot-blocked; Naver returned many results but was the only usable
fallback. Its result set was noisy, produced no accepted candidate for Janome or
Gressel, and cannot establish official-source authority. During the same diagnosis
Google changed from blocked/timeout behavior to ten results per query for an
otherwise identical Bosch/DE request. This is direct inter-run evidence, not an
inference from low coverage.

HONOR and Dreame expose two secondary, generic weaknesses after discovery:
marketing/multilingual raw labels mostly become dynamic attributes instead of
expected schema facts, category evidence can remain unknown, fallback-derived
sources retain unknown authority, and targeted search multiplies provider cost
without necessarily finding useful evidence.

## Reproduction runs

All full runs used `ProductVerifierService`, `repository=None`,
`force_refresh=True`, `max_sources=5`, one worker, an independent killable child
process per product, atomic JSON checkpoints, and the immutable Stage 17 reference
inputs unless an A/B override is stated. Live JSON outputs are intentionally
ignored under `diagnostics/results/`.

| Run | Purpose | Key result |
|---|---|---|
| `stage18-dataset-markets.json` | Full, targeted enabled, Stage 17 markets, 210 s | Bosch timeout; HONOR timeout; Janome 0%; Gressel 0%; Dreame 5.6% |
| `stage18-no-targeted.json` | Full, targeted disabled, 90 s | Bosch timeout; HONOR 0%/78.0 s; Janome 0%/33.1 s; Gressel 0%/30.6 s; Dreame 5.6%/41.7 s |
| `stage18-discovery-only.json` | Stage 17 markets, 90 s | Bosch discovery timeout; others 30.4–38.3 s |
| `stage18-discovery-global.json` | Stage 9 market shape (`global`) | Bosch 4 accepted; HONOR 31; Janome 0; Gressel 0; Dreame 12 |
| `stage18-bosch-de-repeat.json` | Repeat identical Bosch/DE discovery | 65.5 s, Google success, 27 accepted |
| `stage18-dreame-stage9-shape.json` | `model="G12 Pro HHR32A"`, no article, global | 16.7%, category unknown; did not recover Stage 9 |
| `stage18-dreame-global-exact-no-targeted.json` | Stage 17 identity, global | 5.6%/43.6 s, versus RU 5.6%/41.7 s |

One attempted global run was rejected as invalid evidence because the sandbox
explicitly returned `WinError 10013` and `ERR_NETWORK_ACCESS_DENIED`. It was
overwritten by the valid network-enabled run and is not used below.

Exact timestamps, queries, provider attempts, returned URLs/titles, rejection
metadata, fetch statuses, raw and mapped attributes, targeted results, validation,
and quality are present in each checkpoint. Source-level duration is recorded as
unavailable because the existing fetch DTO does not expose it; the diagnostic does
not fabricate timing.

Stage 9 retained only the final reference coverage values, not its live URLs,
provider attempts, stage timings, raw attributes, or exact run environment.
Consequently an exact old-versus-new payload diff at every stage is impossible.
The defensible comparison is the retained Stage 9 outcome plus the identical core
Git tree, reconstructed Stage 9 input defaults, and the new controlled stage traces.

## Stage-by-stage evidence

### Bosch PUE611BB5E

- Identity: exact normalized model, high confidence in completed discovery-only
  runs.
- Discovery: Stage 17 full timed out at 180 s; Phase 18 full timed out at 210 s;
  an isolated DE discovery timed out at 90 s. A later identical Bosch/DE discovery
  completed in 65.5 s: Google returned zero for the first query and ten results for
  each of the next five, yielding 27 accepted and one rejected candidate. A global
  run completed in 33.7 s with Google/DDG blocked and Naver yielding four accepted
  candidates from 394 results.
- Relevance onward: unavailable in the timed-out reference execution because the
  killed worker produced no internal result. Fetch, extraction, category, mapping,
  targeted search, validation, and quality cannot be reconstructed from that run;
  reporting values for them would be invented.
- Localization: the separate discovery-only run itself exceeded 90 s, proving that
  initial provider work alone can exhaust a meaningful boundary. It does not prove
  the exact internal phase reached by each 180/210 s full-run worker. Market is not
  a deterministic explanation because the same DE discovery request later
  succeeded.

### HONOR X8d

- Identity: normalized `HONOR` / `X8d`, no article, high confidence.
- Discovery/ranking: partial; Google and DDG bot-blocked; Naver returned 477 result
  appearances across six queries. The relevance gate accepted 31 and rejected 125;
  five were selected, including four `honor.com` regional pages and Gizmochina.
- Fetch/extraction: four successes and one error; 46 raw attributes
  (`label_value=9`, `spec_block=37`).
- Category/schema/mapping: category remained `unknown`/low. All 46 raw facts were
  mapped, but chiefly to 31 dynamic marketing labels such as `3000nits`,
  `instant_ai_button`, and `ultra_slim_design`; none filled the six base quality
  fields in the no-target A/B.
- Targeted search: the plan contained 11 queries for dimensions and weights.
  Stage 17 fetched ten targeted sources and reached 16.7% in 170.2 s. A repeat with
  targeted search exceeded 210 s, while disabling it completed in 78.0 s at 0%.
- Validation/quality: 0 confirmed, 23 unresolved in the no-target trace, all
  authority `unknown`; quality `insufficient`, 0/6 there. Stage 17's targeted run
  produced 1/6 (16.7%) but still 0 confirmed.

### Janome Sakura 95

- Identity: normalized exact model, high confidence.
- Discovery/ranking: partial; Google and DDG bot-blocked. Naver returned 367 result
  appearances across six queries, but the relevance gate accepted 0 and rejected
  122 unique candidates.
- Fetch/extraction/mapping: no selected source, fetch, raw attribute, or mapped
  fact. This localizes the first loss to discovery result quality plus relevance,
  before extraction.
- Category/schema: `unknown`/low; base quality schema 6, found 0.
- Targeted search: 11 planned queries; no useful raw or mapped fact. The full trace
  recorded zero fetches, four unresolved field outcomes, and provider attempts
  ending at Naver.
- Validation/quality: 0 confirmed, 6 unresolved, 0 conflicts, all authority
  `unknown`; `insufficient`, 0%.

### Gressel GAF-1825

- Identity: normalized exact model, high confidence.
- Discovery/ranking: partial; Google and DDG bot-blocked. Naver returned 106 result
  appearances across six queries; 0 accepted and 30 unique candidates rejected.
- Fetch/extraction/mapping: no initial selected source, fetch, raw attribute, or
  mapped fact.
- Category/schema: `unknown`/low; base quality schema 6, found 0.
- Targeted search: 11 planned queries; no useful raw or mapped fact. The full trace
  recorded zero fetches, two unresolved and two blocked field outcomes; eight
  query outcomes reached Naver successfully and two were blocked.
- Validation/quality: 0 confirmed, 6 unresolved, 0 conflicts, all authority
  `unknown`; `insufficient`, 0%.

### Dreame G12 Pro / HHR32A

- Identity: normalized `Dreame` / `G12 Pro`, article `HHR32A`, high confidence.
- Discovery/ranking: partial; Google and DDG bot-blocked; Naver returned 629 result
  appearances across eight queries. The gate accepted 12 and rejected 146; five
  were selected. They included a contradictory `dreame.ua/...g10-pro-flex.html`
  URL whose result title claimed G12 Pro Flex, the global G12 Pro page, a Korean
  manual, and two Behance pages. All retained authority `unknown`.
- Fetch/extraction: three successes and two HTTP 403 blocks. The successful sources
  produced 107 raw attributes (`label_value=77`, `spec_block=21`, `html_table=5`,
  `json_ld=4`); the global manufacturer page itself produced zero.
- Category/schema/mapping: category was correctly `wet_dry_vacuum`/high. All 107
  raw attributes were mapped, but mostly to dynamically created Ukrainian labels;
  only one of 18 expected quality fields was found. This is semantic mapping loss,
  not empty extraction.
- Targeted search: 44 planned queries, six unique fetches, no useful targeted raw
  or mapped fact; 14 field outcomes unresolved and two blocked in the captured full
  trace.
- Validation/quality: 0 confirmed, 18 unresolved, 0 conflicts, all authority
  `unknown`; `insufficient`, 1/18 = 5.6%. The reason is missing confirming evidence
  for core model identity.
- Input A/B: switching RU to global with the same split identity kept 5.6%. Using
  the historical compound model, no article, and global produced 16.7% only because
  category fell to unknown and the denominator fell to six; it still had zero
  confirmed facts and therefore did not recover the Stage 9 result.

## Root-cause classification

| Product | Root cause | Evidence | Confidence | Deterministic vs external | Generic fix candidate (not implemented) |
|---|---|---|---|---|---|
| Bosch | Search-provider volatility and unbounded aggregate discovery tail, converted to a product timeout by the harness boundary | DE discovery-only changed from >90 s to Google success with 27 candidates/65.5 s; full runs exceeded 180/210 s; `core/` unchanged | High | Primarily external C/D; deterministic timeout exposure E | Per-provider and per-stage deadlines, circuit breaker, provider health telemetry, independent browser contexts |
| HONOR | Primary providers blocked; fallback finds pages but unknown authority plus weak category/schema conversion; targeted fan-out dominates latency | 31 accepted, 4/5 fetch success, 46 raw, category unknown, 0 confirmed; 78 s without targeted versus 170–>210 s with it | High for discovery/latency; medium for semantic secondary cause | Mixed external C/D and generic deterministic robustness H/E | Diversify provider path; bounded targeted budget; category evidence from trustworthy page context; multilingual/semantic schema mapping tests |
| Janome | Only fallback returns results, but none survive generic relevance | Google/DDG blocked; Naver 367 appearances, 122 unique rejected, 0 accepted/fetched | High | External C/D exposed at deterministic relevance boundary | Provider diversification/health; retain rejection telemetry; improve generic evidence-bearing result retrieval, not product rules |
| Gressel | Same provider failure pattern with an especially sparse/noisy fallback set | Google/DDG blocked; Naver 106 appearances, 30 unique rejected, 0 accepted/fetched | High | External C/D exposed at deterministic relevance boundary | Same generic provider recovery and bounded retry strategy as Janome |
| Dreame | Blocked primary discovery leads to noisy/contradictory candidates; extracted multilingual marketing data does not populate expected schema; targeted fan-out adds cost without evidence | 12 accepted/146 rejected; 3 fetch successes/2 blocks; 107 raw but only 1/18 expected; 0 confirmed; input/market A/B does not recover | High | Mixed external C/D and generic deterministic robustness H/E | Stronger generic URL/title/article consistency; safe authority-domain discovery; semantic multilingual mapping; targeted wall-clock budget |

Classification A (post-Stage-9 core regression) is disproved for this revision
pair. G (cache behavior) is disproved for the cold runs. B (dependency/runtime
change) remains possible historically but is not demonstrated because Stage 9 did
not record an exact interpreter, lockfile, browser revision, or retained raw run.
F (harness configuration) affects timing and the Dreame input representation, but
the controlled A/B tests show it is not the primary quality-loss cause.

## Stage 17 harness audit

| Item | Finding |
|---|---|
| Service construction | Stable `ProductVerifierService`; service reshapes results and invokes the same core workflow |
| Cold/cache | SQLite repository is configured, but cold sets `force_refresh=True`; successful results are saved but never read for that call |
| `max_sources` | 5, same as Stage 9 diagnostic default |
| Targeted search | Enabled, same as Stage 9 diagnostic |
| Market | Stage 9 diagnostic default was `global`; Stage 17 uses DE/RU dataset markets. Global A/B left accepted counts unchanged for four products; Dreame quality stayed 5.6%. Bosch variability is demonstrably inter-run |
| Article/model | Four products equivalent. Stage 9 represented Dreame as compound model `G12 Pro HHR32A`; Stage 17 correctly separates model and article. The historical shape currently yields 0 confirmed and does not recover quality |
| Timeout | Stage 17 adds a 180 s killable hard boundary; Stage 9 diagnostic had no outer bound. This changes timeout reporting, not pipeline facts, and prevents one product from blocking the suite |
| Concurrency | Stage 17 uses 2; diagnosis uses 1. Timeouts and low quality reproduce sequentially, so concurrency is not sufficient as the cause |
| Browser | Headless defaults true in both trees. Google uses one fixed temp persistent-profile directory; two concurrent workers may contend for it. This is a real generic risk, but sequential reproduction shows it is not the sole cause |
| Request mapping | Brand, model, article, market, max sources, and targeted flag are passed 1:1 into `ProductWorkflowRequest.from_parts` |

## Code and environment findings

- `git rev-parse 95b6495:core` and `git rev-parse HEAD:core` both return
  `efffdb73cbb3771d1f1f65b1fb95ef1684370806`.
- The only requirements-policy change after Stage 9 is adding
  `python-telegram-bot`; the core dependency ranges remain Playwright 1.x,
  pypdf 6.x, BeautifulSoup 4.x, and requests 2.x.
- Diagnostic host: CPython 3.14.4 on Windows 11; requests 2.33.1, urllib3 2.7.0,
  BeautifulSoup 4.14.3, Playwright 1.59.0, pypdf 6.14.2. Chromium is installed
  and launched; both bot-check and later successful Google runs were observed.
- Deployment image is pinned to Python 3.13.13 and the lockfile above. Therefore
  the local live diagnosis is not an image-runtime equivalence test. Docker build
  remains unavailable because the daemon is unavailable.
- Exact Stage 9 installed package/browser/Python versions were not recorded, so a
  dependency regression cannot honestly be asserted or excluded beyond the
  unchanged declared ranges and identical core tree.

## Smallest generic fix sequence for Phase 18.2

1. **P0.1 — Bound and instrument discovery per provider and per stage.** Add a
   total discovery budget, shorter provider deadlines, circuit breaking after a
   blocked result, provider latency/status metrics, and unique browser profiles per
   worker. Prove it with deterministic timeout/block fixtures before live reruns.
2. **P0.2 — Restore a dependable, authority-safe discovery route.** Add or
   configure a provider that returns relevant public results without treating a
   fallback success as official authority. Keep provenance and authority rules
   unchanged.
3. **P0.3 — Bound targeted search by wall clock and evidence yield.** Prioritize
   critical gaps, stop after repeated provider blocks/no useful facts, and dedupe
   queries/fetches across fields.
4. **P1.1 — Harden generic identity-aware ranking.** Detect contradictions between
   URL, title, page identity, and requested article/base model before selection;
   add multilingual fixtures rather than product-specific exceptions.
5. **P1.2 — Improve category and semantic mapping from trustworthy context.** Turn
   recognized multilingual spec labels into existing canonical attributes and
   avoid letting marketing fragments dominate category/schema evidence. Do not
   change schema, thresholds, missing values, or authority.
6. **P1.3 — Rerun the exact immutable five-product cold set, then all 50.** Record
   locked runtime/browser metadata and retain provider/stage timings so future
   Stage 9 comparisons do not depend on final coverage alone.

## Reproduce the diagnostic

```text
python -m diagnostics.live_quality_diagnosis all --concurrency 1 --timeout 210
python -m diagnostics.live_quality_diagnosis all --phase discovery --concurrency 1 --timeout 90
python -m diagnostics.live_quality_diagnosis all --no-targeted-search --concurrency 1 --timeout 90
python -m diagnostics.live_quality_diagnosis dreame --model "G12 Pro HHR32A" --without-article --market global --timeout 210
```

These are live experiments: timestamps and provider outcomes are part of the
evidence and later executions are expected to differ.

## Files and verification

- Added `diagnostics/live_quality_diagnosis.py`: read-only, resumable
  stage-by-stage live diagnostic; it calls existing workflow/service behavior and
  serializes existing decisions.
- Added `tests/test_live_quality_diagnosis.py`: verifies the five requests against
  the immutable Stage 17 dataset and proves all pipeline stages are exposed without
  recomputation.
- Added this report and ignored ephemeral `diagnostics/results/` live outputs.
- Increased only the Stage 17 scheduler test timeout from 0.5 to 1.5 seconds. On
  Windows/Python 3.14 the deadline includes spawn/import time and the 0.5-second
  value repeatedly timed out the nominal fast worker. The production scheduler is
  unchanged, and the slow fixture still sleeps 2 seconds.

Verification:

```text
python -m unittest tests.test_regression_harness.RepeatabilityAndSchedulingTests.test_hard_timeout_is_recorded_and_next_product_still_runs -v
OK (1 test, 2.119 s)

python -m unittest discover -s tests
OK (570 tests, 19.437 s)
```
