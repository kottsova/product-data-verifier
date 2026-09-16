# Stage 18.6 — Live Quality Gate & Conflict Resolution Audit

Date: 2026-09-16

Verdict: **PASS** (audit + localization, not a numbers-improvement stage).

Scope was audit-only. One deterministic bug was found during the Janome
conflict audit (a schema alias collision, not a validation/authority/
threshold change) and was fixed tests-first. No other production code was
touched. No threshold, validation rule, or authority rule was weakened.

## 1. Baseline verification

Before any live run: `python -m unittest discover -s tests` → 618/618 PASS,
`git diff --check` clean, `py_compile` clean on `core/`, `services/`,
`adapters/`, `bot/`. HEAD and `origin/main` both `36b4c200773045cb5717c06a4f97a1faab9dc331`,
working tree clean.

## 2. Dreame recheck — two independent cold runs

Both runs: separate process, `repository=None`, `force_refresh=True`,
`max_sources=5`, 90 s workflow budget, 120 s outer timeout.

| Run | UTC interval | Runtime | Discovery outcome | Fetch attempted | Category | Coverage |
|---|---|---:|---|---:|---|---:|
| 1 | 12:45:40.947–12:47:11.696 | 90.748 s | partial; all 6 base + 2 official Google queries **succeeded** (8/9–24.0 s each = 90.16 s total), then a final DuckDuckGo HTML call timed out | 0 | unknown/low | 0.0% |
| 2 | 12:47:18.714–12:48:49.378 | 90.663 s | partial; same pattern — 6 Google queries succeeded (9.4–24.0 s each), then the 7th Google call and a DuckDuckGo HTML call both timed out | 0 | unknown/low | 0.0% |

Both runs: `budget.exhausted_stage = "initial_discovery"`,
`exhaustion_reason = "Insufficient workflow budget to start initial_discovery: 0.000s remaining."`
32 and 30 candidates were accepted respectively (mostly retailer/marketplace
domains; the real `ru.dreametech.com` / `global.dreametech.com` /
`ge.dreametech.com` manufacturer pages were present in the accepted set both
times but never reached fetch). Fetch, extraction, mapping, and validation
never ran — the entire 90 s budget was consumed inside discovery itself,
because every Google query this run *succeeded* (so the failure-triggered
circuit breaker never opened) but took 9–24 s per call through the headless
browser transport.

This is a different failure shape than Stage 18.5's diagnosis (Qrator/403/
captcha on selected *sources*): here the selected sources were never even
fetched.

## 3. Dreame loss-point diagnosis

**External, not deterministic.** Evidence:

- A third, fully independent cold run of Dreame was executed ~4 minutes
  later as part of the Part 3 five-product gate (12:54:33.124–12:55:50.647Z,
  77.522 s). In that run the *first* Google query timed out at its 25 s
  deadline, the request-local circuit opened immediately, and the remaining
  7 queries fell through to DuckDuckGo HTML/Naver at 1–3 s each — the
  historically normal pattern from Stage 18.4/18.5. Discovery finished in
  well under 90 s, leaving budget for fetch: category correctly resolved to
  `wet_dry_vacuum/high`, one source (`dreame.ua`) fetched successfully,
  four were blocked (captcha/403/bot_challenge), and coverage reached
  **44.4% (8/18 fields)** — consistent with the Stage 18.4 Dreame repeat
  (44.4%).
- No code changed between any of the three runs (clean tree, HEAD unchanged
  until the Janome fix below, which does not touch discovery/fetch/wet_dry_vacuum
  code paths).
- The provider-deadline, budget, and circuit-breaker mechanisms all behaved
  exactly as designed in every run: a *slow-but-successful* provider is
  correctly not treated as broken, so the circuit never opens for it. That
  is deliberate Stage 18.2 behavior, not a bug. The problem is that Dreame's
  fixed query plan (6 base + 2 official queries) has no independent time cap
  distinct from the shared workflow budget, so if Google answers slowly on
  every single query in a run, discovery alone can consume the entire
  budget. This is a legitimate architectural gap worth flagging (see §14),
  but it was already present in this exact same shape at Stage 18.2/18.4 and
  is not a new regression — it simply was not the specific manifestation
  observed then (Google failing fast was the previously-seen pattern).

Confidence: **high**. Reproduced identically twice (structural), then
reproduced the *recovery* once under the historically normal external
profile, in the same session, with the same code.

## 4. Janome conflict audit

Two live snapshots were used, both under identical conditions
(`force_refresh=True`, no cache, `max_sources=5`, 90 s budget, 120 s outer
timeout):

- `diagnostics/results/stage18-5b-live-janome.json` — 2026-09-16
  12:00:02.934–12:01:33.154Z, produced under the current HEAD
  (`36b4c20`, the Stage 18.5 follow-up), coverage 63.2%, confirmed 11,
  conflicts 1. This is the exact case cited in the Stage 18.6 brief.
- A fresh cold run inside today's Part 3 gate — 2026-09-16
  12:52:30.583–12:53:39.980Z — returned a *different* candidate set this
  time (`janome.club` success, `dns-shop.ru` ×2 blocked/`bot_challenge`,
  `chipdip.ru` 404, `sewing-world.ru` success); `janome-official.by` was not
  discovered/selected at all in this run, so the conflict scenario below
  did not recur here — 0 conflicts, but also 0 confirmed (10.5% coverage).
  This is ordinary discovery-candidate volatility (see §9), not related to
  the conflict fix.

### The one conflict (from the 18.5.1 snapshot)

- **Field**: `accessories` (sewing_machine category, priority medium).
- **Resolution reason**: `conflicting_equal_authority`.
- **Both** supporting and conflicting facts trace to the **same single
  source**: `https://janome-official.by/shveynye-mashiny/elektromehanicheskie-shveynye-mashiny/shveynaya-mashina-janome-sakura-95-kupit`
  (authority `verified`, source_type `manufacturer`/`official_document`
  equivalent — rank 4 both sides, hence "equal authority").
- **Raw values**:
  - `raw_label="Стандартная комплектация"` → `"Педаль, руководство по
    эксплуатации (инструкция), Шпульки пластиковые 3шт. (2шт. и 1 в
    машине) Набор игл Organ - 3 шт. Вспарыватель"` (pedal, manual,
    bobbins, needle set, seam ripper).
  - `raw_label="Лапки в комплекте"` → `"Лапка стандартная Лапка для
    вшивания молнии Лапка для выметывания петли Лапка для потайного шва"`
    (standard foot, zipper foot, buttonhole foot, blind-hem foot).
- Both raw labels mapped via `mapping_reason="exact_alias:accessories"` —
  `core/schema.py` had aliased **both** distinct Russian labels to the
  single canonical field `accessories` (`value_type="list"`).
- `janome.club`, the other fetched source in that run, had **no**
  accessories-related row at all (checked directly in the raw extraction
  trace) — so this was never a cross-source disagreement.
- `core/validation.py`'s `normalize_value()` has no special case for
  `value_type="list"`; it falls through to plain text comparison. Two
  different list-string blobs from the *same* source, mapped to the *same*
  canonical field, therefore compared as unequal text and were classified
  as a same-authority conflict.

### Classification

**D — field semantic mismatch (schema alias bug).** "Standard kit
contents" and "included presser feet" are two different real-world
attributes that a single manufacturer page commonly states side by side.
Aliasing both to one canonical field was the mapping mistake; the values
were never in genuine disagreement, so this is not case A (genuine
authoritative disagreement) and not a validation bug to work around.

### Fix (RED → GREEN)

- RED: `tests/test_schema.py::test_sewing_accessories_and_presser_feet_are_distinct`
  asserted `included_presser_feet` exists as a canonical field for
  `sewing_machine` and that `"Лапки в комплекте"` resolves to it separately
  from `"Стандартная комплектация"` → `accessories`. Failed before the fix
  (`included_presser_feet` did not exist; both labels aliased to
  `accessories`).
- Fix: `core/schema.py` — removed `"лапки в комплекте"` from the
  `accessories` alias tuple and added a new sibling definition,
  `included_presser_feet` (`value_type="list"`, `priority="medium"`), with
  aliases `"included presser feet"`, `"presser feet included"`, `"лапки в
  комплекте"`. No validation, authority, or threshold code changed.
- GREEN: same test now passes; full `tests/test_schema.py`,
  `tests/test_mapping.py`, `tests/test_validation.py` (73 tests) pass; full
  suite 619/619 PASS; `git diff --check` clean.
- This is not a live-reproduction of the exact conflict again (today's live
  run happened to not fetch `janome-official.by`, so the scenario could not
  recur in this session) — the proof is the deterministic unit test, which
  is the correct level of evidence for a schema-definition bug.

## 5. Bosch result

| Stage | Coverage | Confirmed | Notes |
|---|---:|---:|---|
| Stage 9 | 77.8% | — | pre-Stage-18 baseline |
| Stage 18.4 | 44.4% (8/18) | 0 | `bosch-home.co.uk` ×2 + `bosch-home.com` fetched successfully, `galaxus.ch` blocked (403), `otto.de` error |
| Stage 18.5 | 44.4% | **8** | same fetch pattern, but the SERP "official site" bootstrap succeeded that run, seeding authority corroboration |
| Stage 18.6 (today) | 44.4% (8/18) | 0 | 12:50:06.618–12:50:59.037Z, 52.419 s. Identical fetch pattern (`bosch-home.co.uk` ×2 success, `bosch-home.com` success, `galaxus.ch` 403, `otto.de` 400/error). `Bosch official website` query routed to Naver only (109 results); none were recognized as an "official site" snippet, so no domain was seeded into the trusted-source cache. Per `core/authority.py`, an elevated role (`manufacturer`) requires independent corroboration from an already-trusted source in the *same run* — without a seed, even the genuine `bosch-home.com` page stays capped at `unknown`/`retailer`. 0 confirmed despite real manufacturer pages being fetched. |

## 6. Janome result

| Stage | Coverage | Confirmed | Conflicts | Notes |
|---|---:|---:|---:|---|
| Stage 9 | 68.4% | — | — | |
| Stage 18.4 | 10.5% (2/19) | 0 | 0 | `janome.club` + `sewing-world.ru` fetched; `dns-shop.ru` ×2 + `market.yandex.ru` blocked |
| Stage 18.5.1 (snapshot used for audit) | 63.2% | 11 | 1 | `janome-official.by` reached `verified` manufacturer authority via corroboration; 1 false conflict (§4) |
| Stage 18.6 (today) | 10.5% (2/19) | 0 | 0 | 12:52:30.583–12:53:39.980Z, 69.397 s. Different candidate set: `janome.club` success, `dns-shop.ru` ×2 `bot_challenge` (401), `chipdip.ru` 404, `sewing-world.ru` success; `janome-official.by` not discovered this run. |

Discovery-candidate volatility (which live sources a given SERP round
returns) — not a regression — explains the swing between 10.5% and 63.2%.
The one deterministic issue found (the accessories/presser-feet alias
collision) is fixed.

## 7. Gressel result

| Stage | Coverage | Confirmed | Notes |
|---|---:|---:|---|
| Stage 9 | 29.4% | — | |
| Stage 18.4 | 0.0% | 0 | `gressel.ru` ×2 404, `dns-shop.ru` login-required, `market.yandex.ru` blocked |
| Stage 18.5 | 17.6% | 0 | |
| Stage 18.6 (today) | 0.0% (0/17) | 0 | 12:53:40.483–12:54:32.587Z, 52.104 s. `gressel.ru` ×2 404 (manufacturer's own product pages are gone/moved), `dns-shop.ru` ×2 `bot_challenge` (401), `market.yandex.ru` captcha (200-with-challenge-body). Zero successful fetches. Targeted search hit the global 16-query cap with no accepted candidates. |

Gressel's bottleneck is squarely fetch/access: the manufacturer's own listed
URLs 404, and every retailer/marketplace alternative is bot-blocked. This
matches Stage 18.4's finding almost exactly — no deterministic regression.

## 8. HONOR result

| Stage | Coverage | Confirmed | Notes |
|---|---:|---:|---|
| Stage 9 | 60.9% | — | |
| Stage 18.4 | 47.8% (11/23) | — | |
| Stage 18.5 | 47.8% | 0 | |
| Stage 18.6 (today) | 47.8% (11/23) | 0 | 12:50:59.751–12:52:29.970Z, 90.219 s (budget-exhausted at `initial_fetch`, 0.110 s remaining — not the 120 s outer kill). `honorstore.ru` success, `honor.com/global/...` success, `honor.com/ie/...` error, `honor.com/eurasia/...` success. Three real manufacturer/regional pages fetched successfully, but — same mechanism as Bosch — no SERP "official site" corroboration seed this run, so authority stayed capped and confirmed remained 0. |

Numbers are stable and reproduce Stage 18.4/18.5 almost exactly. The
dominant remaining bottleneck is authority insufficiency, not discovery or
fetch.

## 9. Dreame result

See §2–3. Coverage swung 0.0% / 0.0% / 44.4% across three cold runs in one
session, fully explained by external Google discovery-latency variance.
When discovery completes with budget to spare, category detection, fetch,
and mapping all work (8/18 fields); confirmed stayed 0 in the 44.4% run for
the same authority-corroboration reason as Bosch/HONOR (only one source,
`dreame.ua`, fetched successfully — insufficient for a strong/confirmed
fact without an elevated role).

## 10. Remaining bottleneck matrix

| Product | Discovery | Fetch/access | Authority | Extraction/mapping | Conflict | External evidence availability |
|---|---|---|---|---|---|---|
| Bosch | OK | Partial (3/5 success) | **Dominant** — no SERP corroboration seed this run | OK (8/18 mapped) | none | sufficient when fetched |
| Janome | Volatile (different candidates run-to-run) | **Dominant this run** (bot_challenge/404) | OK when `janome-official.by` is reached (verified via corroboration) | OK | fixed (§4) | sufficient when reached |
| Gressel | OK | **Dominant** — manufacturer URLs 404, retailers bot-blocked | n/a (nothing fetched) | n/a | none | manufacturer's own listed pages appear stale/moved |
| HONOR | OK | Partial (3/4 success) | **Dominant** — same corroboration-seed gap as Bosch | OK (11/23 mapped) | none | sufficient when fetched |
| Dreame | **Dominant in 2/3 runs** — discovery alone can exhaust the 90 s budget when Google is slow-but-successful | Partial when discovery leaves budget (1/5 success) | Dominant in the successful run (same corroboration gap) | OK (8/18 mapped) | none | sufficient when reached |

Cross-cutting observation: in every run this session where fetch succeeded
at all (Bosch, HONOR, Dreame's 44.4% run), the `Bosch/HONOR/Dreame official
website` SERP bootstrap query itself failed to produce a recognized
"official site" snippet (Google was circuit-broken/slow across the board
today), so **no page could be elevated above `unknown`/`retailer`
authority even when it was the genuine manufacturer's own domain** — hence
confirmed stayed 0 in every product today except the older Janome 18.5.1
snapshot. This is not new or permanent: Stage 18.5's own Bosch run (same
day, different hour) got the SERP bootstrap to succeed and reached 8
confirmed facts with the identical fetch pattern. It is a single external
dependency (one successful Google "official site" SERP round) that the
entire authority-corroboration mechanism currently relies on as its only
seed — worth flagging as a future scope, not a bug (§14).

## 11. Deterministic changes

- **Bug**: `core/schema.py` aliased two distinct Russian labels
  ("standard kit contents" and "included presser feet") to one canonical
  `accessories` field. A single source stating both produced a false
  same-source "conflict".
- **RED test**: `tests/test_schema.py::test_sewing_accessories_and_presser_feet_are_distinct`.
- **Fix**: split into `accessories` and a new `included_presser_feet`
  canonical field (`core/schema.py`), scoped to `sewing_machine` only.
- **GREEN**: focused tests (73) and full suite (619/619) pass.

No other production code changed. No validation, authority, mapping-engine,
or quality-threshold logic was touched.

## 12. Runtime/reliability

- No run (2 Dreame + 5 full-gate) reached the 120 s outer kill timeout.
  Longest runtime was 90.663 s (Dreame run 2), bounded by the 90 s workflow
  budget plus ≤0.7 s of allowed post-budget finalization, consistent with
  Stage 18.2's documented behavior.
- The provider circuit breaker opened correctly on genuine failures
  (timeout/blocked) in every run and was correctly *not* triggered by a
  slow-but-successful provider (Dreame runs 1–2) — working as designed.
- No retry explosion observed in any provider-attempt trace.
- Global 16-query targeted-search cap was hit cleanly once (Gressel,
  `global_query_limit`).

## 13. Tests

- `python -m unittest discover -s tests`: **619/619 PASS** (618 baseline +
  1 new RED→GREEN test).
- `git diff --check`: clean (only CRLF/LF advisory warnings, no whitespace
  errors).
- `py_compile` over `core/`, `services/`, `adapters/`, `bot/`: clean.

## 14. Final MVP quality assessment

No thresholds changed; no new scoring introduced. Under the existing gate,
none of the five reference products currently produce a `verified` or
`partial` result in a cold run — all five are `insufficient` today, exactly
as in Stage 18.4/18.5. This is unchanged by Stage 18.6. What Stage 18.6
adds is precise localization:

- Bosch, HONOR, and Dreame (when discovery completes) reliably **discover
  and fetch the real manufacturer/official pages** and **extract/map**
  real facts (44–48% coverage), but cannot reach `confirmed` status because
  the authority-corroboration mechanism needs an independent SERP-verified
  seed domain that a circuit-broken/slow Google did not provide in most of
  today's runs. This is the dominant, precisely identified limiter for
  these three.
- Gressel is limited purely by external fetch/access: its own manufacturer
  URLs return 404 and every fallback is bot-blocked.
- Janome swings between the same two limiters (fetch/access volatility,
  and — before today's fix — a false conflict when its distributor page
  was reached); the conflict cause is now fixed.
- Dreame's specific new finding this stage is that a *successful-but-slow*
  Google can, by itself, consume the entire workflow budget during
  discovery before fetch ever starts — distinct from, and in addition to,
  the previously diagnosed fetch-blocking causes.

None of this is a threshold, validation, or authority regression. The
pipeline is deterministic-safe: identical code produced identical
structural behavior across all cold runs, with the differences fully
explained by which external round trips (Google SERP timing/availability,
retailer bot-challenges, manufacturer URL staleness) succeeded in a given
run.

## 15. Recommendation for next scope (not started)

Two independent, generic (non-product-specific) improvements are now
precisely justified by this audit's evidence, in priority order:

1. **Authority corroboration seed diversity.** Today, an elevated role can
   only be seeded by a Google-recognized "official site" SERP snippet. When
   Google is slow/blocked (which happened for all five products in several
   of today's runs), no source — not even the literal manufacturer
   domain — can ever be confirmed, regardless of fetch success. Recovering
   a corroboration seed from DuckDuckGo/Naver results, or from strong
   structural signals on an already-successfully-fetched page, would remove
   this single point of failure. This directly explains most of today's
   `confirmed=0` results.
2. **A soft discovery-stage time cap independent of the shared workflow
   budget**, so that a run where every discovery query happens to succeed
   slowly (Dreame runs 1–2 today) cannot by itself consume the entire
   budget before fetch gets a chance to run.

Both are analysis-backed by this stage's evidence; neither was started
here, per the audit-only mandate.

## 16. Git

- HEAD before this stage: `36b4c200773045cb5717c06a4f97a1faab9dc331`.
- Deterministic fix applied and verified (619/619 PASS, clean diff-check).
- Commit created for the schema fix; pushed to `origin/main` because Stage
  18.6's combined PASS criteria (deterministic safety, Dreame condition 1,
  Janome conflict resolved, all five bottlenecks localized) were met.
- Working tree clean after commit. `diagnostics/results/*.json` from this
  stage's live runs are gitignored and were not committed, matching prior
  stage convention.
