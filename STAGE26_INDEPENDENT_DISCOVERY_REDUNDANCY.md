# Stage 26 — Independent Discovery Redundancy

Date: 2026-09-17  
Dataset: `regression/datasets/stage23_blind_v1.json` (15 frozen products)  
Verdict: **FAIL**

## 1. Root cause of the DDG HTML dependency

The Stage 25 provider pool was a serial fallback chain, not independent
redundancy. `ResilientSearchSession.search_with_status()` returned as soon as
one provider produced a non-low-value result. Later providers therefore did
not contribute candidates to the same query, and `DirectDomainProbeProvider`
ran only after every SERP provider failed. The direct path could return only a
brand-root homepage; it could not discover an exact product page.

The controlled pre-change fault injection reproduced the dependency:

| DDG HTML forced down | Apple iPhone 15 | Bosch PUE611BB5E |
|---|---:|---:|
| confirmed | 3 | 0 |
| schema fields found | 3 | 0 |
| full retrieval collapse | no | **yes** |

## 2. Why the previous alternatives did not save retrieval

| Path | Pre-change / controlled diagnosis |
|---|---|
| Google | HTTP results fell through to Playwright; live calls timed out or hit network denial. A slow call consumed its per-provider allocation before yielding. |
| Bing | Reachable in some windows, but results frequently failed the identity-signal quality gate; other responses contained organic anchors that the parser could not safely resolve. |
| Naver | Productive while reachable, but WAF blocking was intermittent. It was the only prior alternative that returned meaningful exact-model candidates at scale in this test window. |
| DDG Lite | Shared DDG/egress failure characteristics; blocked or connection-reset in the live gates. |
| DirectDomainProbe | Independent transport, but only a homepage bootstrap. It did not previously search sitemaps, robots declarations, structured links, or a bounded site-search endpoint. |
| Deterministic official URL path | Guessed exact brand roots were never auto-trusted (correctly), but there was no generic method to turn a verified root into an exact product URL. |

The later providers also received the remainder of a serial budget. One slow
or blocked path could delay all paths behind it. Provider health was global by
provider name; this was correct for shared SERP endpoints but incorrect for the
direct provider, whose actual failure domain is the individual manufacturer
host.

## 3. Changes made

### Provider orchestration and queries

- A productive SERP result is now merged with supplemental independent paths
  in deterministic provider order. Third-party request volume stays bounded:
  after one productive SERP, other SERPs are skipped, while the independent
  direct path may still contribute.
- Provider-aware syntax was added: Bing/Google receive an exact quoted model;
  Naver receives unquoted tokens. Already-quoted phrases are preserved.
- `PDV_DISABLED_DISCOVERY_PROVIDERS` enables controlled subprocess gates
  without monkey-patch leakage on Windows.
- Direct-provider shared health is scoped to the normalized brand rather than
  globally poisoning unrelated manufacturer domains.

### Independent public index

- Added `SeznamSearchProvider`, a keyless public-index provider with bounded
  timeout, generic external-link parsing, query-quality gating, canonical URL
  normalization, and the existing identity/authority/validation pipeline.
- No product, brand-domain table, blind URL, or dataset-specific query was
  added.

### Direct official-domain discovery

- The direct path is configured with the request's brand/model context.
- It performs at most eight requests and remains depth-bounded:
  brand-root homepage, robots-declared/default sitemaps, a small sitemap-index
  fan-out, homepage/search structured links, and one conventional site-search
  endpoint.
- Only same-registrable-domain model-bearing links are returned.
- Homepage and catalog/search URLs are excluded as product evidence.
- Guessed domains are still not trusted. Promotion continues through the
  existing official-domain corroboration rules.

### Telemetry

Provider attempts now record effective query, exact-model hit,
official-domain hit, budget before/after, independent success without DDG,
and post-ranking accepted/rejected candidate counts. A gate-summary diagnostic
reports quality states, coverage, runtime, full collapses, provider failures,
and provider contribution.

## 4. Changed files

- `core/discovery.py`
- `diagnostics/live_quality_diagnosis.py`
- `diagnostics/provider_fault_injection.py`
- `diagnostics/stage26_gate_summary.py` (new)
- `tests/test_discovery.py`
- `tests/test_stage26_discovery_redundancy.py` (new)
- `STAGE26_INDEPENDENT_DISCOVERY_REDUNDANCY.md` (new)

## 5. Tests

- Full suite: **730/730 PASS** (`python -m unittest discover -s tests`).
- New deterministic coverage includes:
  - provider-specific query construction;
  - DDG removal without removing other providers;
  - timeout isolation;
  - merged SERP + independent results;
  - exact-model sitemap discovery without a SERP;
  - cross-provider canonical dedupe/provenance;
  - Seznam external exact-model parsing;
  - all previous authority, identity, validation, workflow, fetch, and
    provider-resilience tests unchanged and green.

Unit tests prove deterministic behavior only; the verdict below is based on
the live gates.

## 6. Provider-by-provider final live diagnosis

Final Gate A aggregate (45 product-runs, DDG HTML absent):

| Provider | Live result | Exact-official product-run contribution | Independent of DDG HTML |
|---|---|---:|---|
| Naver | 7 successes / 21 real requests; later WAF blocks opened the circuit | 0 in final gate | Different endpoint/index, same public egress |
| Seznam | 91 successes / 116 real requests; later rate limiting | **6** | Yes: different endpoint/index |
| DDG Lite | 0 successes; 11 blocked requests | 0 | No practical independence from DDG/WAF conditions |
| Bing | 0 successes after quality gating; 211 low-value and 4 parse-error attempts | 0 | Endpoint-independent but not productive |
| Google | 0 successes; 8 timeouts | 0 | Endpoint-independent but not productive |
| DirectDomainProbe | 18 successful result attempts, mainly homepages; 7 timeouts | 0 | Yes: manufacturer hosts, per-brand health scope |

Seznam demonstrably found accepted exact-model/official candidates without DDG
HTML and rescued a meaningful part of one run, but it did not remain available
across the required repeatability gate. Direct discovery remained independent
but did not find an exact product page live in this dataset.

## 7. Gate A — DDG HTML disabled (three final cold runs)

Each run used a fresh provider-health file, no repository cache, full workflow,
60-second per-product budget, 180-second process timeout, and concurrency 3.

| Metric | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| completed / service success | 15 / 15 | 15 / 15 | 15 / 15 |
| confirmed > 0 | **5/15** | **0/15** | **0/15** |
| verified / partial / insufficient / conflicted | 0 / 0 / 15 / 0 | 0 / 0 / 15 / 0 | 0 / 0 / 15 / 0 |
| average coverage | 11.25% | 0.00% | 0.00% |
| median coverage | 11.8% | 0.0% | 0.0% |
| aggregate product runtime | 548.96s | 199.87s | 162.62s |
| full retrieval collapses | **7/15** | **15/15** | **15/15** |
| provider failures | 43 | 120 | 117 |

Repeatability score: **80.39/100**. The score is not evidence of health: two
runs repeatably collapsed. DDG removal still causes systemic collapse.

Artifacts:

- `diagnostics/results/stage26-gate-a-final-run{1,2,3}.json`
- `diagnostics/results/stage26-gate-a-final-audit.json`

## 8. Gate B — normal provider pool (three cold runs)

| Metric | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| completed / service success | 15 / 15 | 15 / 15 | 15 / 15 |
| confirmed > 0 | **5/15** | **0/15** | **0/15** |
| verified / partial / insufficient / conflicted | 0 / 0 / 12 / 3 | 0 / 0 / 15 / 0 | 0 / 0 / 15 / 0 |
| average coverage | 6.81% | 0.00% | 0.00% |
| median coverage | 0.0% | 0.0% | 0.0% |
| aggregate product runtime | 240.28s | 39.72s | 33.35s |
| full retrieval collapses | **10/15** | **15/15** | **15/15** |
| provider failures | 120 | 30 | 30 |

DDG HTML itself succeeded on 8/20 real requests across the three runs and
contributed three exact-official product-runs. The normal pool also suffered a
systemic external outage in runs 2–3.

Artifacts:

- `diagnostics/results/stage26-gate-b-run{1,2,3}.json`
- `diagnostics/results/stage26-gate-b-audit.json`

## 9. Apple isolated regression

Three independent artifacts, each with a fresh health store:

| Metric | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| confirmed | 0 | 0 | 0 |
| coverage | 0% | 0% | 0% |
| quality | insufficient | insufficient | insufficient |
| full collapse | yes | yes | yes |

This regressed from Stage 25's stable `confirmed=5` in all three runs and
confirms that the final provider window was broadly unhealthy, not just weak
for obscure products.

## 10. Comparison with Stage 25

| Gate | Stage 25 | Stage 26 |
|---|---|---|
| normal blind confirmed > 0 | 7/15 → 10/15 → 12/15 | **5/15 → 0/15 → 0/15** |
| DDG-disabled blind gate | not passed; two-product fault showed collapse | **5/15 → 0/15 → 0/15** |
| Apple isolated | 5 → 5 → 5 confirmed | **0 → 0 → 0** confirmed |
| independent productive path | direct homepage only | Seznam found 6 exact-official product-runs in Gate A, but was not repeatable |

## 11. Accepted evidence without DDG HTML

- **Seznam** was the only new independent path to supply exact-official
  product candidates in the final Gate A (six product-run contributions).
- **Naver** supplied accepted candidates during Gate A run 2 but those did not
  convert into confirmed facts; in earlier controlled/pre-final runs it was
  the path that produced the successful evidence.
- **DirectDomainProbe** supplied verified/plausible roots and was invoked
  independently, but produced no exact-official product contribution.
- Bing, Google, and DDG Lite supplied no accepted exact-official evidence.

## 12. Remaining blockers

1. No independent path stays productive across three cold runs. Naver and
   Seznam alternate between productivity and WAF/rate-limit failure.
2. Direct generic sitemap/search discovery is too shallow for brands whose
   consumer-product site is not the exact normalized corporate root, and its
   bounded sitemap fan-out found no live exact product page in the blind gate.
3. Bing's result quality/parsing remains non-productive.
4. Google and DDG Lite remain unavailable in the observed egress window.
5. Official-domain discovery remains the dominant downstream blocker even
   when retailer/external exact-model candidates exist.
6. The normal pool's late-window systemic outage prevents demonstrating that
   the new provider improves the healthy baseline.

## 13. Acceptance criteria verdict

| Criterion | Result |
|---|---|
| all tests pass | PASS (730/730) |
| blind dataset ≥15 | PASS |
| three DDG-disabled cold runs | PASS (executed) |
| three normal cold runs | PASS (executed) |
| DDG-disabled no systemic collapse | **FAIL** |
| independent path saves meaningful products live | PARTIAL (Seznam, one run only) |
| alternatives have real budget | PASS (real requests and accepted candidates recorded) |
| no product-specific hardcode / blind URLs | PASS |
| authority/identity/validation unchanged | PASS |
| committed and pushed; clean synchronized tree | recorded in final handoff |

**Final Stage 26 verdict: FAIL.** The code establishes real independent
mechanisms and one new provider produced accepted live evidence, but the key
acceptance condition is not met: DDG-disabled retrieval still collapsed
systemically in two of three final cold runs, and the normal pool/Apple guard
also collapsed in the final external-provider window.
