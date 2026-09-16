# Stage 18.2 — Live Quality Recovery

Date: 2026-09-16

Verdict: **FAIL** against the combined Phase 18.2 success criteria.

The P0 reliability controls are implemented and deterministic tests pass. Live
runtime is now bounded, provider failures are observable, and targeted fan-out
is substantially smaller. The live quality/discovery criterion did not recover:
the final cold snapshot still had Google timeouts, DuckDuckGo bot blocks, and a
noisy Naver fallback. Janome and Gressel therefore still had zero accepted
candidates, and no product had a confirmed fact. No authority, validation,
schema, or quality threshold was weakened.

## Files changed

- `core/budget.py`
- `core/discovery.py`
- `core/targeted_search.py`
- `core/workflow.py`
- `services/product_verifier.py`
- `diagnostics/live_quality_diagnosis.py`
- `tests/test_discovery.py`
- `tests/test_targeted_search.py`
- `tests/test_workflow.py`
- `tests/test_live_quality_diagnosis.py`
- `STAGE18_LIVE_QUALITY_RECOVERY.md`

## Implemented P0 controls

### P0.1 — Provider deadlines, circuit breaker, telemetry

- Configurable request-level provider deadlines default to Google 25 seconds,
  DuckDuckGo Lite 8 seconds, and Naver 8 seconds.
- A timed-out/blocked/non-working provider no longer prevents the next provider
  from running.
- A request-local circuit prevents repeated calls after a demonstrated failure.
- Attempts distinguish success, empty, blocked, timeout, parse error, generic
  error, and circuit-open state.
- Telemetry includes provider, query, outcome, result count, duration, applied
  timeout, blocked/timeout/parse flags, exception class, fallback state, circuit
  state, and budget exhaustion.

### P0.2 — Authority-safe normalized discovery

- Google, DuckDuckGo Lite, and Naver share a provider-neutral result record.
- Candidates retain normalized URL, title, snippet, provider, query, discovery
  rank, raw/redirect URL, parse status/confidence, and merged provenance.
- Redirect/tracking cleanup, malformed links, and cross-provider URL dedupe are
  handled before ranking.
- The browser parser uses multiple generic strategies around headings, ancestor
  result blocks, and `href`/`data-href`/`data-url` links. A normal result page
  with no parsed external link is a parse failure, not an empty success.
- Browser readiness now accepts a result heading even when the link is its
  parent or sibling; this closes the layout mismatch found by the reference
  Chromium A/B.
- Search-provider provenance never grants source authority. Authority still
  comes only from the final source URL/domain and existing evidence rules.

### P0.3 — Global wall-clock budget

- Default request budget: 90 seconds, configurable through both workflow and
  stable service requests.
- Remaining time caps discovery provider calls and live fetch timeouts.
- Initial fetch, extraction, targeted discovery/fetch/extraction, and local
  completion check the shared monotonic budget before new work.
- Exhaustion returns the best partial profile and records the exact stage and
  reason. It does not convert timeout/block into success.

Local validation/profile assembly is deliberately allowed to finish after an
external-operation boundary so a usable partial result can be returned. This
caused at most 0.59 seconds of finalization beyond the 90-second live budget.

### P0.4 — Targeted early-stop

- Global product-agnostic cap: 16 executed targeted queries.
- Stops after configurable consecutive zero accepted candidates, zero useful
  facts, duplicate-domain saturation, no new covered fields, or no new
  authority-safe confirmable fields.
- Repeated provider blocks are suppressed by the request-local circuit.
- Query and run telemetry includes accepted candidates, useful facts, new
  domains, coverage gain, confirmable-fact gain, and stop reason.
- “Confirmable” is intentionally used as a targeted-stage proxy: final
  confirmation remains exclusively a downstream validation decision.

## Verification

```text
python -m unittest discover -s tests
Ran 581 tests in 24.129s
OK

git diff --check
clean
```

New deterministic coverage includes provider timeout/fallback and circuit
behavior, full attempt telemetry, parser-layout/redirect/malformed-link handling,
provenance-preserving dedupe, global-budget exhaustion with partial results, and
all targeted early-stop classes used by the implementation.

## Final cold reference-5

Every row is a separate child process with `repository=None`,
`force_refresh=True`, `max_sources=5`, one worker, a 90-second workflow budget,
and a 120-second outer kill boundary. JSON checkpoints under
`diagnostics/results/` are intentionally ignored and are not release artifacts.

| Product | Exact UTC interval | Runtime | Discovery/provider outcome | Accepted / selected / fetched | Category | Expected / extended schema | Mapped | Confirmed / unresolved / conflicts | Targeted q / accepted / fetched / useful | Stop | Coverage | Status |
|---|---|---:|---|---:|---|---:|---:|---:|---:|---|---:|---|
| Bosch PUE611BB5E | 09:35:10.361402–09:35:40.306275Z | 29.945 s | partial; Google timeout, DDG blocked, Naver 1 success then blocked; circuits open | 0 / 0 / 0 | unknown/low | 6 / 14 | 0 | 0 / 6 / 0 | 4 / 0 / 0 / 0 | completed plan | 0.0% | insufficient |
| HONOR X8d | 09:36:45.741366–09:38:16.329416Z | 90.588 s | partial; Google timeout, DDG blocked, Naver 6 successes | 27 / 5 / 5 | unknown/low | 6 / 78 | 79 | 0 / 68 / 0 | 1 / 4 / 4 / 0 | budget at targeted fetch | 0.0% | insufficient |
| Janome Sakura 95 | 09:38:24.231133–09:39:27.391853Z | 63.161 s | partial; Google timeout, DDG blocked, Naver 6 successes | 0 / 0 / 0 | unknown/low | 6 / 14 | 0 | 0 / 6 / 0 | 10 / 0 / 0 / 0 | zero accepted | 0.0% | insufficient |
| Gressel GAF-1825 | 09:39:42.764846–09:40:30.002868Z | 47.238 s | partial; Google timeout, DDG blocked, Naver 6 successes | 0 / 0 / 0 | unknown/low | 6 / 14 | 0 | 0 / 6 / 0 | 7 / 0 / 0 / 0 | completed plan | 0.0% | insufficient |
| Dreame G12 Pro / HHR32A | 09:40:37.914471–09:41:51.884698Z | 73.970 s | partial; Google timeout, DDG blocked, Naver 8 successes | 11 / 5 / 5 | wet_dry_vacuum/high | 18 / 104 | 107 | 0 / 98 / 0 | 10 / 10 / 2 / 0 | zero useful facts | 5.6% | insufficient |

Fetch detail: HONOR had four successes and one error; Dreame had four successes
and one block. All other final rows had no selected source to fetch.

## Stage 9 vs Stage 18.1 vs Stage 18.2

| Product | Stage 9 | Stage 18.1 baseline | Stage 18.2 | Runtime | Accepted | Confirmed | Main remaining bottleneck |
|---|---:|---:|---:|---:|---:|---:|---|
| Bosch | 77.8% | timeout/volatile | 0.0% | 29.945 s | 0 | 0 | External provider volatility; the same implementation previously produced 11 accepted/50%, then the final cold run had Naver block after one noisy result set |
| HONOR | 60.9% | 16.7% | 0.0% | 90.588 s | 27 | 0 | Noisy candidate ranking plus deterministic category/semantic mapping; budget safely stopped targeted fetch |
| Janome | 68.4% | 0.0% | 0.0% | 63.161 s | 0 | 0 | Google/DDG unavailable; Naver returned 114 unique candidates, all correctly rejected as model-absent/noisy |
| Gressel | 29.4% | 0.0% | 0.0% | 47.238 s | 0 | 0 | Google/DDG unavailable; Naver returned 30 unique candidates dominated by person/sports and unrelated pages |
| Dreame | 66.7% | 5.6% | 5.6% | 73.970 s | 11 | 0 | 107 mapped marketing/multilingual facts populate only 1/18 expected fields; targeted stopped at 10 rather than 44 with zero useful facts |

## Concrete evidence and classification

### Fixed in P0

- No reference product hit the 120-second outer timeout.
- HONOR and Dreame no longer run uncontrolled for 170–210+ seconds.
- A failed Google/DDG path is called once; subsequent queries show explicit
  circuit-open attempts with zero duration.
- Dreame targeted fan-out fell from 44 planned/executed attempts in the Stage
  18.1 evidence to 10 executed queries and two unique fetches.
- Provider timeout/block/parse state and candidate provenance are now visible.
- A same-code Bosch run immediately before the final snapshot produced 11
  accepted candidates, 5 selected, 3 successful fetches, category `cooktop`,
  and 50% coverage in 69.235 seconds. The final cold run then produced zero
  accepted after Naver became blocked. This directly proves remaining external
  volatility rather than a deterministic downstream regression.

### Still external

- In the final snapshot Google exhausted 25 seconds before exposing a usable
  result layout and DuckDuckGo returned a bot check for every product.
- Naver was usable but highly noisy and cannot establish official authority.
- Janome's Naver pool included unrelated commerce/docs and a brand-only Janome
  support page without the requested model. Gressel's pool was dominated by the
  footballer name collision. Keeping these rejected is correct.
- The earlier reference Chromium A/B remains relevant: headed Chromium can load
  a normal SERP, but changing headless/browser behavior would be an anti-bot
  transport change and was outside this phase.

### Deterministic P1 issues

- HONOR can fetch and map dozens of facts but category remains unknown and the
  six expected quality fields remain empty.
- Dreame maps 107 raw facts, primarily multilingual/marketing labels, while only
  one of 18 expected fields is found.
- Candidate ranking still permits noisy fallback pages to consume the five-source
  limit for HONOR/Dreame. Authority remains correctly unknown.

## Recommendation for Stage 18.3

Do not change validation, authority, schema, or quality thresholds. Scope Stage
18.3 to generic deterministic improvements only:

1. identity-aware candidate ranking using URL/title/page-identity consistency;
2. category detection from trustworthy product context;
3. multilingual/semantic mapping of real specification labels into existing
   canonical fields;
4. a supported discovery transport that can use the already-hardened normalized
   result contract without anti-bot heuristics or authority promotion.

## Git

No commit or push was made. The instruction conditioned commit/push on a
successful Stage 18.2, and the combined live success criteria were not met.
