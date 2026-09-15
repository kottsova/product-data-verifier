# Product Data Verifier — Final MVP Regression

Run date: 2026-09-15  
Stage 17 implementation base: `08075147e2bed085a95ba38ccfe3ec66929b7933`  
Continuity cold run: `23a0c02c-4377-4721-b351-34346b836ffc`

## Status

Stages 1-16 remain PASS. Stage 17 tooling, deterministic wiring checks, live
execution, reporting, and deployment rechecks are complete. The final MVP
quality gate is **FAIL**: the current cold continuity sample has no `verified`
or `partial` results and is materially below the Stage 9 reference values.

This is a measurement result, not a benchmark-driven fix. Stage 17 did not
change pipeline thresholds, authority, conflict handling, missing values, or
add rules for any benchmark product.

The full deterministic suite passes: **568 tests in 23.948 seconds**.

## Benchmark

The versioned dataset `mvp-regression-50-v1` contains **50 real products**:
10 cooktops, 10 smartphones, 10 sewing machines, 10 air fryers, and 10 wet/dry
vacuums. It covers 42 brands and the DE, GB, RU, US, and global market modes.
It contains identity/category expectations only, not invented expected specs.

Five distinct products were run live in cold/force-refresh mode. A second warm
run covered the same five to verify cache behavior. The 50-product dataset was
not run in full: the five-product cold run took about seven minutes with two
workers and exposed 170-180 second tail latency. A 300-product live dataset was
not fabricated or run. Dataset validation is covered with a synthetic
300-record scale fixture, while the shipped benchmark remains the curated 50.

Cold configuration: concurrency 2, hard timeout 180 seconds per product,
maximum 5 initial sources, targeted search enabled, dedicated SQLite cache.
Results were atomically checkpointed after every product.

| Cold metric | Result |
|---|---:|
| Selected/live products | 5 |
| Service-completed | 4 |
| Application failures | 0 |
| Timed out | 1 |
| Verified / partial / insufficient / conflicted | 0 / 0 / 4 / 0 |
| Service success rate | 80.0% |
| Average / median coverage | 5.57% / 2.8% |
| Average / median runtime | 129.880 s / 170.157 s |
| Cache hit rate | 0% (`force_refresh`) |
| External discovery/timeout group | 3 (60%) |
| Non-external low-evidence group | 2 (40%) |
| Deterministic/application exceptions | 0 |

The external group is Bosch's hard timeout plus Janome and Gressel returning
`partial` discovery with zero candidates. The low-evidence group is HONOR and
Dreame: discovery found candidates/sources, but the final profiles remained
`insufficient`. These groups are mutually exclusive; `insufficient` is a
successful service outcome, not a technical crash.

The warm run reused four successful cold records and timed out the uncached
Bosch request after its deliberately shorter 30-second boundary: 80% cache hit
rate, 0.002-second median runtime including the timeout record in the aggregate
runtime population. Cold and warm results are stored in separate artifacts.

Runtime JSON artifacts are under ignored `regression/results/`; the committed
dataset schema and Stage 9 reference baseline make future runs reproducible.
The artifacts include timestamps, run IDs, config, per-product fields, stable
source/fetch metadata availability, aggregate metrics, and reference deltas.
Resume skips successful records by default and retries failures/timeouts;
`--keep-failures` supports aggregation without retrying them.

## Five reference products

| Product | Current cold quality | Current coverage | Stage 9 | Delta | Assessment |
|---|---|---:|---:|---:|---|
| Bosch PUE611BB5E | timeout | n/a | 77.8% | n/a | External/operational variability: hard timeout at 180 s |
| HONOR X8d | insufficient | 16.7% | 60.9% | -44.2 pp | Needs deterministic review; discovery found 33 candidates and 4 sources |
| Janome Sakura 95 | insufficient | 0.0% | 68.4% | -68.4 pp | External discovery variability: partial, zero candidates |
| Gressel GAF-1825 | insufficient | 0.0% | 29.4% | -29.4 pp | External discovery variability: partial, zero candidates |
| Dreame G12 Pro HHR32A | insufficient | 5.6% | 66.7% | -61.1 pp | Needs deterministic review; discovery found 12 candidates and 2 sources |

“Needs deterministic review” is a backlog classification, not proof that a
specific internal rule regressed: live source content and accessibility also
changed since Stage 9. Conversely, the large deltas cannot be dismissed as
network-only when usable candidates reached the service. A controlled rerun
with provider diagnostics is required before assigning root cause.

## Telegram end-to-end regression

Deterministic tests exercise:

`Telegram handler -> JobManager -> ProductVerifierService -> cache -> formatter`

The final wiring covers accepted and running messages, final result delivery,
active duplicate suppression, cancellation with discarded eventual result,
an actual service cache hit on the second request, and safe workflow-failure
formatting. It uses a fake workflow/service and no Telegram API or live web.

## Deployment checks

| Check | Result |
|---|---|
| `python app.py` | PASS; exits 0 with the smoke banner |
| CLI parser / service-boundary tests | PASS |
| `python -m healthcheck` | PASS; config, SQLite repository, diagnostics healthy |
| Config validation | PASS |
| Bot construction without polling | PASS; `Application` constructed with handlers and repository |
| `python -m pip check` | PASS; no broken requirements |
| `docker compose config --quiet` | PASS |
| Static Dockerfile/Compose deployment tests | PASS |
| Docker daemon | UNAVAILABLE; client 29.5.3 cannot open `docker_engine` pipe |
| Docker image build / container health smoke | NOT RUN because the daemon is unavailable |

The Compose command also reports that the current user's Docker config file is
not readable, but rendering still succeeds. This does not substitute for a
real build.

## Known limitations

- Live search, manufacturer sites, anti-bot controls, and source documents are
  unstable. Current discovery has both long-tail latency and zero-candidate
  partial outcomes.
- The continuity result is only five live products; the curated 50-product set
  still needs a scheduled, resumable run before population-level conclusions.
- Telegram jobs are process-local and in-memory. Restart loses job state and a
  running worker thread cannot be force-killed cooperatively.
- SQLite is the single-instance MVP cache; it is not a distributed job/result
  store and has no multi-instance coordination.
- Metrics are in-process and reset on restart; there is no external metrics or
  provider-health backend.
- The real Docker build and container health smoke remain unverified while the
  Docker daemon is unavailable.
- Stable service metadata exposes aggregate discovery/fetch counts, not every
  source-level failure. The harness marks unavailable fields instead of
  crossing the service boundary.

## Go / no-go

- **Internal/demo MVP: conditional GO.** The architecture, deterministic
  wiring, cache, failure handling, and reporting are demonstrable, but live
  output must be presented as unstable and often insufficient.
- **Small private Telegram usage: NO-GO for dependable verification; limited
  pilot only.** Operational wiring works, but the current reference quality
  and tail latency do not support a reliable user promise.
- **Public/high-volume production: NO-GO.** Current evidence does not establish
  acceptable live quality, throughput, durable jobs, multi-instance state,
  external metrics, or a verified container build.

## Prioritized backlog

### P0 — blocks dependable use

- Restore a reproducible cold reference outcome: diagnose provider availability
  and the HONOR/Dreame evidence-to-profile path, then rerun the same immutable
  dataset. Keep fixes generic and open them as a post-Stage-17 change.
- Obtain a working Docker daemon and pass image build plus container health
  smoke before any deployment claim.

### P1 — strongly affects quality

- Add provider-level health/retry/backoff observability without leaking
  internals through `ProductVerifierService`.
- Run all 50 products cold with resume and publish category-level confidence,
  evidence, latency, and external-failure findings.
- Investigate category remaining `unknown` despite fetched sources using
  category-generic diagnostics and regression fixtures.

### P2 — improvement and scale

- Schedule a real, curated 300-product dataset/run; do not generate fictional
  identities or ground truth.
- Move jobs/metrics to durable shared infrastructure before multi-instance use.
- Add CI-owned Docker build/health smoke and retained trend artifacts.
