# Stage 28 — Browser-Backed Official Retrieval

Verdict: **PARTIAL**

## 1. Why Stage 27 was not enough

Stage 27's `DirectDomainProbeProvider` only ever issues plain HTTP GETs
(`requests`). Of the 5 smoke brands, only Samsung produced an exact-model
official candidate; the other 4 failed for reasons that are not, in
principle, HTTP-vs-browser questions on their own:

- **Dell**: sitemap shards exceeded the bounded byte window, and the public
  search endpoint returned HTTP 403.
- **Miele**: the regional root (`miele.de`) returned HTTP 403; the global
  root's sitemap/search routes did not expose the model.
- **Smeg**: the UK route redirected to a generic homepage; the `.com` route
  timed out.
- **PFAFF**: the homepage was a small JS shell with no server-rendered
  content, and robots/sitemap/search all returned HTTP 404.

Only PFAFF's failure ("small JS shell") is a JS-rendering problem browser
rendering can plausibly fix. Dell, Miele, and Smeg's Stage 27 failures were
WAF/403/timeout at the transport layer, upstream of any JS rendering
question — a real risk flagged going into Stage 28 that a headless browser,
which cannot bypass anti-bot challenges, might hit the identical wall.

## 2. What was added

- **`BrowserOfficialDiscoveryProvider`** (`core/discovery.py`) — a bounded,
  browser-backed fallback for official-site discovery. Reuses the exact same
  brand-derived candidate-domain logic as `DirectDomainProbeProvider`
  (extracted to a shared `_brand_root_domains` function) and the identical
  in-domain/exact-model acceptance rule (extracted to a shared
  `_extract_domain_links` function, now also used by `DirectDomainProbeProvider`
  itself with no behavior change). Adds a JSON-LD `Product` extraction pass
  (`_json_ld_links`) available to both providers via an opt-in flag.
- **`RenderedPage` / `BrowserRenderer` / `_PlaywrightRenderer`** — a small
  renderer abstraction so the provider's discovery logic is independently
  testable from the real Chromium session. `_PlaywrightRenderer` launches one
  Chromium browser/context/page per product/domain, navigates with a bounded
  `"load"` wait plus a short, separately-bounded `networkidle` settle window
  (a plain `networkidle` wait proved unreliable live — see §5), and passively
  captures public JSON XHR/fetch responses the page itself requests via
  Playwright's response listener (no reverse engineering, no forged auth).
- **Bounds enforced**: one renderer per product/domain, `max_pages` (default
  3) navigations, a per-navigation timeout (default 8s), and a global
  per-product deadline (default 20s, configured via
  `DiscoveryRuntimeConfig`/`ResilientSearchSession`'s existing per-provider
  timeout and wall-clock-budget machinery — no new plumbing needed).
- **Captcha/WAF handling**: any blocked page (via `core.fetch._blocked_reason`,
  reused rather than duplicated) immediately ends the browser path for that
  product — recorded as blocked, never bypassed.
- **Wired into `ResilientSearchSession`**: inserted directly after
  `DirectDomainProbeProvider` in the default provider tuple. Because both
  providers set `short_circuit_on_exact_model = True`, the browser provider
  is only ever actually invoked when the HTTP path did not already produce an
  exact-model candidate for that query — verified by
  `test_browser_not_invoked_when_http_already_found_exact_official`.
- **Telemetry**: 8 new `ProviderAttempt` fields (`browser_invoked`,
  `browser_reason`, `browser_pages_opened`, `browser_navigation_seconds`,
  `browser_rendered_candidate_count`, `browser_xhr_candidate_count`,
  `browser_captcha_detected`, `browser_budget_used_seconds`), surfaced
  generically through `_provider_discovery_telemetry` and mirrored into both
  `core/workflow.py:_provider_attempt_data` and
  `diagnostics/live_quality_diagnosis.py:_provider_attempt`.
- **`diagnostics/stage28_browser_smoke.py`** — smoke script reusing the exact
  same `DEFAULT_PRODUCTS` from `diagnostics/stage27_official_smoke.py`, SERP
  disabled, running the HTTP and browser providers independently per brand
  for a direct comparison.

## 3. Changed files

- `core/discovery.py` — new provider/renderer classes, shared extraction
  helpers, new `ProviderAttempt` fields, provider-tuple wiring, health-store
  brand-scoping extended to the new provider.
- `core/workflow.py` — telemetry mirroring.
- `diagnostics/live_quality_diagnosis.py` — telemetry mirroring.
- `diagnostics/stage28_browser_smoke.py` (new) — smoke script.
- `tests/test_stage28_browser_official_discovery.py` (new) — 10 deterministic
  tests.
- `tests/test_discovery.py` — updated default-provider-order assertions to
  include the new provider.

## 4. Tests

`python -m unittest discover -s tests` → **745/745 PASS** (735 pre-existing +
10 new Stage 28 tests). All 9 required scenarios are covered (one extra:
authority is not auto-granted):

1. HTTP path fails -> browser rendered DOM yields exact product.
2. Browser finds product via rendered site search.
3. Browser observes a public JSON/XHR product result.
4. The resulting candidate passes the existing identity/discovery pipeline
   (`discover_with_status`) and is not rejected.
5. CAPTCHA/challenge -> path ends immediately, without bypass, and without
   trying the site-search page.
6. Browser navigation error/timeout -> SERP fallback still runs.
7. Global page budget (`max_pages`) is respected.
8. Browser is not invoked at all when HTTP already found an exact official
   candidate.
9. Discovery never self-declares official authority (`last_accepted_official_url`
   stays `None`), matching `DirectDomainProbeProvider`'s existing invariant.

## 5. Smoke gate: Stage 27 vs Stage 28 (same 5 brands, SERP disabled)

| Brand | Stage 27 HTTP | Stage 28 browser invoked? | Stage 28 browser result | Exact official found | Blocking reason |
|---|---|---|---|---|---|
| Samsung (Galaxy Z Flip6, US) | Exact via sitemap (3 candidates) | No net gain needed — HTTP already exact; browser ran anyway (test harness calls it independently) and hit **captcha** | Blocked | **Yes** (from HTTP) | Browser: captcha at `samsung.com/az/function/ipredirection/...` |
| Dell (XPS 13 9340, US) | No candidate (sitemap byte-limit, search 403) | Invoked | Homepage itself returned **HTTP 403** | No | 403 at `dell.com` homepage |
| Miele (KM 7464 FL, DE) | No candidate (regional root 403, no model on global) | Invoked, 3 pages | `miele.de` homepage **403**; `miele.com` search **404** | No | 403 / 404 |
| Smeg (SI2M7953D, GB) | No candidate (sitemap connect-timeout) | Invoked, 3 pages | Homepage rendered, no exact-model link/XHR found | No | none found (not blocked) |
| PFAFF (ambition 620, global) | No candidate (JS shell, 404s) | Invoked | `pfaff.com` now redirects to `singer.com/pages/pfaff` -> **captcha** | No | captcha at `singer.com/pages/pfaff` |

**Stage 27: exact official 1/5. Stage 28 (browser-backed): exact official
1/5.** No net improvement on this brand set.

## 6. Which cases did the browser actually save

None. For the one brand where Stage 27 already succeeded (Samsung), the gain
came from the existing HTTP sitemap path, not the browser (and the browser
run of Samsung separately hit a captcha). For the other 4 brands, the
blocking condition was upstream of JS rendering in 3/4 cases (Dell/Miele:
WAF/403 on the very first navigation; Smeg: connection timeout) — a headless
browser without stealth/anti-detection (explicitly out of scope) is blocked
by the same WAF signal a plain HTTP client is. PFAFF's original Stage 27
blocker ("JS shell homepage") turned out, live, to no longer be the operative
blocker: the brand's domain now redirects to `singer.com`, which itself
returned a captcha challenge before any rendering distinction could matter.

## 7. CAPTCHA/WAF cases

3 of 5 brands hit an explicit block: Samsung's browser path (captcha, via
`_blocked_reason` marker detection), and PFAFF's browser path (captcha, on
the `singer.com` redirect target). Dell and Miele returned HTTP 403 directly
(handled as a hard failure, not further classified as captcha/WAF text, since
the response bodies were not HTML challenge pages). All are recorded as
`browser_captcha_detected` / an HTTP-403 failure reason; none were bypassed —
each ended the browser path for that product immediately, per the smoke
script's per-brand independent run of the browser provider (in the live
pipeline, `ResilientSearchSession` would simply continue to SERP fallback,
verified by `test_browser_timeout_lets_fallback_continue`).

## 8. Full gate

**Not run.** Per the task's own decision rule, browser-backed exact-official
success must reach at least 3/5 before a full ≥15-product gate is justified;
it remained at 1/5, identical to the Stage 27 baseline. Running the expensive
full gate would not have produced new information.

## 9. Remaining blockers

1. WAF/403 blocking happens on the very first navigation for several brands
   (Dell, Miele) — before any JS execution or rendering distinction is
   possible. No browser-rendering approach that respects the "no anti-bot
   bypass" constraint can move these.
2. CAPTCHA challenges (Samsung, PFAFF/Singer) are explicitly out of scope to
   solve, by design and by task constraint.
3. Brand-derived root-domain resolution still cannot generically discover an
   unrelated consumer domain a brand happens to use (e.g. PFAFF's actual
   current storefront is under `singer.com`, not a PFAFF-labeled domain) —
   this is the same "brand-derived root resolution" limitation Stage 27
   already recorded, and browser rendering does not address it.
4. Smeg's rendered homepage loaded cleanly but exposed no exact-model
   link/XHR within the bounded page budget — a deeper, brand-specific site
   search flow might find it, but that would mean brand-specific automation,
   which the task explicitly forbids.

## 10. PASS / PARTIAL / FAIL

**PARTIAL.** Tests pass (745/745), the architecture is correctly bounded,
captcha/WAF is detected and never bypassed, there is no product-specific
hardcode, and authority/identity/validation are unchanged (candidates from
the new provider go through the identical acceptance path, verified by
`test_candidate_passes_existing_identity_pipeline` and
`test_authority_is_not_auto_granted_by_browser_discovery`). However, the
smoke gate did not reach the required ≥3/5 exact-official threshold (stayed
at 1/5, no case was actually saved by browser rendering on this brand set),
so the full gate was correctly not run and Stage 28 does not clear the PASS
bar.
