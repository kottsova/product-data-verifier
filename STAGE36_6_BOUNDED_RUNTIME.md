# Stage 36.6 follow-up: an enforced 75-second discovery limit

**Verdict: Stage 36.6 remains PARTIAL and PR #1 remains draft.** The timing
defect is fixed: one `discover_name(name)` call now returns in at most
75 s + 6 s grace (81 s). Meeting the time limit says nothing about search
quality or about support pages and documents, which were 0 and 0 before and
are still 0 (see [What is not established](#what-is-not-established)).
The `stage36_7_*` file names only mark this timing follow-up; the stage
number is unchanged.

## 1. Where the time went (Stage 36.6 full run, 8d5865c)

Source: the saved [raw archive](diagnostics/baselines/stage36_6/full_name_only_20260920/raw_sanitized.zip),
analysed by `python -m diagnostics.stage36_7_time_audit` (per-product CSV,
summary and the [32-row overrun table](diagnostics/baselines/stage36_7/time_audit_baseline/overruns.md)
are in `diagnostics/baselines/stage36_7/time_audit_baseline/`). A provider
attempt's start and end are reconstructed from its recorded
`budget_before/after_seconds`; what the trace never recorded is shown as
*unattributed*, not guessed.

| Stage | Seconds, 50 products (total 4,241.4 s) |
| --- | ---: |
| direct official-domain probes | 1,512.5 |
| search providers (DDG, Naver, Seznam, Bing, Google) | 1,350.1 |
| browser official discovery | 554.2 |
| document searches | 32.5 |
| page loads (sum; four run in parallel) | 127.6 |
| unattributed (result assembly, PDF reads, provider/browser cleanup, joins) | 665.7 |

The trace has no cleanup or PDF timer, so those two cannot be separated in
the baseline; the new runs record phases and cleanup (section 5). The
browser's own `browser_navigation_seconds` telemetry is also copied onto
skipped attempts, so it sums to more than the runtime and was not used.

Every one of the 32 overruns falls into one of two mechanisms:

* **A single call ignoring its own deadline (4 rows, 427 s of the 907.5 s
  total overrun):** Milwaukee `direct_domain_probe` ran 214.3 s against a 36 s
  limit; STIHL `google` 248.4 s against 8 s (its error text claims
  "Chromium is unavailable", but the driver had hung for four minutes);
  Einhell `direct_domain_probe` 82.3 s against 36 s; Bosch Professional
  `browser_official_discovery` 67.6 s against 20 s.
* **Time after the last recorded provider call (the other 28):** 9 rows
  spent 21-88 s there (Lenovo 88.1, WD 87.0, MSI 70.5, Kärcher 60.6,
  Crucial 34.5, LG 32.3, Epson 28.3, Roborock 23.3, Kenwood 21.1; together
  385 s of overrun), 10 spent 5-20 s and 13 under 5 s. The call that
  straddles t = 75 s is usually a cheap one whose limit had been clamped to
  the few remaining milliseconds; the overrun is what follows it. Page
  loads cannot start below 8 s remaining and take at most 12 s, so the
  larger tails point at result assembly and provider cleanup, which the old
  trace did not time. That is an inference, not a measurement.

### The four zero-candidate rows and STIHL

Makita, Milwaukee, Bosch Professional and Einhell each got **one query out
of about eleven**: the first provider (`direct_domain_probe`, then the
browser) consumed the whole budget, and DDG, Naver and Seznam either were
never called or answered with a bot check, HTTP 403 or a timeout
(Makita: DDG blocked, Naver 403, Seznam timed out at its clamped
limit). Zero candidates therefore records *"no search was completed"*, not
*"no page exists"*: the Makita page was found on the next run
(`makita.co.nz`, PASS) and STIHL's first-party page on both later runs.
STIHL's 314.6 s was one Playwright/Google call that never returned.

## 2. The limit

Python cannot stop a thread, and `ThreadPoolExecutor.__exit__` waits for the
hung worker; Playwright's sync API is thread-affine and its Node driver and
Chromium are separate processes. Abandoning a thread would leave all of
that running, so the limit is a process boundary
([services/discovery_isolation.py](services/discovery_isolation.py),
[core/process_containment.py](core/process_containment.py)):

* `DiscoveryDebugService.discover_name` (the public route) starts the
  unchanged pipeline in a child interpreter placed in a Windows Job Object
  with kill-on-close (POSIX: its own process group). Driver, browser and any
  grandchild belong to the container.
* Every provider attempt and result, each page fetched and each document
  read is appended to a checkpoint file **as it happens** (a hook on
  `ResilientSearchSession`), so a kill loses at most the call in flight.
* Timeline for the defaults `budget = 75 s`, `grace = 6 s`: the worker
  stops starting work at 75 s; a finished result is written to disk
  *before* provider cleanup; the parent waits at most 2 s for a worker
  that has already saved its result, then kills the tree. If no result
  exists at 78 s (budget + grace/2) the tree is killed, and the remaining
  3 s bound a replay.
* The replay feeds the recorded responses back through the same
  `_discover_inline` pipeline. There is no separate trust logic: ranking,
  authority and identity run the same code. Unfinished work appears as a
  `hard_deadline` provider attempt (`performance.isolation.stopped_in`
  names the provider or page URL that was in flight and for how long).
  Pages that were not fetched in time are `hard_deadline`, so their
  candidates stay unverified. If the replay cannot finish in its window it
  degrades to an unranked list of every collected candidate with no verdict
  (`replay_incomplete`); it can lose information, never grant authority.
* The worker leaves with `os._exit` after its result is saved and the
  parent always sweeps the container, so the Playwright driver pipe is
  never written to after its reader is gone. Worker `stderr` is captured and
  scanned for `EPIPE`.

Measured limits (Windows 11, Python 3.14): a hard stop returns at
75 + 3 s + the replay window, so no later than 81 s plus scheduling slack.
The worst call in the final full run took **81.108 s**, 0.108 s beyond the
81 s limit; the tests allow 1.5 s of slack. The fixed cost of the process
boundary is about 0.7 s per call (measured on a failing smoke run; startup
imports dominate). Only the Job Object path was exercised; the POSIX
process-group path is written and unverified.

## 3. Tests that reproduce it

`tests/test_stage36_7_hard_deadline.py` uses real child processes and real
clocks (17 tests, about 105 s) and asserts elapsed time, surviving pids
(recorded by the test hooks and by the containment layer) and the partial
result:

* provider that never returns: stopped in the documented window, provider
  named, the candidate returned by the earlier provider of the same query is
  kept, worker and its stray child dead;
* page load that never finishes: `page_fetch` in flight, URL named,
  candidate kept as unverified;
* shutdown that would take 600 s with a leftover child: result returned in
  under the budget, `post_result_cleanup`, no survivors;
* **real Playwright Chromium** whose shutdown never completes: driver and
  browser processes are contained and gone, no `EPIPE`;
* four concurrent requests: total time about one budget (not four), four
  distinct process trees, all gone;
* worker crash: structured `worker_exited_without_result` with the last
  traceback line;
* a page whose analysis takes seconds of CPU cannot stretch the return time,
  and a replay that cannot finish degrades without granting trust.

## 4. Authority and identity are unchanged

`tests/test_stage36_7_decision_equivalence.py` runs the ten manually accepted
pages and the seven exclusions through (a) the inline pipeline as before,
(b) the worker route and (c) a replay of the recorded checkpoints, and
compares status, exact-official flag, every source's authority/model
match/role/evidence and the fetch identity relation. They agree for all 17
(TP-Link distributor, Roborock accessory, Razer HyperSpeed, Corsair SHIFT,
CeraVe refill, Oral-B twin pack, NETGEAR forum stay excluded; every accepted
page stays verified). A truncated checkpoint that lost the page fetch never
produced an exact official page for any exclusion. In the live full run below
none of the seven excluded URL patterns is in an official group. The suite
(1,177 tests) passes. No authority rule, seed or threshold was edited; the
only change to ranking/matching code is an `lru_cache` on the pure
`normalize_model`.

## 5. Live results

All runs used the archived strings through `discover_name(name)` with no
category. Rows are per product in the linked CSV; "blocks" are provider bot
checks/403s, kept apart from program faults (worker error, leftover
process, `EPIPE`, row exception).

* [Limited run](diagnostics/baselines/stage36_7/limited_live_20260921/comparison_summary.json)
  (16 products: the 11 largest overruns plus Siemens, Braun, Razer, Corsair,
  CeraVe): all finished in **80.3 s or less** (old maximum 314.6 s); three of
  the four old zero-candidate rows (Makita was not in this run) now have
  43-170 candidates.
* **Run 1** ([archive](diagnostics/baselines/stage36_7/full_run1_pre_replay_bound_20260921/comparison_summary.json),
  commit 2607eec): 17 over 75 s, maximum **97.0 s**. The killed worker was
  gone at 80 s; the extra 5-17 s was the parent's replay re-analysing
  seconds of page HTML with no limit of its own. That is a defect of my first
  implementation; the replay window and the degraded fallback fixed it. The
  run is kept as evidence and is not counted in the comparison below.
* **Run 2** ([archive](diagnostics/baselines/stage36_7/full_name_only_20260921/comparison_summary.json),
  commit 6b4d788, [sanitised raw and hashes](diagnostics/baselines/stage36_7/full_name_only_20260921/manifest.json)):

| | Stage 36.6 (8d5865c) | Run 2 |
| --- | ---: | ---: |
| PASS / PARTIAL / FAIL | 9 / 34 / 7 | 15 / 34 / 1 |
| Over 75 s | 32 | 10 (all in grace, 75.9-81.1 s) |
| Maximum / median seconds | 314.6 / 78.0 | 81.1 / 61.0 |
| Total service seconds | 4,241.4 | 2,800.8 |
| Zero-candidate products | 4 | 0 |
| Unique candidates | 2,640 | 3,787 |
| Queries attempted | 309 | 362 |
| Page fetches / loaded | 108 / 44 | 119 / 76 |
| Support pages / documents | 0 / 0 | 0 / 0 |
| Provider bot-check blocks | 226 | 183 |
| HTTP 403 on page fetch | 5 | 22 |
| Provider timeouts (network) | 66 | 50 |
| Program faults (worker error, leftover process, `EPIPE`) | 0 | 0 |

Stop reasons in run 2: 44 rows completed normally; 5 rows saved a complete
result and their provider/browser shutdown was cut by the 2 s cap (2.0 s,
median cleanup 0.30 s); **1 hard stop** (Samsung RB38C7B6AS9, 81.1 s):
its replay did not fit the 2.4 s window, so it returned the 47 collected
candidates unranked and unverified. No process survived in 116 worker runs
(50 + 50 + 16) and no worker `stderr` contained anything, so the old post-run
`EPIPE` did not appear.

New PASS rows (Philips HD9876/90, Braun MQ 9187 XLI, ASUS RT-BE88U, Epson
L6270, Makita GA023GZ, STIHL MS 182) were each opened live: a first-party
product page whose heading/title names the requested model. Nike's accepted
URL moved from the `/gb/` to the `/dk/` locale of the same product page.
Namazu went PARTIAL to FAIL with 11 candidates instead of 29 because DDG
(4 bot checks), Naver and DDG-Lite blocked this run, not because of a
program fault.

**These changes cannot be credited to the limit.** The baseline and the new
runs were made hours apart on different network conditions (Kärcher took
105.5 s and then 41.4 s with identical code paths, and providers block
differently each time). The controlled effects are the bound on time and the
absence of leftover processes and `EPIPE`; candidate, PASS and FAIL counts
are observations. That the FAIL rows caused by "one provider ate the budget"
are gone is what the limit is expected to do, but the same rows can also
change with the network alone.

## What is not established

* Support pages and documents: 0 and 0 in both runs. Nothing in this step
  addressed retrieval or extraction of them; that needs its own evaluation.
* Provider calls still overrun their own deadlines *inside* the worker
  (`direct_domain_probe`, `browser_official_discovery`, Google/Playwright).
  The outer limit contains the damage but a stuck first provider still
  starves later queries until 78 s. Per-provider abandonment (a bounded
  helper process per provider) is the next candidate fix.
* One CPU-bound path is slow on some pages: `category_from_primary_product`
  took 11.6 s on a synthetic 3.4 MB page (real pages measured about 1 s of
  analysis). Only the process boundary contains it.
* Nine rows still finish 75.9-78.5 s after their cooperative deadline, and
  roughly 1 in 10 browser shutdowns needs the 2 s kill. Hard stops with an
  incomplete replay return an unranked list; how often that happens under
  worse networks is one row in fifty here.
* POSIX containment is untested; the machine-specific measurements above are
  from one Windows host and two network conditions.
* The 11 historical unknown transitions from the earlier report remain
  unknown. Stage 36.6 stays PARTIAL; Stage 37 and retailer fallback remain
  deferred.
