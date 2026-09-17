# Stage 23 — Retrieval Reliability & Evidence Conversion

Date: 2026-09-17  
Baseline commit: `b51f28a4724ec460948de1f710ba57996da51185`  
Verdict: **PARTIAL**

The gate is not passed. The primary post-change run improved evidence conversion and runtime substantially, but it did not reach the required confirmed/quality/official-use thresholds. An immediate repeat run also exposed material provider instability, so the stronger first result is not treated as a reliable PASS.

## A. Funnel before

Stage 22 baseline, same 15-product blind set:

| Funnel step | Products |
|---|---:|
| Official candidate discovered | 12/15 |
| Exact official candidate identified | 10/15 |
| Exact official selected into fetch budget | 9/15 |
| Fetch attempted | 9/15 |
| Fetch succeeded | 5/15 |
| Raw attributes extracted | 5/15 |
| Expected schema fields matched | 5/15 |
| Authority accepted | 5/15 |
| Corroboration/validation passed | 5/15 |
| Confirmed fact produced | 5/15 |

Baseline conversions:

- exact official discovered → fetched: 5/10 = 50.0%
- fetched official → raw: 5/5 = 100.0%
- raw → expected mapped: 5/5 = 100.0%
- expected mapped → confirmed: 5/5 = 100.0%

The aggregate funnel is product-level: a step means at least one item for that product crossed it. Field-level mapping loss is separately represented by `raw` and `expected_mapped` in the per-product table.

## B. Root causes

1. Search-result snippets were allowed to prove exact identity. This admitted family variants such as iPhone 15 Pro/Plus for iPhone 15.
2. Slow providers could consume half of the workflow budget before fallbacks had a fair opportunity.
3. `google.com` was blocked as a suffix, unintentionally excluding product hosts such as `store.google.com` and `support.google.com`.
4. Exact official documents did not have a deterministic selection tier and regional mirrors could occupy several fetch slots.
5. Requests and browser fallback each effectively received a fresh timeout, allowing transport work to exceed the intended deadline.
6. Generic labels with count prefixes or parenthetical qualifiers, composite display values, and battery units were lost between raw extraction and the expected schema.
7. Specialist sources were not classified consistently, so useful corroboration competed as an ordinary comparison source.
8. Live access remains unstable: WAF/captcha/access-denied responses and provider rate/circuit behaviour change materially between consecutive runs.

## C. Changes

- `core/discovery.py`
  - provider fair-share default reduced from 50% to 25%;
  - exact-model matching now uses title/URL, not snippet echoes;
  - known commercial family modifiers are rejected as another variant;
  - exact `google.com` search host is blocked without blocking Google product subdomains;
  - specialist reference domains receive an explicit source class;
  - a generic quoted `site:<normalized-brand>.com` query is added;
  - official-domain bootstrap is limited to mechanically exact `<brand>.com` evidence with brand and exact model signals;
  - generic first-party support results may enter a verification fetch without being pre-declared exact.
- `core/workflow.py`
  - deterministic priority: exact official document, exact manufacturer, then exact specialist corroboration and bounded verification candidates;
  - host/source diversity and a one-slot limit for ordinary weak results;
  - fetched identity upgrade only from title, H1, social title, or final URL—not incidental body mentions;
  - per-fetch deadline is capped and reserves a workflow tail.
- `core/fetch.py`
  - requests and Playwright share one deadline;
  - browser fallback is capped at five seconds and skipped when insufficient time remains;
  - verified first-party transport errors can use the same bounded browser fallback as blocked responses.
- `core/normalize.py`, `core/schema.py`, `core/mapping.py`
  - generic removal of parenthetical qualifiers and numeric count prefixes;
  - cross-category aliases for common RAM/storage/capacity/mode/voltage/frequency labels;
  - generic display composite parsing;
  - mAh/Ah semantic mapping to battery capacity.
- `diagnostics/retrieval_conversion_audit.py`
  - reproducible ten-step funnel, conversions, per-product rows, failure classes, and aggregate metrics.
- `diagnostics/run_live_dataset.py`
  - reusable runner for an arbitrary frozen dataset or ID subset.
- `regression/datasets/stage23_blind_v1.json`
  - frozen copy of the Stage 22 15-product blind set.
- Regression coverage was added in discovery, identity, workflow, fetch, mapping, and audit tests.

No Telegram, UI, CSV, Excel, or deployment code was changed.

## D. Funnel after

Primary post-change full run (`.cache/stage23/blind-after.json`):

| Funnel step | Products | Change vs baseline |
|---|---:|---:|
| Official candidate discovered | 9/15 | -3 |
| Exact official candidate identified | 9/15 | -1 |
| Exact official selected into fetch budget | 9/15 | 0 |
| Fetch attempted | 9/15 | 0 |
| Fetch succeeded | 5/15 | 0 |
| Raw attributes extracted | 5/15 | 0 |
| Expected schema fields matched | 5/15 | 0 |
| Authority accepted | 5/15 | 0 |
| Corroboration/validation passed | 9/15 | +4 |
| Confirmed fact produced | 9/15 | +4 |

Conversions:

- exact official discovered → fetched: 5/9 = 55.6%
- fetched official → raw: 5/5 = 100.0%
- raw → expected mapped: 5/5 = 100.0%
- expected mapped → confirmed: 5/5 = 100.0%

Immediate repeat (`.cache/stage23/blind-final.json`) produced only 3 exact-official discoveries, 2 successful official fetches, and 5 products with confirmed facts. That variance is a Stage 23 blocker, not a reason to select the better run as a PASS.

## E. Blind dataset after

Primary post-change full run:

| Product | Category | Official | Selected | Fetched | Raw | Mapped | Confirmed | Unresolved | Conflicts | Coverage | Quality | Runtime s | Dominant failure |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|
| Google Pixel 9 Pro | smartphone | yes | yes | yes | 94 | 3 | 5 | 16 | 2 | 34.8% | conflicted | 48.80 | none |
| Samsung Galaxy Z Flip6 | smartphone | yes | yes | yes | 100 | 11 | 9 | 13 | 1 | 43.5% | conflicted | 61.87 | none |
| Sony Xperia 1 VI | smartphone | yes | yes | no | 0 | 0 | 3 | 19 | 1 | 21.7% | conflicted | 52.22 | provider access/fetch |
| Dell XPS 13 9340 | laptop | yes | yes | no | 0 | 0 | 3 | 14 | 0 | 41.2% | partial | 58.02 | provider access/fetch |
| Lenovo ThinkPad X1 Carbon Gen 12 | laptop | yes | yes | yes | 23 | 2 | 5 | 12 | 0 | 29.4% | partial | 73.83 | none |
| HP Spectre x360 14-eu0000 | laptop | yes | yes | no | 0 | 0 | 0 | 17 | 0 | 0.0% | insufficient | 61.70 | provider access/fetch |
| Miele KM 7464 FL | cooktop | no | no | no | 0 | 0 | 0 | 18 | 0 | 0.0% | insufficient | 30.56 | official discovery |
| Smeg SI2M7953D | cooktop | no | no | no | 0 | 0 | 9 | 7 | 2 | 61.1% | conflicted | 51.40 | official discovery |
| Fisher & Paykel CI604DTB4 | cooktop | yes | yes | no | 0 | 0 | 0 | 18 | 0 | 5.6% | insufficient | 63.18 | provider access/fetch |
| Tineco FLOOR ONE S7 Stretch | wet/dry vacuum | yes | yes | yes | 5 | 2 | 2 | 4 | 0 | 33.3% | insufficient | 52.10 | none |
| Ninja AF400UK | air fryer | no | no | no | 0 | 0 | 0 | 17 | 0 | 0.0% | insufficient | 37.14 | official discovery |
| Philips NA342/00 | air fryer | no | no | no | 0 | 0 | 0 | 17 | 0 | 29.4% | insufficient | 53.70 | official discovery |
| PFAFF ambition 620 | sewing machine | no | no | no | 0 | 0 | 2 | 18 | 0 | 10.0% | insufficient | 38.65 | official discovery |
| Husqvarna VIKING OPAL 650 | sewing machine | no | no | no | 0 | 0 | 0 | 20 | 0 | 0.0% | insufficient | 32.84 | official discovery |
| Elna eXperience 550 | sewing machine | yes | yes | yes | 34 | 6 | 6 | 14 | 0 | 30.0% | partial | 51.83 | none |

`Raw` and `Mapped` in this table are the counts attributable to successfully fetched exact official evidence, not all raw attributes from lower-authority sources.

## F. Metrics

| Metric | Stage 22 | Post-change run 1 | Immediate repeat | Gate |
|---|---:|---:|---:|---:|
| Products with confirmed > 0 | 5/15 (33.3%) | 9/15 (60.0%) | 5/15 (33.3%) | ≥12/15 |
| Partial or verified | 1/15 (6.7%) | 3/15 (20.0%) | 0/15 (0.0%) | ≥9/15 |
| Exact official use when available | 50.0% | 55.6% | 66.7% | ≥70% |
| Median coverage | 5.6% | 29.4% | 0.0% | ≥25% |
| Median runtime | 90.06 s | 52.10 s | 30.40 s | bounded |
| Max runtime | 100.90 s | 73.83 s | 50.58 s | ≤90 s configured budget |

All 15 service calls completed in both post-change full runs with no process timeout. The affected-subset run recorded one 90.15-second trace with `budget_exhausted=true`; the final full runs remained below 90 seconds. Browser work was capped at five seconds per fallback.

## G. Apple iPhone 15

Final isolated trace (`.cache/stage23/apple-after3.json`):

- exact official evidence: `https://support.apple.com/en-au/111831`
- source type: `official_document`
- authority: verified
- identity: exact base model / same base model
- official fetch: success
- raw official attributes: 47
- expected mapped from official evidence: 3
- confirmed expected facts: 2
- conflicts: 0
- coverage: 8.7%
- quality: insufficient
- runtime: 76.50 seconds
- timeout/budget exhausted: false/false

The required Apple exact-official-use and nonzero-confirmed conditions are satisfied. A prior attempt also exposed and then fixed an unsafe upgrade of a generic current-iPhone page that merely mentioned iPhone 15 in its body.

## H. Safety

- No product-specific URL or label rule was added.
- Authority thresholds and validation semantics were not lowered; `core/authority.py` and `core/validation.py` are unchanged.
- Unknown/retailer evidence is not promoted to first-party authority.
- Fetched identity verification requires a verified first-party candidate and structural page identity evidence.
- iPhone 15 Pro/Plus family variants are rejected for an iPhone 15 request.
- Specialized references remain lower authority and are used for corroboration, not first-party promotion.
- Discovery query text is never treated as page evidence.
- Provenance is preserved through candidate, fetch, raw, mapping, and validation traces.

## I. Tests

- Full suite: **678/678 PASS**
- Focused Stage 23 suite: **215/215 PASS**
- `git diff --check`: PASS
- `python -m py_compile` for all changed/new Python modules: PASS
- Live artifacts: Apple isolated run, six-product affected subset, two-product transport subset, and two full 15-product runs completed.

## J. Git

- Implementation commit: `f6fde26` (`Improve retrieval reliability and evidence conversion`)
- Final report: committed in the immediately following documentation commit.
- Push target: `origin/main`
- Post-push checks required and reported in the final handoff: `HEAD = origin/main` and clean working tree.

## K. Verdict

**Stage 23 PARTIAL — evidence conversion and runtime improved, but the acceptance gate is blocked by provider/access instability, insufficient repeatable official discovery/fetch, and low final quality counts.**

Blockers by class:

- Code limitations:
  - discovery cannot safely infer corporate/brand relationships such as alternative parent-company domains without stronger evidence;
  - category resolution can collapse to `unknown` when a provider run returns too little usable content;
  - field-level mapping still leaves many expected attributes unresolved even when raw content is abundant.
- Provider/access limitations:
  - WAF, captcha, access-denied, request timeout, and provider circuit/rate behaviour prevent repeatable first-party fetches;
  - consecutive full runs varied from 9 to 3 exact-official discoveries.
- Unsupported-category/schema limitations:
  - cooktop, air-fryer, and sewing-machine runs remain especially dependent on sparse or blocked sources;
  - several products achieve coverage from lower-authority evidence but remain `conflicted` or `insufficient`, correctly preventing a false PASS.
