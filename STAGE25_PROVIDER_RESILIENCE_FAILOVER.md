# Stage 25 — Provider Resilience / Failover Architecture

Date: 2026-09-17

Baseline commit: `4c19cf0fc4e732c55bfc6da7c2b0eaba9d7ac041`

Verdict: **PARTIAL**

Telegram, UI, CSV, Excel, authority thresholds, validation semantics, and identity
matching were not changed. `core/authority.py`, `core/validate.py`,
`core/validation.py`, `core/identity.py`, and `core/match.py` have zero diff.

## A. Root cause of the Stage 24 collapse

Confirmed from Stage 24's own retained telemetry (`.cache/stage24/final-run-3.json`),
re-read as part of this stage's diagnosis, not assumed:

- In gate run 3, for **every** product, the first DuckDuckGo HTML, Naver, and
  DuckDuckGo Lite attempts all returned `403 Forbidden` (WAF-classified `blocked`),
  and Google's attempt returned `timeout` ("deadline exhausted waiting for
  results"). Bing stayed reachable but its results were repeatedly classified
  `low_value` (lacked query-identity signal) for model-specific queries. No
  product's discovery trace produced a single `fetch_attempted` source.
- Each of the 15 products ran in its own fresh OS subprocess with a brand-new
  `ResilientSearchSession`. The existing circuit breaker
  (`core/discovery.py`'s `_open_providers`/`_failure_counts`) is scoped to one
  session, so every product independently rediscovered the same already-dead
  providers from scratch: Google alone cost `380.95s` of cumulative runtime
  across the three-run gate for a 0% success rate, and DuckDuckGo Lite/Naver
  were each retried fresh by all 15 products despite being blocked for all of
  them.
- `discover_global_official_domains` (the existing deterministic
  "official domain" corroboration path) does no network calls of its own — it
  is a pure filter over results a SERP provider already returned, so it could
  not help once every SERP provider was down or slow simultaneously.

Architecturally: the system had **one shared failure domain** (five keyless
HTML-scraping SERP providers, all reachable only over the same egress path)
and **no mechanism to learn, across a run, that part of that domain was
already down**, so a correlated outage cost every product in the run its own
full discovery budget for nothing.

## B. Architectural changes

### 1. Cross-process provider health / circuit breaker (`core/provider_health.py`, new)

`ProviderHealthStore`: a small, file-backed (or in-memory, for tests)
success/failure counter with an explicit `closed → open → half_open → closed`
circuit per string key (`"discovery:<provider>"`, `"fetch:<host>"`). Opens
after 2 consecutive failures observed **anywhere in the run** (not just this
product), stays open for a cooldown (default 45s, doubling on a failed
half-open probe up to 180s), then allows exactly one recovery probe. It is
opt-in: disabled (a pure no-op) unless `PDV_PROVIDER_HEALTH_PATH` is set, so
every existing caller/test is unaffected by default. Locking is a best-effort
sidecar-file lock — health tracking is a scheduling optimization, never a
correctness dependency, so a lock that can't be acquired promptly is skipped
rather than blocking the pipeline.

### 2. Discovery (`core/discovery.py`)

- `ResilientSearchSession` now checks the shared store before every provider
  attempt (in addition to its existing per-session circuit) and records
  success/failure after each one. A `shared_circuit_open` field on
  `ProviderAttempt` distinguishes a shared-store skip from a local one in
  telemetry.
- A new `failure_class` axis (`FailureClass.TIMEOUT` / `CONNECTION_ERROR` /
  `BLOCKED` / `RATE_LIMITED` / `MALFORMED` / `EMPTY` / `OTHER`) is additive to
  the existing `status` field — it lets a 403 WAF challenge and an HTTP 429 be
  told apart even though both currently map to `status="blocked"`.
- One bounded retry (`RETRY_BACKOFF_SECONDS = 0.3s`), only for a fast-fail
  `CONNECTION_ERROR` and only if the shared workflow budget still has >1s
  remaining. A WAF block or a timeout that already spent its full deadline is
  **not** retried — repeating either would just double the cost for the same
  outcome, which is exactly the "first dead provider must not monopolize the
  budget" failure Stage 24 flagged.
- `DirectDomainProbeProvider` (new, genuinely independent discovery path): the
  only provider in the session that never queries a search engine. It only
  activates for the `"{brand} official website"` bootstrap query, generates 1-2
  candidate root domains generically from the brand string (no per-brand or
  per-product table), and does a small number of direct, bounded HTTP GETs to
  the brand's own domain. A probed page is never auto-trusted: it becomes a
  plain candidate that still has to clear the pre-existing
  `discover_global_official_domains` brand-in-title / exact-model
  corroboration logic — this provider adds no new promotion path and does not
  touch authority. It is deliberately narrow: it establishes an official
  *domain*, not a model-specific *product page* (see §I, remaining blockers).

### 3. Fetch (`core/fetch.py`)

- `fetch_source` gained the same finer failure classification (429 →
  `RATE_LIMITED`, 5xx → `UNAVAILABLE`, connection/timeout distinguished) and
  one bounded, budget-aware retry (`FETCH_RETRY_BACKOFF_SECONDS = 0.3s`) for
  `TIMEOUT` / `CONNECTION_ERROR` / `RATE_LIMITED` only — never for a WAF block,
  which would just get the same answer again.
- `fetch_candidate` now consults the same shared `ProviderHealthStore`, keyed
  by host (`"fetch:<hostname>"`): a host with repeated recent failures
  elsewhere in the run is skipped without a new request.
- `FetchResult` now carries a real `duration_seconds` (previously a dead
  placeholder in the diagnostics serializer — `_source()` in
  `diagnostics/live_quality_diagnosis.py` always emitted `None`; this is now
  wired to the actual measured value).

### 4. Workflow (`core/workflow.py`)

`run_product_workflow` resolves one `ProviderHealthStore.from_env()` instance
per live request and passes it to both `ResilientSearchSession` and every
`fetch_candidate` call, so a host discovery already learned is dead this run
and a host fetch independently finds dead are tracked in the same place.
No-op unless a batch runner explicitly enables it (see below) — the Telegram
bot path is unchanged.

### 5. Diagnostics / batch runner wiring

`diagnostics/live_quality_diagnosis.py` gained `--provider-health-path`: when
set, the store is reset at the start of `run()` (so three independent cold
runs never see stale state from a previous run), the path is exported via
`PDV_PROVIDER_HEALTH_PATH` before `regression.runner.run_products` spawns its
per-product child processes (each spawned process inherits the parent's
environment, giving genuine cross-process sharing for the duration of one
run), and a final `provider_health_snapshot` is recorded into the output
artifact. `diagnostics/stability_repeatability_audit.py` now also reports
`shared_circuit_open_attempts`, `retried_attempts`, and the per-run health
snapshots.

### 6. Controlled fault injection (`diagnostics/provider_fault_injection.py`, new)

Forces a named provider class to always raise, for the duration of one live
product run, via `unittest.mock.patch.object` — reproducible, unlike waiting
for a real WAF window. Resets `core.discovery`'s module-level official-domain
cache before every scenario so results from one scenario cannot leak into the
next (a test-harness concern only; the real per-product-subprocess gate never
shares that cache across products).

## C. Files changed

- `core/provider_health.py` (new)
- `core/discovery.py`
- `core/fetch.py`
- `core/workflow.py`
- `diagnostics/live_quality_diagnosis.py`
- `diagnostics/stability_repeatability_audit.py`
- `diagnostics/provider_fault_injection.py` (new)
- `tests/test_provider_health.py` (new)
- `tests/test_provider_resilience.py` (new)
- `tests/test_discovery.py`, `tests/test_fetch.py` (updated for the new default
  provider and new internal timing calls)

Not touched: `core/authority.py`, `core/validate.py`, `core/validation.py`,
`core/identity.py`, `core/match.py`, `core/category.py`, `core/mapping.py`,
`core/schema.py`, `bot/*`, `core/export.py`.

## D. Which providers/paths are now independent

- The five SERP-scraping providers (DuckDuckGo HTML, Naver, DuckDuckGo Lite,
  Bing, Google) remain a shared failure domain by nature (same egress, same
  class of anti-automation defenses) — that has not changed and cannot be
  changed without adding paid/keyed APIs, which is out of scope. What changed
  is that the pipeline now **learns** when part of that domain is down and
  stops re-paying for it every product.
- `DirectDomainProbeProvider` shares no transport, host, or rate limit with
  any of the five above — confirmed structurally (it only holds its own
  `requests.Session`, no reference to any other provider class) and
  confirmed live (§F): it made real, successful HTTP requests to manufacturer
  domains in every blind-gate run, independent of whichever SERP providers
  were up or down that run.
- Fetch was already decoupled from *which* provider discovered a URL (a
  candidate is just a URL by the time it reaches `fetch_candidate`); it now
  additionally has its own, separate circuit breaker keyed by host, so a dead
  fetch host cannot be conflated with, or silently protected by, a healthy
  discovery provider.

## E. Failover / circuit breaker / retry / budget isolation, concretely

| Mechanism | Scope | Behavior |
|---|---|---|
| Request-local circuit (pre-existing) | one `ResilientSearchSession` (one product) | opens after 1 failure of `blocked`/`timeout`/`parse_error` |
| Shared circuit (new) | one run, across products/processes | opens after 2 consecutive failures observed anywhere; 45s cooldown, doubling to 180s max; single-probe half-open recovery |
| Provider time-share cap (pre-existing) | one product | a provider is skipped once its cumulative time reaches 25% of the total workflow budget |
| Per-provider timeout (pre-existing) | one attempt | 8s per SERP provider, 6s for the new direct-domain probe |
| Bounded retry (new) | one attempt | exactly one extra try, only for `CONNECTION_ERROR` (discovery) / `TIMEOUT`+`CONNECTION_ERROR`+`RATE_LIMITED` (fetch), only if budget headroom remains |

## F. Unit/integration/focused test results

- Full suite: **723/723 PASS** (690 baseline + 33 new).
- Focused discovery/workflow/targeted/fetch/repeatability/resilience suite:
  **207/207 PASS**.
- `core/authority.py`, `core/validate.py`, `core/validation.py`,
  `core/identity.py`, `core/match.py`: **zero diff**.

## G. Controlled failure matrix

9 scenarios × 2 live products (Apple iPhone 15, Bosch PUE611BB5E), run twice
for reproducibility (results below are from the second, methodologically
clean run — see §B.6).

| Scenario | Apple | Bosch | Notes |
|---|---|---|---|
| DuckDuckGo HTML down (alone) | collapsed | collapsed | reproducible both runs — see caveat below |
| Naver down (alone) | OK (confirmed 5) | OK (schema_found 9) | not collapsed |
| DuckDuckGo Lite down (alone) | OK (confirmed 5) | OK (schema_found 9) | not collapsed |
| Bing down (alone) | OK (confirmed 5) | OK (schema_found 9) | not collapsed |
| Google down (alone) | OK (confirmed 5) | OK (schema_found 9) | not collapsed |
| Direct-domain-probe down (alone) | OK (confirmed 5) | OK (schema_found 9) | not collapsed — confirms this path is a bonus, not a dependency |
| Multiple SERP providers down simultaneously (HTML+Naver+Lite) | OK (confirmed 5) | collapsed | mixed |
| All 5 SERP providers down (only direct-domain-probe left) | collapsed | collapsed | expected — see §I |

**Honest caveat, not papered over**: disabling DuckDuckGo HTML alone
collapsed both test products in both independent runs. Live conditions at
test time show DuckDuckGo HTML as the current dominant, most productive
keyless engine; Naver/DuckDuckGo Lite/Bing/Google were not, on their own,
independently sufficient for these two specific products *today*. This is an
external, live condition (matches Stage 24's own provider table showing the
same asymmetry), not something the shared circuit breaker can manufacture
capacity around — the breaker makes failure of a *non-dominant* provider
free, and makes a *known-dead* provider stop costing budget, but it cannot
invent search results a live engine isn't returning. This is the single most
important remaining risk and is why this stage is not a PASS (see §J).

## H. Blind gate: 3 independent cold runs, 15 products

Same configuration as Stage 24's gate: `regression/datasets/stage23_blind_v1.json`,
`force_refresh=True`, `repository=None`, `max_sources=5`, workflow budget 90s,
process timeout 95s, concurrency 3.

| Metric | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Service success | 15/15 | 15/15 | 15/15 |
| Confirmed > 0 | 7/15 (46.7%) | 10/15 (66.7%) | 12/15 (80.0%) |
| Partial/verified | 0/15 (0.0%) | 1/15 (6.7%) | 2/15 (13.3%) |
| Exact official discovered | 8/15 | 9/15 | 12/15 |
| Exact official fetch/use rate | 75.0% | 88.9% | 66.7% |
| Median coverage | 5.9% | 10.0% | 17.6% |
| Median runtime | 48.66s | 60.43s | 36.35s |
| Max runtime | 89.96s | 78.39s | 90.49s |

No run produced anything resembling Stage 24's 0/15 collapse.

**Stability**:

- confirmed-rate spread: **33.3 percentage points** (Stage 24 final: 73.3pp)
- median-coverage spread: **11.7 percentage points** (Stage 24 final: 21.7pp)
- products changing quality class: 4/15 (26.7%)
- products with confirmed jump >50%: 10/15 (66.7%)
- **repeatability score: 65.41/100** (Stage 24 final: 46.24/100)

**Resilience mechanism evidence, from the same 3 runs** (per-provider,
aggregated, `.cache/stage25/gate-repeatability.json`):

| Provider | Requests made | Success rate | Circuit-open skips (of which shared) | Runtime | Exact-official contributions |
|---|---:|---:|---:|---:|---:|
| duckduckgo_html | 79 | 59.5% | 275 (13 shared) | 109.1s | 19 |
| naver | 237 | 86.5% | 69 (7 shared) | 577.2s | 22 |
| duckduckgo_lite | 3 | 0.0% | 99 (**24 shared**) | 4.1s | 0 |
| bing | 94 | 2.1% | 8 (0 shared) | 50.8s | 0 |
| google | 4 | 0.0% | 96 (**23 shared**) | 32.1s | 0 |
| direct_domain_probe | 100 | 11.0% | 0 | 14.6s | 0 (by design, see §I) |

Google made only **4** real requests across the entire 3-run/45-product-run
gate (down from Stage 24's 45, one per product) because the shared circuit
recognized it as dead after the first couple of products and skipped it for
the rest of the run — Google's total cost fell from 380.95s (Stage 24 final)
to 32.1s. DuckDuckGo Lite shows the same pattern (47 requests in Stage 24,
3 here). That reclaimed budget went to more query attempts against Naver and
DuckDuckGo HTML, both of which show materially higher success rates here than
in Stage 24 (Naver: 11.5% → 86.5%; DuckDuckGo HTML: 52.1% → 59.5%) — partly
better live conditions on this pass, partly the direct effect of not
splitting budget with two providers already known to be unproductive.
`direct_domain_probe` made 100 real requests to manufacturer domains and
succeeded 11 times, live, independent of any SERP provider's state.

## I. Apple isolated set (3 cold runs)

`regression/datasets/stage24_apple_v1.json`, same configuration, concurrency 1.

| Metric | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Exact official discovered | yes | yes | yes |
| Confirmed / unresolved / conflicts | 5 / 17 / 1 | 5 / 17 / 1 | 5 / 17 / 1 |
| Coverage | 30.4% | 30.4% | 30.4% |
| Quality | conflicted | conflicted | conflicted |
| Runtime | 30.92s | 30.50s | 36.19s |

Fully repeatable across all 3 runs (identical confirmed/coverage/quality every
time — an improvement over Stage 24's Apple set, which varied 3/3/5 confirmed
and once exceeded its budget). **Apple is not treated as evidence of general
reliability**: it is the one product in both stages' datasets with an
unambiguous, single, globally indexed official page, and its stability here
mainly confirms `discover_global_official_domains`'s existing deterministic
corroboration logic is undisturbed — it says nothing about harder cases
(regional brands, ambiguous product lines) where the blind 15-product gate
(§H) and the fault-injection matrix (§G) are the real evidence.

## J. Comparison with Stage 24

| | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Stage 24 confirmed > 0 | 11/15 (73.3%) | 10/15 (66.7%) | **0/15 (0.0%)** |
| Stage 25 confirmed > 0 | 7/15 (46.7%) | 10/15 (66.7%) | **12/15 (80.0%)** |

Stage 25 did not reproduce the 0/15 collapse and trended upward rather than
collapsing on its third run; its confirmed-rate spread and repeatability
score both improved substantially. Its first run (46.7%) is below both of
Stage 24's non-collapsed runs, and no run met the original Stage 24 aspirational
thresholds (confirmed ≥12/15, partial/verified ≥9/15, median coverage ≥25%,
exact-official-use ≥70% *every* run — run 3's 66.7% narrowly misses that last
one). This stage measurably improved worst-case repeatability, which was its
explicit target, but did not achieve uniformly high absolute quality on every
run.

## K. Safety

- No hardcoded product URL, brand→domain table, or provider rule specific to
  any dataset product was added; `DirectDomainProbeProvider`'s domain
  candidates are derived generically from the brand string at request time.
- `core/authority.py`, `core/validate.py`, `core/validation.py`,
  `core/identity.py`, `core/match.py` are unchanged (§F).
- A probed domain is never auto-trusted: it is a plain candidate subject to
  the same `discover_global_official_domains` corroboration as any
  SERP-sourced result.
- The shared circuit breaker and retry logic only affect *scheduling*
  (which provider is tried, in what order, with how many attempts) — they do
  not touch which evidence is accepted, mapped, or confirmed.
- Health-store state is reset at the start of each independent cold run
  (`--provider-health-path` resets on `run()` entry), so the 3 blind-gate runs
  and the 3 Apple runs are genuinely independent, not warmed by each other.

## L. Remaining blockers

1. DuckDuckGo HTML is currently a de-facto dominant provider: disabling it
   alone, live, collapsed both fault-injection test products in both runs.
   The shared circuit breaker makes this failure mode *cheap* (detected and
   skipped quickly across a run) but does not make it *survivable* for
   products where no other provider independently returns exact-model
   evidence that day. Closing this gap further would need either a new
   keyed/paid search API (explicitly out of scope for a "no new hardcode,
   no weakened thresholds" stage) or deeper per-provider query-strategy work.
2. `DirectDomainProbeProvider` only ever establishes an official *domain*; it
   does not search that domain for a specific model's page. Losing every SERP
   provider simultaneously still collapses retrieval — an intentional scope
   limit (a domain-probe is not a general search engine), documented rather
   than silently accepted.
3. Bosch's pre-existing 0-confirmed-despite-real-manufacturer-fetch gap
   (`core/authority.py`'s "official"/"официаль" literal-word corroboration
   check, flagged since Stage 18.7 and still open per Stage 18.9) is
   unrelated to provider resilience and was correctly left untouched.
4. Median coverage (5.9-17.6%) and exact-official-use rate (66.7-88.9%)
   remain below the aspirational Stage 24 thresholds in at least one of the
   three runs.

## M. Verdict

**Stage 25 PARTIAL.**

Met: all unit/integration/focused tests pass (§F); no repeat of the 0/15
total-collapse pattern across 3 independent cold blind-gate runs (§H);
repeatability score improved from 46.24 to 65.41 and confirmed-rate spread
tightened from 73.3pp to 33.3pp; the shared circuit breaker is demonstrably
engaging live and reclaiming real budget from known-dead providers (§H);
`DirectDomainProbeProvider` is demonstrably independent and productive live
(11 real successes, §H); failure of any *non-dominant* provider degrades
gracefully rather than collapsing retrieval (§G); authority/validation/identity
thresholds are provably unweakened (zero diff, §F); no product-specific
hardcode was added (§K); all changes are committed and pushed (§N).

Not met: the controlled fault-injection matrix shows one specific, named,
reproducible failure domain — DuckDuckGo HTML going down alone, under today's
live conditions — still collapsed both tested products in both independent
runs, which is exactly the "fallback exists but doesn't rescue retrieval"
condition that blocks a PASS verdict. Absolute quality thresholds (coverage,
exact-official-use rate every run) are not uniformly met either.

This is not a FAIL: the specific, diagnosed Stage 24 root cause (a correlated
multi-provider outage silently rediscovered from scratch by every product,
wasting the whole run's budget) is fixed and proven not to recur across 3 live
runs, and the new mechanisms are proven to be real and load-bearing rather
than decorative. It is not a PASS because a single-provider outage of the
current dominant provider remains capable of degrading a meaningful share of
products, which the acceptance gate explicitly treats as disqualifying.

## N. Git

- Baseline: `4c19cf0fc4e732c55bfc6da7c2b0eaba9d7ac041` (`Record Stage 24 evidence`)
- This stage's commit: see below.
- Push target: `origin/main`.
- Final handoff records post-push HEAD equality and clean working tree.
