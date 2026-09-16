# Stage 18.4 — Discovery Recovery

## Result

**PASS.** Generic discovery was restored for Bosch PUE611BB5E, Janome Sakura 95,
and Gressel GAF-1825 without product-specific routing, relaxed authority rules,
or weaker wrong-model rejection.

The live quality gate remains downstream-limited. Janome and Gressel now have
relevant accepted and selected sources, but source authority/fetch availability
still prevents strong validation. Those issues are intentionally left for a
separate stage.

## Diagnosis

The controlled pre-change run localized the first loss before production edits:

| Product | Exact loss point | Evidence | Confidence |
|---|---|---|---|
| Bosch | A/F: Google timed out, DDG Lite was blocked, and the remaining Naver route returned only brand/noise results | 109 Naver appearances on the first query, 71 globally deduped candidates, 0 accepted | High |
| Janome | A/F: usable product pages existed, but no working route returned them; Naver results reached ranking and were correctly rejected | 352 parsed appearances in the baseline trace, 114 globally deduped, 0 accepted; 87 exact-model-absent rejections | High |
| Gressel | A/F plus noisy provider result quality, not a permissive-ranking problem | 106 parsed appearances, 30 globally deduped, 0 accepted; 4 explicit person/sports rejections | High |

A controlled transport A/B then isolated an additional deterministic loss:
the DDG HTML endpoint returned ten exact product results through `requests`, but
the same URL was bot-checked through the previous `urllib` transport. A second
F-class loss was that the broad `brand official website` query ran before exact
identity queries, so a bootstrap bot-check could open the request-local circuit
before a provider saw the product query.

No evidence showed that relevant candidates were being lost by the existing
model/article gate. Wrong-model, accessory, and person/sports rejection remains
enabled.

## Discovery architecture changes

- Added a generic `duckduckgo_html` HTTP provider before the existing DDG Lite
  and Naver fallbacks.
- The new provider uses the already-declared `requests` dependency because the
  live transport A/B proved that this route avoids the `urllib`-specific block.
- Added a deterministic HTML parser for multiple result title/snippet layouts.
- Preserved raw URL, normalized URL, redirect URL, title, snippet, provider,
  query, rank, parse status, and parse confidence in the normalized candidate
  contract.
- Exact brand/model queries now run before the broad official-site bootstrap.
  The authority queries still run, authority thresholds are unchanged, and the
  request-local circuit breaker remains unchanged.
- Added provider telemetry for HTTP/browser transport and raw, parsed, and
  provider-deduped counts. The diagnostic also reports global dedupe and
  accepted/rejected totals.
- Candidate dedupe, cross-provider provenance, relevance scoring, authority
  semantics, the 90-second workflow budget, and targeted-search limits were not
  weakened.

Query generation did not require product-specific expansion. The existing
generic forms already cover brand + exact model, quoted brand/model, quoted
model + brand, specifications, article/SKU forms, and authority bootstrap.

## Deterministic regression tests

| Failing condition | Fix | Passing test |
|---|---|---|
| DDG HTML organic layouts were unsupported | Multi-layout parser with snippet and redirect preservation | `test_duckduckgo_html_parser_normalizes_layouts_and_snippets` |
| Organic anchors could disappear without raw/parsed distinction | Raw anchor count plus parse-error handling | `test_duckduckgo_html_parser_counts_unparseable_organic_links` |
| The stable HTML route was absent from the provider chain | Added bounded `duckduckgo_html` provider | `test_default_session_includes_independent_duckduckgo_html_route` |
| `urllib` was bot-checked while `requests` returned results | New provider uses `requests` | `test_duckduckgo_html_provider_uses_requests_transport` |
| Authority bootstrap could poison the circuit before exact identity discovery | Exact identity queries precede authority bootstrap | `test_exact_identity_query_precedes_authority_bootstrap` |
| Provider tracing could not separate raw/parsed/deduped or HTTP/browser | Extended `ProviderAttempt` contract and trace output | `test_provider_attempt_reports_raw_parsed_deduped_and_transport_counts`, diagnostic contract test |

## Live results

All full runs used `force_refresh=True`, no repository/shared cache,
`max_sources=5`, a 90-second workflow budget, and a 120-second outer timeout.

### Bosch PUE611BB5E

- Stage 9: 77.8%.
- Stage 18.2/18.3 baseline: final run 0 candidates / 0%; separate volatile run
  11 candidates / 50%.
- Stage 18.4: 186 raw, 186 parsed, 115 provider-deduped, 99 globally
  deduped; 12 accepted and 87 rejected.
- Selected/fetched domains: `bosch-home.co.uk` (success),
  `bosch-home.com` (success), `galaxus.ch` (blocked), `otto.de` (error),
  `mediamarkt.de` (blocked).
- Category: `cooktop/high`.
- Quality: 8/18 expected fields, 44.4%, 0 confirmed, 18 unresolved,
  0 conflicts, `insufficient`.
- Runtime: 47.236 s, 42.765 s budget remaining.
- Exact run: 2026-09-16T10:55:12.753659Z to
  2026-09-16T10:55:59.989993Z.

### Janome Sakura 95

- Stage 9: 68.4%.
- Stage 18.2/18.3 baseline: 114 unique results, 0 accepted, 0%.
- Stage 18.4: 221 raw, 221 parsed, 124 provider-deduped, 99 globally
  deduped; 11 accepted and 88 rejected.
- Selected/fetched domains: `janome.club` (success), two `dns-shop.ru`
  pages (blocked), `sewing-world.ru` (success), `market.yandex.ru` (blocked).
- Category: `sewing_machine/high`.
- Quality: 2/19 expected fields, 10.5%, 0 confirmed, 19 unresolved,
  0 conflicts, `insufficient`.
- Runtime: 69.876 s, 20.129 s budget remaining.
- Exact run: 2026-09-16T10:56:06.919372Z to
  2026-09-16T10:57:16.795679Z.

### Gressel GAF-1825

- Stage 9: 29.4%.
- Stage 18.2/18.3 baseline: 30 unique noisy results, 0 accepted, 0%.
- Stage 18.4: 111 raw, 111 parsed, 64 provider-deduped, 44 globally
  deduped; 14 accepted and 30 rejected.
- Product results dominate the accepted set. The top candidates are
  `gressel.ru`, `dns-shop.ru`, and `market.yandex.ru`; person/sports results
  remain rejected.
- Fetch results: both `gressel.ru` URLs returned 404, both `dns-shop.ru` URLs
  required login, and `market.yandex.ru` was blocked.
- Category: `air_fryer/high` from discovery context.
- Quality: 0/17 expected fields, 0%, 0 confirmed, 17 unresolved,
  0 conflicts, `insufficient`.
- Runtime: 51.895 s, 38.106 s budget remaining.
- Exact run: 2026-09-16T10:57:23.790923Z to
  2026-09-16T10:58:15.686238Z.

The zero Gressel coverage is now a fetch/evidence problem, not `0 candidates`.

## HONOR and Dreame regression

- HONOR X8d: `smartphone/high`, 29 accepted candidates, five selected sources,
  three successful fetches, 11/23 fields (47.8%) versus Stage 18.3 2/23
  (8.7%). The budget stopped targeted search at 90.52 s; outer timeout was not
  reached.
- Dreame G12 Pro HHR32A repeat: `wet_dry_vacuum/high`, 19 accepted candidates,
  exact G12 Pro/HHR32A sources selected, canonical mapping recovery present,
  8/18 fields (44.4%) versus Stage 18.3 9/18 (50.0%). Four of five selected
  sources were externally blocked in the repeat, versus four successful fetches
  in the Stage 18.3 baseline. The small coverage difference is therefore
  explained by live fetch volatility rather than deterministic downstream
  regression. Runtime was 73.719 s.

An earlier Dreame repeat had all five selected sources blocked and produced no
category. The immediate independent repeat restored the required category and
mapping as soon as one source (`dreame.ua`) was fetchable, confirming the
external nature of the first result.

## Runtime and reliability

- Google browser discovery timed out at its 25-second provider deadline and its
  request-local circuit opened.
- DDG HTML returned exact product results for the first identity-bearing
  queries. Later bot-checks remained visible as `blocked`; the circuit then
  prevented repeated cost.
- DDG Lite and Naver remain distinguishable fallbacks.
- No target full run exhausted the 90-second workflow budget or the 120-second
  outer timeout.
- No authority, validation, quality, or wrong-model threshold changed.

## Remaining issues

- Deterministic fixed: missing stable DDG HTML route, transport-specific block,
  brittle single-layout parsing, authority-bootstrap query ordering, and
  incomplete provider/result-count telemetry.
- Deterministic still open: none demonstrated inside the Stage 18.4 discovery
  scope.
- External/provider-related: Google timeout, late DDG bot-checks, retailer
  captcha/login/access blocks, and Gressel manufacturer URLs returning 404.
- Downstream/authority-related: accepted Janome/Gressel pages remain unknown
  authority, so extracted facts do not confirm; Gressel had no fetchable source
  in the measured full run.

## Verification

- `python -m unittest discover -s tests`: 599/599 PASS.
- Focused discovery/diagnostic tests: 80/80 PASS.
- Compile/import and `git diff --check` are recorded in the final handoff.

## Recommendation

Next scope should be source fetch/authority recovery for already-discovered
Janome and Gressel pages: verify evidence-based manufacturer domains, recover
changed/redirected product URLs, and improve fetch access classification. Do not
compensate by weakening identity or authority thresholds. That work was not
started in Stage 18.4.
