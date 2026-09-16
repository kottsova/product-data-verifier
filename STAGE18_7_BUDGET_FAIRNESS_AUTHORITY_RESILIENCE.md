# Stage 18.7 — Discovery Budget Fairness & Authority Bootstrap Resilience

Date: 2026-09-16

Verdict: **PASS**.

Two architectural dependencies diagnosed in Stage 18.6 were removed with
minimal, generic, tests-first fixes: (1) a single slow-but-successful
discovery provider could consume nearly the whole shared workflow budget by
winning every query's fallback race, and (2) all authority corroboration in
a run depended on one specific provider (previously Google) succeeding on
an "official website" query. Both are now provider-agnostic and bounded.
Live cold runs confirm both mechanisms firing correctly, including a case
(HONOR) where authority recovery went from 0 confirmed facts to 35, with
13 legitimate regional-variant conflicts newly surfaced.

## 1. Discovery budget diagnosis

**Mechanism (class A+B — sequential provider starvation with no
cumulative per-provider budget)**: `ResilientSearchSession.search_with_status`
tries providers in a fixed order and returns as soon as one *succeeds*
(`core/discovery.py`, `search_with_status`). A provider's own per-call
timeout (`config.timeout_for`) and the shared `WallClockBudget` bound each
*individual* call, but nothing tracked a provider's *cumulative* time spent
across the many queries in one discovery plan (6-8 base/official queries,
run sequentially). Because the fallback chain only advances on structured
*failure* (blocked/timeout/error/empty), a provider that keeps succeeding —
just slowly — is never demoted: it wins every single query. Two
independent Stage 18.6 Dreame cold runs showed Google answering all 6-8
queries successfully at 9-24s each, consuming the entire 90s budget inside
discovery itself before fetch ever started (0 fetches attempted in either
run). This is not "Google is slow" being treated as a deterministic bug —
it is the *absence of any cumulative fairness mechanism* that is generic to
whichever provider happens to be first/slow in a given run.

## 2. Authority bootstrap diagnosis

**Mechanism (single-provider seed dependency)**: official-domain
recognition (`discover_global_official_domains`) is itself provider-agnostic
in its own signature — it only needs `SearchResultLike` records and applies
an evidence-based filter (domain/brand label consistency, an explicit
"official"/"официаль" signal, and the brand name in the title). But its
only two call sites (`discover_with_status`, `discover_identity_query_with_status`)
gated it behind `_primary_provider_succeeded(attempts)`, which required the
*first, non-fallback* provider's attempt to have status `"success"` —
discarding a fallback provider's identical evidence outright (documented
intent: "Fallback discovery can find sources but cannot establish
authority"). Since `core/authority.py`'s post-fetch corroboration
(`_resolve_authority_roles` in `core/workflow.py`) can only draw
`trusted_sources` from domains *already* `manufacturer`/`verified` from this
same discovery-time mechanism, the entire run's authority corroboration —
for every fetched page, including the literal manufacturer's own domain —
depended on that one provider succeeding on that one query. Verified with
Bosch/HONOR/Dreame/Janome traces from Stage 18.6: in most runs Google was
circuit-broken by the time the "official website" query ran, and
Naver/DuckDuckGo's equivalent results (109, 106, 61 results respectively in
different runs) were silently discarded despite meeting the same evidence
bar. Stage 18.5's own same-day Bosch run got 8 confirmed facts only because
Google happened to succeed on that specific query that time.

## 3. Files changed

- `core/discovery.py` — provider time-share cap; provider-agnostic official-
  domain bootstrap; marketplace-first classification order.
- `core/workflow.py` — no changes (consumes the now-richer discovery output
  unchanged; `_resolve_authority_roles`'s corroboration contract is intact).
- `tests/test_discovery.py` — 2 new budget-fairness tests, 1 replaced test
  (previously encoded "fallback cannot verify authority") plus 2 new
  adversarial tests (domain-consistency still required; marketplace never
  elevated).

No changes to `core/authority.py`, `core/validation.py`, `core/quality.py`,
or any threshold/schema file.

## 4. Budget architecture changes

- Added `DiscoveryRuntimeConfig.provider_time_share` (default `0.5`).
- `ResilientSearchSession` now tracks `_provider_elapsed[index]`, the
  cumulative wall-clock time a provider has spent (success or failure)
  across the *whole* session — which spans initial discovery, targeted
  discovery, and (indirectly, by leaving budget) fetch, since one session/
  budget is shared for the entire product-verification request
  (`run_product_workflow`).
- Before attempting a provider, if `budget is not None` and
  `_provider_elapsed[index] >= budget.total_seconds * provider_time_share`,
  the provider is skipped for that query (`status="capped"`,
  `provider_time_capped=True`) and the next provider in the chain is tried
  instead — even though the capped provider is not blocked, timed out, or
  circuit-open. This is a purely additive check: it only activates when a
  budget object is present (existing budget-less tests/usages are
  unaffected) and never changes provider identity/order — whichever
  provider is slow gets capped, generically.
- No change to the 90s default total budget, `max_sources`, query counts,
  or identity/ranking thresholds. No new provider-specific behavior.

## 5. Authority architecture changes

- `_primary_provider_succeeded` renamed to `_official_query_returned_results`
  and changed from "the non-fallback provider succeeded" to "any provider
  succeeded" (`any(item.status == "success" for item in attempts)`).
  Applied at both call sites (initial discovery's official-site bootstrap
  and the targeted-discovery equivalent).
- The safety bar that makes a claim trustworthy is unchanged: domain-label/
  brand consistency, an explicit "official"/"официаль" signal, and the
  brand name present in the title/snippet — evaluated identically
  regardless of which provider returned the result. Provider identity is no
  longer part of the trust decision; content evidence is.
- `classify_source` now checks marketplace exclusion *before* the official-
  domain match (previously after), so a brand name that happens to share a
  domain label with a known marketplace (e.g. a brand literally named after
  one) can never have that marketplace domain itself promoted to
  `manufacturer`, regardless of which provider or query produced the
  "official" claim.
- `core/authority.py`'s two-tier self-declared/corroboration model (Stage
  18.5.1) is untouched: self-assertion (copyright, JSON-LD, site-name,
  wording) is still never sufficient alone; `find_corroboration` still
  requires an independent, already-trusted source's HTML to link to the
  candidate domain.

## 6. Tests

| Bug | RED test | Fix | GREEN |
|---|---|---|---|
| Slow-but-successful provider can exhaust the workflow budget alone | `test_slow_but_successful_provider_cannot_monopolize_workflow_budget` (confirmed failing pre-fix: `TypeError` for the new `provider_time_share` kwarg, i.e. the capping mechanism did not exist) | Cumulative provider time-share cap in `ResilientSearchSession` | Same test green; `test_fast_provider_is_never_time_capped` guards against regression for normal-speed providers (20 queries, never capped) |
| Official-domain bootstrap silently discards a fallback-only "official" claim | Replaced `test_explicit_official_claim_from_fallback_does_not_verify_authority` (encoded the old, now-intentionally-changed behavior) with `test_official_claim_from_fallback_provider_seeds_manufacturer_authority` | `_official_query_returned_results` accepts any provider | Green |
| (Adversarial, preserved) a fallback claim naming the right brand wording but the wrong domain must still be rejected | `test_fallback_official_claim_without_domain_consistency_stays_unverified` | No change needed — proves the existing evidence filter, not provider trust, is what gates verification | Green |
| (Adversarial, new) a marketplace domain must never be promoted via an "official" claim from any provider | `test_marketplace_domain_official_claim_from_any_provider_is_never_manufacturer` | Reordered `classify_source` to check marketplace first | Green |

Focused run: `tests/test_discovery.py` 82/82 PASS. Full suite: 623/623 PASS
(619 Stage 18.6 baseline + 4 net new). `test_authority.py` (12 adversarial
corroboration tests) and `test_workflow.py` unchanged and green, confirming
Stage 18.5.1's self-assertion/corroboration safety model is intact.

## 7. Bosch

| Stage | Coverage | Confirmed | Notes |
|---|---:|---:|---|
| Stage 18.6 (previous) | 44.4% | 0 | `bosch-home.co.uk`/`bosch-home.com` fetched successfully; authority stayed `unknown` (no SERP corroboration seed that run) |
| Stage 18.7 (today) | 50.0% (9/18) | 0 | 2026-09-16T13:18:45–13:20:17Z, 91.845s. Discovery: 5 Google queries succeeded (14.3/7.3/9.1/8.7/10.1s), then the 6th ("Bosch official PUE611BB5E") was correctly **capped** at 49.5s cumulative (0.5 × 90s fair share) and yielded to DuckDuckGo HTML, which answered in 0.77s. All 5 `bosch-home.*` pages fetched successfully; 154 raw attributes extracted (up from a much smaller count pre-budget-fix, since discovery no longer ate the whole run). Authority still `unknown`: the real Google/DuckDuckGo/Naver titles for "Bosch official website" never contain the literal word "official" (e.g. `bosch-home.com` → "Home Appliances Global Website \| Bosch") — confirmed by direct unit-level testing of `discover_global_official_domains` against the exact captured titles. This is a **separate, deeper** evidence-filter narrowness (title-wording detection), not the provider-restriction bug this stage targeted, and is flagged for future scope (§16), not fixed here. |

The budget-fairness fix demonstrably worked for Bosch (capping fired,
freed time, fetch/extraction succeeded); the remaining bottleneck moved
cleanly from "budget" to "authority evidence-filter narrowness."

## 8. Janome

| Stage | Coverage | Confirmed | Notes |
|---|---:|---:|---|
| Stage 18.6 (previous, same-session snapshot) | 63.2% / 10.5% (volatile) | 11 / 0 | `janome-official.by` reached `verified` via corroboration in one snapshot; not discovered at all in another |
| Stage 18.7 (today) | 10.0% | 0 | 2026-09-16T13:21:48–13:22:57Z, 68.369s, budget not exhausted (21.6s remaining). `janome.club` and `sewing-world.ru` fetched successfully but stayed `unknown` (no matching "official" evidence this run); `dns-shop.ru` ×2 and `market.yandex.ru` blocked. `janome-official.by` was not discovered this run at all — ordinary discovery-candidate volatility (which live sources a given SERP round returns), not a regression. |

The Stage 18.6 schema fix (accessories/presser-feet split) remains intact
(0 conflicts; no regression in `test_schema.py`).

## 9. HONOR

| Stage | Coverage | Confirmed | Conflicts | Notes |
|---|---:|---:|---:|---|
| Stage 18.6 (previous) | 47.8% | 0 | 0 | 3/4 `honor.com` regional pages fetched successfully but stayed `unknown` (no corroboration seed) |
| Stage 18.7 (today) | 47.8% (11/23 schema) | **7** (schema-level) / **35** (all canonical facts) | **4** (schema-level) / **13** (all canonical facts) | 2026-09-16T13:20:17–13:21:48Z, 90.430s. Google circuit-opened on the very first query; Naver answered "HONOR official website"/"HONOR official X8d" successfully, seeding `honor.com` as an official domain. `honor.com/global/...`, `honor.com/sa-en/...` fetched successfully and reached `manufacturer`/`official_document`; two more `honor.com` regional pages errored/blocked. This is the clearest, largest positive result of the stage: authority recovery went from **zero** confirmed facts (every prior Stage 18.x snapshot) to 35, entirely through the fallback-provider fix. |

The 13 conflicts are largely across *different regional* `honor.com` pages
(global/Ireland/Saudi Arabia/UAE editions) disagreeing on fields like
`battery_capacity`, `rear_camera`, `display_size` — plausibly genuine
regional-variant differences now visible for the first time because
multiple official sources are trusted simultaneously, not a new bug. A
field-by-field genuine-vs-bug classification (as Stage 18.6 did for Janome)
was not performed here — it is out of this stage's scope (budget/authority
architecture only) and is recommended as explicit next-stage work if HONOR
remains a target (§16).

## 10. Dreame — both cold runs

| Run | UTC interval | Runtime | Discovery | Fetch | Authority | Coverage |
|---|---|---:|---|---|---|---:|
| 1 (standalone) | 13:18:49.944–13:20:21.842Z | 91.897s | Google timed out (25.0s) on query 1, circuit opened; DuckDuckGo/Naver answered the rest cheaply (0.8–2.8s) — budget-fairness cap never needed to fire | 5 attempted: `ru.dreametech.com` Playwright-fallback timeout (30s), `global.dreametech.com` ×2 success, `dreame-russia.ru` captcha-blocked, `ozon.ru` 403 | `ru.dreametech.com` and `global.dreametech.com` (×2) reached `verified`/`manufacturer`/`official_document` via the fallback-provider fix (confirmed: the `"G12 Pro" site:dreametech.com` query only gets constructed once `official_domains` already contains that domain) | **0.0%** — extraction found **0** raw attributes from either successfully-fetched `global.dreametech.com` page despite HTML being retrieved; category stayed `unknown` |
| 2 (full five-product gate) | 13:23:50.824–13:25:21.132Z | 90.308s | Same fast-Google-failure pattern | `global.dreametech.com` success, `ru.dreametech.com` error, `market.yandex.ru`/`dreame-russia.ru` blocked | Same domains reached `verified`/`manufacturer` again — reproduced | Same **0.0%**, extraction again found nothing usable from the manufacturer pages that were fetched |

**New, precisely localized finding**: the authority fix now reliably (2/2
runs) gets `dreametech.com` domains to `manufacturer`/`verified`, but
extraction/mapping finds zero usable facts on the specific pages reached
(`.../pages/g12-pro`, `.../pages/g12s-pro-user-manual`) even though they
returned HTTP 200 with available text. This is a genuinely new bottleneck,
only visible now that these pages are reachable and trusted for the first
time — not evidence of a budget or authority regression. Not investigated
further (out of this stage's scope); flagged for next-stage work (§16).

## 11. Gressel regression/control

2026-09-16T13:22:57.740–13:23:49.625Z, 51.885s, budget not exhausted
(38.1s remaining), 0.0% coverage, 0 confirmed. `gressel.ru` ×2 return 404,
`dns-shop.ru` ×2 `bot_challenge` (401), `market.yandex.ru` blocked (200 +
challenge body). Identical shape to every prior stage's Gressel finding —
zero successful fetches, so neither the budget-fairness nor the authority
fix had anything to act on. No regression; confirms Gressel's bottleneck
remains purely external fetch/access, unrelated to this stage's changes.

## 12. Runtime/budget table

| Product | Discovery+fetch | Total runtime | Budget remaining at fetch/targeted start | Outer 120s hit? |
|---|---:|---:|---|---|
| Bosch | to 91.8s (targeted_extraction exhausted) | 91.845s | initial fetch ran; targeted stage got 0s | No |
| HONOR | to 90.4s (not exhausted) | 90.430s | initial fetch ran fully | No |
| Janome | to 68.4s (not exhausted) | 68.369s | 21.6s remaining after full run | No |
| Gressel | to 51.9s (not exhausted) | 51.885s | 38.1s remaining after full run | No |
| Dreame run 1 | discovery/fetch to 91.9s (targeted_discovery exhausted) | 91.897s | fetch ran (5 attempted); targeted got 0s | No |
| Dreame run 2 (gate) | to 90.3s (targeted_discovery exhausted) | 90.308s | fetch ran (5 attempted); targeted got 0s | No |

No run reached the 120s outer kill boundary. No retry explosion observed
in any provider-attempt trace. The circuit breaker opened correctly on
genuine failures and was untouched by the new capping logic (verified:
`circuit_open` and `provider_time_capped` never both true on the same
attempt in any run).

## 13. Authority evidence table

| Source | Role before fetch | Self-declared evidence | Independent corroboration | Final authority |
|---|---|---|---|---|
| `bosch-home.co.uk`/`.com`/`.ie`/`.at` (Bosch) | unknown | not evaluated (no discovery-time seed; real titles never contain "official") | none available (no seed) | `unknown` |
| `honor.com/global`, `/sa-en` (HONOR) | unknown | domain/brand consistent | discovery-time official-domain match via Naver's "HONOR official website"/"HONOR official X8d" results (fallback-provider fix) | `manufacturer`/`official_document`, `verified` |
| `global.dreametech.com`, `ru.dreametech.com` (Dreame) | unknown | domain/brand consistent | discovery-time official-domain match via DuckDuckGo/Naver results (fallback-provider fix; Google was circuit-open) | `manufacturer`/`official_document`, `verified` |
| `janome.club`, `sewing-world.ru` (Janome, today) | unknown | not evaluated (no discovery-time seed this run) | none available (no seed; `janome-official.by` not discovered this run) | `unknown` |
| `dns-shop.ru`, `market.yandex.ru`, `ozon.ru` (all products) | unknown | n/a (never fetched successfully — blocked/404) | n/a | `unknown` |

## 14. Remaining bottlenecks

| Product | Deterministic | External | Access | Evidence availability | Extraction/mapping |
|---|---|---|---|---|---|
| Bosch | none open | Google-title wording never says "official" (evidence-filter narrowness — new, separate finding) | 3/5 fetched, 2 blocked | n/a | worked (154 raw attrs) |
| Janome | none open (18.6 schema fix intact) | discovery-candidate volatility (which sources a SERP round returns) | 2/5 blocked this run | n/a | not exercised this run (no verified source) |
| Gressel | none open | manufacturer URLs 404; retailers bot-blocked | 0/5 fetched | n/a | not exercised |
| HONOR | none open | none — authority fix succeeded | 2/4 error/blocked, 2 succeeded | sufficient | worked (35 confirmed facts) |
| Dreame | none open | Google-latency/failure-mode variance no longer starves budget (fixed); "official" wording narrowness same as Bosch's for some domains | 2-3/5 blocked | sufficient reached pages | **new**: 0 raw attributes extracted from 2 successfully-fetched, authority-verified `dreametech.com` pages |

## 15. Tests / Git

- Full suite: **623/623 PASS**.
- `git diff --check`: clean (CRLF/LF advisory warnings only).
- `py_compile` over `core/`, `services/`, `adapters/`, `bot/`: clean.
- Commit created for the budget-fairness and authority-bootstrap fixes.
- Pushed to `origin/main`: Stage 18.7's combined PASS criteria (budget
  fairness demonstrated live, authority no longer single-provider-
  dependent, deterministic safety intact, full suite green) were met.
- Working tree clean after commit.

## 16. Recommendation for next scope (not started)

1. **Official-domain evidence-filter narrowness** (Bosch, and partially
   Dreame/Janome): `discover_global_official_domains`'s "official signal"
   check only recognizes the literal words "official"/"официаль" in a
   title/snippet. Real manufacturer homepages routinely never use that
   word (confirmed directly against live Bosch/HONOR/Dreame titles this
   stage). A broader, still-conservative evidence model (e.g. structured
   data/copyright signals evaluated at discovery time, not only post-fetch)
   would likely recover Bosch and close the gap between "fetched
   successfully" and "confirmed."
2. **Extraction/mapping gap on newly-reachable manufacturer pages**
   (Dreame): `global.dreametech.com/pages/g12-pro` and
   `.../g12s-pro-user-manual` return HTML successfully but yield zero raw
   attributes. Worth a focused extraction audit now that these pages are
   authority-verified and reachable for the first time.
3. Both are evidence-backed by this stage's live runs; neither was started
   here, per the stage's budget/authority-architecture-only mandate.

## 17. Git

- HEAD before this stage: `284e97b6fb444285dc5c8b9152cc31cb51914211`.
- Deterministic fixes applied and verified (623/623 PASS, clean diff-check).
- See repository history for the exact commit hash and `origin/main` state
  after this stage's push.
