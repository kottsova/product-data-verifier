# Stage 29 — MVP Salvage Audit

Verdict: **PARTIAL** (an honest, narrow MVP is real and shippable; it is not the
generic "any brand, any model" MVP the roadmap originally aimed for)

This is an audit, not a new retrieval layer. One code change was made, and it
is a measurement fix, not a retrieval change: `regression/runner.py`'s
`SUPPORTED_CATEGORIES` set was missing `"laptop"` even though `core/category.py`
has treated `laptop` as a first-class category since Stage 23 introduced a
laptop-bearing blind dataset — the validator had simply never been updated, so
`regression/runner.py --dataset stage23_blind_v1.json` hard-failed before this
fix. See §15 for the commit.

## 1. What actually works today

- **Discovery almost always resolves *a* candidate.** Across all 15 cold-run
  products, `exact_model_hit_any_provider` and `official_domain_hit_any_provider`
  were **both `True` 15/15** (derived from `metadata["initial_provider_attempts"]`,
  the Stage 27/28 telemetry). Even products that ended `insufficient` with zero
  confirmed attributes (HP Spectre, Fisher & Paykel, Ninja, Husqvarna) still
  had a provider report an exact-model, official-domain hit. **This confirms
  what Stage 18.6-28 already suspected and this run makes explicit: discovery
  finding a plausible official URL is no longer the bottleneck. Fetch,
  authority-corroboration, and extraction of that URL are.**
- **Smartphone and laptop are the most reliable categories.** Samsung, Google
  (Pixel), Dell, and Lenovo all produced 5-9 confirmed attributes with a real
  manufacturer/official-document source in the mix (§2 of this run's per-product
  data, reproduced in the table under §5). This matches the historical pattern
  the Stage 18-26 docs already recorded (Bosch/HONOR/Samsung as the
  historically-strongest fetchable brands).
- **When a brand's official site is reachable at all, secondary corroboration
  (gsmarena.com, nanoreview.net, manua.ls/manuals.co.uk) reliably fills in
  around it** — every Tier A/B product in this run that found an official
  source also picked up 1-3 secondary sources for free, at no extra retrieval
  cost (they come from the same discovery pass).
- **Zero timeouts, zero crashes, zero deterministic application failures**
  across the 15-product cold run (`failure_groups.deterministic_application_failure
  = 0`, `timed_out = 0` in `regression/results/stage29_cold_v1.json`). Stage
  24-28's stability/circuit-breaker work paid off: the pipeline degrades to
  `insufficient`, it does not hang or crash.
- **The bot layer is already honest and non-leaking.** `bot/formatters.py`'s
  `format_error`/`_localize_quality_message` explicitly strip any snake_case
  internal code or exception class name and substitute a generic message
  (confirmed by the research pass over `bot/formatters.py:108-124,200-213`); no
  provider name, "blocked", "captcha", or "circuit" string is ever sent to a
  user. The bot already shows quality status, coverage, and per-attribute
  confirmed/unresolved markers (`format_result`, `bot/formatters.py:29-34,134-143`)
  without blocking the event loop (`asyncio.to_thread`, `bot/jobs.py:263-269`).
- **CSV export already has the right shape.** `core/export.py`'s
  `export_profile_csv`/`profile_rows` already emit one row per evidence item
  with `Source`, `SourceType`, `Authority`, `Confidence`, `Evidence` columns
  (`core/export.py:20-38,240-268`) — this is most of what §10 needs. It is
  simply not wired to any bot command yet (confirmed zero callers under `bot/`).

## 2. What actually does not work

- **"Verified" is unreachable in this run — 0/15.** `assess_product_quality`
  requires **100% of critical fields found** (not just confirmed),
  ≥75% of critical fields confirmed, ≥40% coverage, ≥50% of confirmed critical
  fields from a manufacturer-verified source, and no critical conflicts
  (`core/quality.py:198-220`, `verified_min_critical_found_ratio = 1.0`). A
  single missing critical spec field blocks "verified" outright. Historically
  (`MVP_REGRESSION_REPORT.md` lines 11-12,44) verified/partial were already
  0/0 out of 5; this run's 15-product sample confirms it is not a fluke —
  "verified" has not been observed live in any stage's benchmark.
- **`air_fryer` is the weakest category with zero exceptions in this run**
  (Ninja and Philips both `insufficient`, 0 and 2 confirmed respectively) —
  consistent with Stage 18.4-18.9's repeated finding that Gressel (air_fryer)
  stays at "0/17 expected fields, manufacturer URLs 404, retailer bot-blocked"
  across five separate stages.
- **Half of `cooktop`/`sewing_machine` brands still fail outright** even
  though the category as a whole isn't hopeless: Miele and Fisher & Paykel
  (cooktop) and Husqvarna (sewing) landed on `insufficient` with 0-2 confirmed
  attributes despite discovery reporting an official-domain hit for all three
  — the found "official" URL did not survive fetch/authority/extraction.
- **Cross-market/cross-generation source mixing produces false "conflicted"
  results even in the best category.** All 3 smartphones and the best cooktop
  (Smeg, 9 confirmed / 61% coverage — the single richest profile in the whole
  run) landed on `conflicted`, not `partial` or `verified`, because
  `core/quality.py`'s conflict check runs before the verified/partial checks
  and does not distinguish "a genuinely disputed spec" from "two manufacturer
  pages for different regional SKUs of the same model." The per-product data
  shows accepted official/secondary URLs mixing `.com`, `.com.au`, `.co.kr`,
  `.ca` variants of the same brand for one requested market — this is a real,
  previously undocumented gap this audit surfaces (see §11).
- **Expensive SERP providers that rarely pay for their runtime remain on by
  default with no cost mitigation configured.** Stage 24/25/26 already found
  Bing "usually semantically low-value" (245/262 nonempty-but-useless
  requests) and Google historically the single most expensive provider (45
  requests, 0% success, 1125.86s runtime pre-fix) until Stage 25's
  cross-process circuit breaker cut it to ~32s. **That circuit breaker is
  opt-in via `PDV_PROVIDER_HEALTH_PATH`, and it is not set anywhere in this
  repo's `.env.example`, `compose.yaml`, `Dockerfile`, or `config.py`** — the
  deployed bot runs every request as if Stage 25 never shipped. This is the
  single highest-leverage, zero-risk fix available (§8, §11).
- **No CSV/XLSX export is reachable by a real Telegram user.** The pipeline
  produces exportable data; nothing in `bot/` calls `core/export.py`. XLSX
  does not exist at all (no `openpyxl`/`xlsxwriter` anywhere in the repo or
  `requirements.txt`).
- **Median per-product runtime (89.9s) sits right at the 90s default
  `wall_clock_budget_seconds`** (`services/product_verifier.py`) for 11 of 15
  products — the budget is being exhausted, not merely used, on the majority
  of products, including several that still ended `insufficient`. Runtime is
  not currently a differentiator between success and failure; it is close to
  maxed out almost regardless of outcome.

## 3. Categories/brands that fit a first MVP

Ranked by this run's actual outcomes (not assumptions):

| Rank | Category | Brands that worked | Brands that didn't |
|---|---|---|---|
| 1 | smartphone | Samsung, Google (both rich, both conflicted on cross-market mixing) | Sony (weak: 3 confirmed, 21.7%) |
| 2 | laptop | Dell, Lenovo (both partial, 5-6 confirmed) | HP (0 confirmed) |
| 3 | wet_dry_vacuum | Tineco (partial, 5 confirmed) | *(only 1 sample in this dataset — promising, unconfirmed breadth)* |
| 4 | cooktop | Smeg (richest single profile: 9 confirmed, 61% coverage, but conflicted) | Miele, Fisher & Paykel (0-2 confirmed) |
| 4 | sewing_machine | Elna (partial, 6 confirmed) | PFAFF, Husqvarna (0-2 confirmed) |
| 6 | air_fryer | *(none)* | Ninja, Philips (both insufficient) |

## 4. Actual success rate of the current pipeline

Single controlled cold run, 15 products, production-like config (default 8
discovery providers, targeted search on, `max_sources=5`, cache forced cold),
`regression/results/stage29_cold_v1.json`:

- **Technical completion: 15/15 (100%)** — no crash, no timeout, no
  deterministic application failure.
- **`verified`: 0/15 (0%)**
- **`partial`: 4/15 (27%)** — Dell, Lenovo, Tineco, Elna
- **`conflicted`: 4/15 (27%)** — both Google and Samsung phones, Sony, Smeg
  (conflicted still carries real confirmed evidence, just with a flagged
  disagreement — see §2's cross-market note)
- **`insufficient`: 7/15 (47%)** — HP, Miele, Fisher & Paykel, Ninja, Philips,
  PFAFF, Husqvarna
- **"Any confirmed evidence" (partial + conflicted): 8/15 (53%)**
- Average coverage 20.9%, median 21.7%; average runtime 76.3s, median 89.9s.

This is a real improvement over the earlier 5-product `MVP_REGRESSION_REPORT.md`
baseline (0 verified/partial out of 5, i.e. 0% "any confirmed evidence") but
the honest headline is still: **just over half of blind products get some
usable, source-backed data; almost none get a clean "fully confirmed" result;
and category/brand is still the dominant predictor of outcome, not query
phrasing or retry count.**

## 5. Tier A / B / C

Built from this run's actual per-product results
(`regression/results/stage29_cold_v1.json` + the cache-derived provenance
check), not assumptions:

### Tier A — exact identity stable, official evidence usually available, confirmed profile usable
- **smartphone**: Samsung, Google — 5-9 confirmed attributes, real
  manufacturer/support sources, official domain always found. Ships with an
  honest "conflicted" label (cross-market mixing), not a false "verified".
- **laptop**: Dell, Lenovo — 5-6 confirmed attributes from manufacturer spec
  pages, clean `partial` status (no conflicts).
- **wet_dry_vacuum**: Tineco — `partial`, 5 confirmed, official product page
  found. *(Caveat: dataset has only 1 wet_dry_vacuum product; do not treat
  this as category-wide confidence — treat it as one strong data point.)*

### Tier B — exact identity present, official source inconsistent, secondary evidence makes a labeled partial result useful
- **cooktop**: Smeg is the richest single result in the whole run (9
  confirmed, 61% coverage) but is brand-, not category-, level reliable —
  Miele and Fisher & Paykel in the same category both failed outright.
- **sewing_machine**: Elna reaches `partial` (6 confirmed); PFAFF and
  Husqvarna do not.
- **smartphone (Sony specifically)**: inside an otherwise Tier A category,
  Sony is materially weaker (3 confirmed, 21.7% coverage) — Tier assignment
  in this system is closer to brand-level than category-level truth.

### Tier C — retrieval not stable enough for MVP
- **air_fryer**: both sampled brands (Ninja, Philips) landed `insufficient`;
  no brand in this category reached usable output in this run.
- Individual Tier-C brands inside otherwise-better categories: HP (laptop),
  Miele + Fisher & Paykel (cooktop), PFAFF + Husqvarna (sewing_machine).

**Practical reading: this system's actual unit of reliability is brand, not
category.** A category-level MVP promise ("we support cooktops") would be
false for 2 of 3 sampled cooktop brands. A brand-aware promise ("we do well
with Samsung, Google, Dell, Lenovo, Tineco, Smeg, Elna; we're unreliable for
HP, Miele, Fisher & Paykel, Ninja, Philips, PFAFF, Husqvarna") is what the
evidence actually supports.

## 6. Honest product contract

**What the bot can promise today:**

> "Send a brand + model. We will try to find the manufacturer's own page and
> at least one independent reference for it, cross-check what they say, and
> tell you exactly what we could confirm, what conflicts between sources, and
> what we could not find — never a made-up spec, and never a silent
> guess dressed up as verified."

**Concrete response states** (mostly already implemented in `bot/formatters.py`,
gaps noted):

1. **Confirmed / partial** (existing `partial` status): "Here's what we
   confirmed (N of M expected fields), from these kinds of sources
   (manufacturer / independent reference), here's what's still missing."
   *(Already shown; gap: no source URL is currently rendered in chat — see §9.)*
2. **Confirmed with a flagged conflict** (existing `conflicted` status): "We
   found conflicting values for X across sources — here is what each source
   said and where it came from, use your judgment." *(This is a more honest
   framing of `conflicted` than "error"; today's bot copy should be checked
   that it doesn't read as a failure — recommend explicit review, not a code
   guess here.)*
3. **Insufficient / no official source reachable** (existing `insufficient`
   status): "We could not get enough confirmed data for this product right
   now — the manufacturer page for this exact model wasn't reachable or
   didn't have machine-readable specs. Try a more specific model name/market,
   or check back later." *(Never claim a fictitious verified result; never
   surface "captcha"/"blocked"/provider names — already enforced.)*
4. **No response / hang**: not currently a real state — 0/15 timeouts in this
   run, and the bot already runs `verify()` off the event loop with an
   accepted/started ack. No change needed here.
5. **Provenance display**: quality/status/coverage/confirmed-count are
   already shown; **source URL and source type per attribute are computed
   but not rendered in chat** — this is the one concrete formatter gap for a
   contract that promises "source provenance."

## 7. Retrieval paths to keep in production

- **`DirectDomainProbeProvider` (HTTP official-first)** — cheap, bounded,
  reused as the base of every accepted-official-URL case in this run. Keep,
  unconditionally on.
- **`BrowserOfficialDiscoveryProvider`** — Stage 28 showed it doesn't move the
  exact-official rate up on its own (1/5 → 1/5 in the SERP-disabled smoke),
  but it is cheap when unused (only launches when HTTP found nothing exact)
  and this cold run shows the production pipeline (SERP + browser both on)
  reaching several brands HTTP-only Stage 27 could not. Keep as a fallback,
  bounded exactly as shipped.
- **`DuckDuckGoHtmlSearchProvider`** — this run's SERP-enabled config is what
  actually found Smeg's real official page (`pi-exchange.smeg.it`, not
  discoverable by the brand-derived-domain logic alone) and Elna's
  (`global.elna.com`). Keep; it is still doing real, brand-specific discovery
  work that `DirectDomainProbeProvider`/browser cannot replicate generically.
- **Secondary/reference domains surfaced incidentally by SERP** (gsmarena,
  nanoreview, manua.ls family) — keep; they arrive at effectively zero extra
  cost alongside official discovery and are what makes Tier B results usable
  at all.

## 8. Paths to disable or de-prioritize for MVP

- **Turn on the Stage 25 cross-process circuit breaker
  (`PDV_PROVIDER_HEALTH_PATH=<writable path>`) and the Stage 26
  `PDV_DISABLED_DISCOVERY_PROVIDERS` knob in the actual deployment config**
  (`.env.example`/`compose.yaml`/`Dockerfile`) — **this is a config-only
  change, zero code, zero new dependency**, and it directly addresses the
  single largest documented waste in the whole system: Google/Bing/DuckDuckGo
  Lite historically burning 100s-1000s of seconds per run for near-zero
  contribution (Stage 24/25/26 findings, §2). This should ship with or before
  the MVP, not after.
- **Recommend disabling `duckduckgo_lite` and de-prioritizing `bing`** via
  that same env var — Stage 24 measured DDG Lite at 4.3%→0% success and Stage
  26 measured Bing at 0 successes after quality-gating across a 45-product
  gate. Neither contributed a single accepted candidate in any cited stage
  report. This is a config flip, not a code change, and is reversible in one
  line if evidence changes.
- **Do not disable `google`** despite its historical cost — once the circuit
  breaker above is actually turned on, Stage 25 already measured its cost
  drop to ~32s for the same benefit; the problem was the missing config, not
  the provider.
- **Do not touch `BrowserOfficialDiscoveryProvider`, authority, identity, or
  validation logic** — Stage 28 already showed browser-backed discovery is
  not the lever that moves outcomes; changing it now would be exactly the
  "another experimental retrieval layer" this stage was told not to build.

## 9. Telegram flow to ship

No redesign. Minimal, additive changes to the existing 1,416-line `bot/`
layer:

1. Keep the existing `/start`, `/help`, `/status`, `/cancel` + free-text flow
   unchanged.
2. Keep the existing accepted → started → result message sequence unchanged
   (already async, already non-blocking).
3. **Add source provenance to `format_result`**: for each confirmed
   attribute, show source type (manufacturer / independent reference) and,
   for at least the top-line identity/confirmed attributes, the source URL
   or domain. This is the one concrete formatter gap identified in §6.
4. **Reframe `conflicted` copy** (review only, likely no logic change) so it
   reads as "we found disagreement, here is each source's value," not as an
   error state, matching the honest-conflict contract in §6.
5. Continue to never expose provider names, blocking/captcha reasons, or
   exception text — already correctly enforced; add a regression test if one
   does not already exist pinning this behavior for the new provenance
   fields specifically (so a future change can't accidentally leak a
   provider-internal string through the new source-URL rendering path).

## 10. CSV/XLSX export to ship

- **CSV: ship it, wired to a new `/export` (or an inline button on the result
  message).** `core/export.py`'s `export_profile_csv` already has the right
  per-evidence-row shape (`Attribute, Value, Status, Source, Evidence,
  Confidence, Authority, SourceType`, `core/export.py:20-38`). The only gap
  for the task's minimum field list (product/brand/model/category/attribute/
  value/source type/source URL/confidence-status) is that brand/model/
  category currently live once in `profile.identity`/`profile.category`
  (JSON), not as CSV columns — add three constant columns (repeated per row)
  when wiring the bot export, no change to `core/export.py`'s core contract
  needed.
- **XLSX: do not add for MVP.** No spreadsheet library exists in this repo
  today (`openpyxl`/`xlsxwriter` absent from `requirements.txt`), and the task
  explicitly forbids new paid dependencies — `openpyxl` is free/MIT, so it
  would not violate that rule, but it is still new infrastructure the task
  asks to add "only if it can be done without new external infrastructure and
  with a small amount of work." Given CSV already covers every listed field
  and opens correctly in Excel, treat XLSX as a fast-follow, not MVP-blocking.

## 11. What's needed before the first usable release

1. Wire `core/export.py` into a bot command/button (no `core/export.py`
   change needed beyond adding brand/model/category as repeated columns at
   the call site).
2. Render source type + URL/domain per confirmed attribute in
   `bot/formatters.py:format_result` (currently computed, not shown).
3. Set `PDV_PROVIDER_HEALTH_PATH` and `PDV_DISABLED_DISCOVERY_PROVIDERS` in
   the actual deployment config (`.env.example`, `compose.yaml`, `Dockerfile`)
   — config only, addresses the largest documented runtime/cost waste.
4. Decide and document (product decision, not a code fix) how `conflicted`
   should read to an end user — this audit surfaced that the richest result
   in the whole run (Smeg, 61% coverage) is currently labeled the same way as
   a genuine spec dispute, when the actual cause was cross-market source
   mixing. Whether to (a) leave the copy as "sources disagree, here's each
   one" (zero code change, ships now) or (b) later teach the identity/
   authority layer to prefer same-market sources (a real retrieval change,
   explicitly out of scope for this stage) is a decision for the next stage,
   not this one.
5. A short internal doc or CLAUDE.md note listing the Tier A/B/C brand list
   from §5, so future stage work and support messaging stay grounded in
   measured brand-level reliability instead of category-level assumptions.

## 12. Remaining work, as concrete tasks (not time estimates)

1. Add source-provenance rendering to `format_result` (bot/formatters.py).
2. Add an `/export` command (or button) calling `core/export.py`'s existing
   CSV serializer, with brand/model/category added as repeated columns at
   the call site.
3. Add `PDV_PROVIDER_HEALTH_PATH` + `PDV_DISABLED_DISCOVERY_PROVIDERS` to
   `.env.example`/`compose.yaml`/`Dockerfile` and to the deployed bot's actual
   environment.
4. Write/confirm a regression test that the new provenance rendering never
   leaks a provider name or blocking-reason string (extends the existing
   `format_error` leak-proofing tests to the new code path).
5. Product-review (not code) the `conflicted`-state chat copy per §11.4.
6. Publish the Tier A/B/C brand list (§5) somewhere durable (README or a
   short internal note) so it can be checked against future benchmark runs
   instead of re-derived from scratch each time.

No item above is a new retrieval mechanism, and none touches
identity/authority/validation.

## 13. Recommended next stage

**Stage 30 — MVP release hardening**: implement items 1-4 in §12 (provenance
rendering, export wiring, provider-cost config, leak-proofing test), then re-run
this exact same 15-product cold benchmark once to confirm the config change
(item 3) measurably reduces average runtime without regressing the
verified/partial/conflicted/insufficient distribution measured here. Do not
open a new retrieval-mechanism stage until that confirmation run exists.

## 14. PASS / PARTIAL / FAIL for MVP readiness

**PARTIAL.** A real, honest, brand-aware MVP exists in the data today (Tier A:
Samsung, Google, Dell, Lenovo, Tineco; Tier B: Smeg, Elna, Sony) and the
Telegram layer already has the honesty properties (no fabricated verified
results, no internal leakage, async and non-blocking) the task's contract
requires. It is not yet released as an MVP because two small, well-scoped
gaps remain — export wiring and provenance rendering — plus one zero-code
config fix (provider cost). None of these require new retrieval work. FAIL
would mean no viable subset exists or the bot leaks internals/fabricates
results; that is not the case. PASS would require §12 done and a confirming
benchmark re-run; that has not happened yet.

## 15. Commit hash

See final message for this turn — commit contains a one-line measurement fix
to `regression/runner.py` (`SUPPORTED_CATEGORIES` gains `"laptop"`), this
report, and the new benchmark artifacts under `regression/results/`.

## 16. HEAD = origin/main

Confirmed at commit time (see final message).

## 17. Working tree clean

Confirmed at commit time (see final message).
