# Stage 36.6 — authority and product identity

## Starting point and retained baseline

The checked starting point was clean `main` at `bd9238e`, equal to `origin/main`
after a fetch. No repository instructions (`AGENTS.md`) were present. The
historical Stage 36.5 scratchpad contained 50 JSON results, its original
harness and log. The originals were not overwritten. Sanitized `raw.zip`,
hashes, launch conditions and the restored harness were committed separately
as `b9ba978` before implementation work.

[Stage 36.5 baseline](diagnostics/baselines/stage36_5/README.md) describes the
limits of the archive. In particular, it lacks fetched HTML and PDF bodies.
The harness cleared the process-local official-domain and official-surface
caches between products, created a new service per product and paused two
seconds. Provider health, circuit breakers and network state were not reset.
The apparent “cold run” was cold only for those explicitly cleared caches.
The 50 searches used `global` market and a 75 s per-item service budget.

## Historical reconciliation

The original automatic result was **31/50 PASS**, 36/50 with any listed
official source/document, 26 document records, 729 recorded provider failures
and 2234.6 s of accumulated service time. These are discovery counters;
Stage 36.5 did not run attribute extraction. Saved records contain 80 exact
product-page claims, 4 exact support-page claims and 12 exact document claims
(across 5 queries). Of the 26 document records, 17 have PDF URLs and 9 have
HTML URLs. None of those counts is a manually measured precision.

The [50-row product reconciliation](diagnostics/baselines/stage36_5/reconciliation/products.csv)
and [1,940-row candidate reconciliation](diagnostics/baselines/stage36_5/reconciliation/candidates.csv)
retain every saved automatic decision and a new **title/URL screen**. The
[summary](diagnostics/baselines/stage36_5/reconciliation/summary.json) identifies
6 saved titles/URLs describing a related item, 5 a different package/refill,
11 forum discussions previously in official product sets, 7 unresolved
regional/variant suffixes, 4 search-level rejections needing review, and 1
exact claim needing primary page content. The remaining 1,906 candidates
also need content or operator evidence before a manual verdict. These buckets
are exclusive screen results, not a confusion matrix.

The earlier figures “26 exact product pages / 29 any exact source / 8 exact
documents” came from partial checking and cannot be reconstructed from the
saved response summaries. The reported four erroneous PASS entries are not
the same thing as four products with no valid official page: Roborock, Razer,
Corsair and CeraVe each also have a plausible base-product URL in their saved
set. If 31 and 29 measured the same outcome and four entire PASS products
were false, two missed positives would be needed to reconcile them; their
definitions and complete labels are unavailable, so that arithmetic is only
a conditional observation. The reconciliation marks historical manual verdicts
`not_reconstructible` instead of inventing a ground truth or historical false
negative count.

Input contracts: 23 requests name a SKU/article, 19 a model and 8 a family.
The historical automatic PASS counts are 14, 13 and 4 respectively. A SKU
requires evidence for the actual commercial identifier; a regional-looking
suffix is unresolved without product evidence. A model page must name the
main product rather than an accessory or variant. A family page does not
justify copying one variant's specifications to another. The old harness
parsed `The North Face Nuptse` as brand `The North` and model `Face Nuptse`;
its Kärcher input also contained a replacement character. The restored live
harness defaults to the **same historical input strings** for a fair repeat;
`--structured-inputs` uses `brand | model` and must be reported separately.

## A. Authority and provenance

The existing search-domain bootstrap now produces a short-lived *hypothesis*
with relation, scope, evidence URL/fragment, check time and rule version. Its
one-hour cache never grants verified authority. A manually reviewed, exact
host registry supplies initial trust. Each seed records brand, operator
relation, category/region scope, evidence URL/fragment, review date and rule
version; entries expire after 365 days. It does not cover sibling brands,
TLDs, arbitrary subdomains or CDN hosts. Old `verified` records without a
fresh seed or contextual corroboration are reconsidered after fetch; the
final redirect host is checked again.

The authority resolver no longer demands a brand-looking domain before it
examines a claimed relationship. Self-declared “official”, copyright,
metadata and Product.brand remain clues, not proof. A fetched trusted page
must link to the exact destination host with **role-specific** wording.
“Where to buy” proves neither brand operation nor authorization. A proven
distributor/dealer has its own source tier and cannot confirm a fact alone.
Community discussions retain their host's operator provenance but are
classified as forum content, outside first-party editorial pages. Discovery,
workflow and Stage 34 extraction carry the operator relationship, evidence
and scope forward; a redirect to an unverified host blocks extraction.

The seed ledger is a bounded manual bootstrap, not an inference that two
sites are official because each links to the other. The evidence is scoped
to the named brand and category. Relevant audited examples:

| Case | Evidence reviewed | Decision and scope |
| --- | --- | --- |
| Siemens | [BSH Siemens press portal](https://press.siemens-home.bsh-group.com/) | Licensed home-appliance brand operator on exact host. |
| Haier | [Haier Europe corporate brand](https://corporate.haier-europe.com/our-brands/haier/) and [customer-care host listing](https://corporate.haier-europe.com/our-brands/customer-care/) | European Haier appliance site. |
| Philips | [Philips announcement of Versuni licensing](https://www.philips.com/c-dam/corporate/newscenter/de/press-releases/2023/202302-philips-da-wird-versuni/2302_PRESSEINFORMATION_Versuni.pdf), [Philips HD9876/90 page](https://www.home-appliances.philips/de/de/p/HD9876_90) | Licensed domestic-appliance operator on `home-appliances.philips`; other Philips categories are excluded. |
| Braun | [De’Longhi Braun brand page](https://www.delonghigroup.com/en/brand/braun) | Licensed Braun household appliances. |
| Kenwood | [De’Longhi Kenwood brand page](https://www.delonghigroup.com/en/brand/kenwood) | Kenwood household appliances. |
| Bosch Professional | [Bosch corporate power-tools brochure](https://assets.bosch.com/media/global/products_and_solutions/market_specific_solutions/solutions_for_the_automotive_industry/brochure.pdf) | Bosch professional tools, not every Bosch product host. |
| Frostbite | [Company/about page](https://fishfrostbite.com/pages/contact), [Drench page](https://fishfrostbite.com/products/drench-39ml) | Operator remains unknown; this seed cannot grant first-party. |
| Nautilus | [Company/about page](https://www.nautilusreels.com/pages/about), [company profile linking the site](https://www.linkedin.com/company/nautilusreels) | Operator remains unknown; this seed cannot grant first-party. |
| TP-Link Czech shop | [Store terms naming 100Mega Distribution](https://www.tp-link.cz/cs/static/page/5) | Independent distributor; never first-party TP-Link manufacturer content. |

The limited external corroboration for the smaller Frostbite and Nautilus
operators leaves their operator relationship unknown. Their primary pages
support product identity, but cannot establish first-party status.

## B. Product identity and final source gate

Ranking uses URL/title for bounded candidate selection. A numeric or opaque
product URL with an exact title is now eligible for a **content check** rather
than rejected solely for its path. Final `exact` requires a primary `<h1>` or
unique Product structured-data object naming the requested product. Body-wide
mentions, including compatibility and recommendations, cannot establish it.
General main-object patterns reject accessories, “for model” relations,
bundles/refills/twin packs, distinct named or numeric variants, and unresolved
regional SKU extensions. A complete SKU may contain a typographic space in
the main heading when the same compact code appears in the product URL; this
was checked on Braun `MQ9187XLI` without accepting a neighboring SKU.
An audited host returning only a JavaScript shell remains official with
**unknown product identity**, rather than a rejected source or a false exact.
Support pages and HTML documents require their own
main-object check before an exact label. PDF links are leads until document
identity verification. Stage 34 independently rechecks product identity on
refetch and withholds attributes after a mismatch or untrusted redirect.

Saved negative examples include Roborock bags/brushes/filters, Razer V3
HyperSpeed, Corsair RM850x SHIFT, CeraVe refill and Oral-B twin pack. The
archived Razer/Corsair/CeraVe sets also include the requested base product;
candidate-level errors must be counted even when product-level PASS remains
correct. NETGEAR community discussions are not manufacturer editorial pages.
The old Siemens support absence, Epson support path and Canon support URL
need content-level checking; a search URL alone does not prove an exact
support source. Ubiquiti, Apple, APC and WD remain discovery/verification
gaps where the needed source was not established in the archive. For Namazu,
the correct statement is **not found within the checked search**, not that an
official source does not exist.

The new [pre-registered held-out cases](diagnostics/heldout/stage36_6_cases.json)
use different synthetic brands/products and cover exact main products,
opaque URLs, parts, distinct variants, refill/twin-pack and role-specific
authority links. They are deterministic regression fixtures rather than a
live market-coverage estimate.

## Live repeat and acceptance

The complete sequential repeat used **all 50 identical historical input
strings** on code commit `12c58ef`. Its [sanitized raw archive](diagnostics/baselines/stage36_6/raw_sanitized.zip),
[manifest with raw SHA-256 hashes and conditions](diagnostics/baselines/stage36_6/manifest.json),
[50-row comparison](diagnostics/baselines/stage36_6/comparison.csv) and
[summary](diagnostics/baselines/stage36_6/summary.json) are separate from the
Stage 36.5 archive. The archive passed `python -m diagnostics.stage36_6_report
--audit --output diagnostics/baselines/stage36_6`.

| Automatic discovery measure | Stage 36.5 | Stage 36.6 |
| --- | ---: | ---: |
| PASS / exact official product found | 31/50 | 10/50 |
| Any listed official source or document | 36/50 | 14/50 |
| Exact product-page records | 80 | 10 |
| Exact support-page records | 4 | 0 |
| Exact document records | 12 (PDF/HTML not consistently content-checked) | 1 PDF, 0 HTML |
| Search queries | 252 | 429 |
| Provider failure/empty/circuit records | 729 | 1,203 |
| Accumulated service time | 2,234.6 s | 2,821.7 s |

The 10 new PASS results comprise 4/23 SKU requests, 4/19 model requests and
2/8 family requests. There were **26 PASS→non-PASS** and **5 non-PASS→PASS**
transitions. New PASS includes Siemens, Haier, Braun, Frostbite and Nautilus.
Roborock, Razer, Corsair and CeraVe retain PASS but exclude the related
accessories or commercial variants from their exact product sets. TP-Link's
Czech shop is a verified independent distributor, not first-party. Oral-B
twin packs are no longer exact for a single brush request. Philips stays
official but identity-unknown because its delivered HTML was a JavaScript
shell. Bosch Professional's host is verified, while the historical input's
`Professional` prefix makes final product identity unresolved.

The lower PASS count is **not a quality score**. The small audited-host ledger
does not cover many likely first-party sites (including the Bosch home,
Epson and Canon cases); requiring primary-page content also leaves JavaScript
shells and inaccessible pages unknown. The new run had 429 queries and 1,203
provider outcome records outside success: 445 circuit-open, 206 blocked, 170
capped, 135 low-value, 52 timeout, 4 parse-error, 2 error and 189 empty.
These outcomes are not negative evidence about a manufacturer's site.
Search results and availability also changed between runs, so the 5 gained
PASS results cannot be attributed solely to the code without a saved-body
replay. The archived candidate screen is the fixed-evidence comparison; it
cannot reconstruct main-page identity because HTML/PDF bodies were not saved.

The [structured-input correction](diagnostics/baselines/stage36_6/structured_summary.json)
is a **separate two-row experiment** for Kärcher (row 11) and The North Face
(row 48). Both now parse into the intended brand and model, but both still
return PARTIAL. Their sanitized responses and hashes are in the separate
[archive](diagnostics/baselines/stage36_6/structured_corrections.zip) and
[manifest](diagnostics/baselines/stage36_6/structured_manifest.json). Their
provider conditions followed the full run, so neither row is included in or
directly comparable to the like-for-like 50 denominator.

The [manual accepted-page audit](diagnostics/baselines/stage36_6/manual_exact_audit.csv)
reviewed all **10/10 accepted exact product URLs** against their current
primary product pages. All ten name the requested main product; that is
10/10 observed exact-identity precision **within this accepted-page set**.
Eight have a reviewed brand-operator or licensed-operator relationship.
Frostbite and Nautilus remain operator-unknown because the available
corroboration is chiefly their own description. Thus first-party status is
confirmed for 8/10 accepted URLs, unresolved for 2/10 and contradicted for
0/10; a whole-set first-party precision is not claimed. This current-page
review does not turn the historical Stage 36.5 URLs into adjudicated labels,
measure recall on the 40 new non-PASS rows, or validate all weak official
sources and extracted attributes.

The complete deterministic suite passed **1133 tests** on implementation
commit `12c58ef`; `git diff --check` passed. Focused gates cover provisional
and stale authority, redirects, role-specific links, forum content, positive
and negative product identity, and evidence at Stage 34 extraction. The
necessary first-party coverage and Epson/Canon support cases are still
unresolved. One live row exceeded the nominal 75 s service budget because
the outer request did not interrupt all downstream waits; this is another
runtime limit to address. **Stage 36.6 is PARTIAL**; the PR remains draft.

## Follow-up iteration: transition causes and bounded recovery

The [26-row transition ledger](diagnostics/baselines/stage36_6/transition_causes.csv)
records every old exact URL, whether it occurred in the new raw results and
candidate set, whether it reached the bounded page-check set, the saved
operator and main-object decisions, observed page loads, JavaScript shells,
provider blocks/timeouts and budget overrun. Its generator is
`python -m diagnostics.stage36_6_transition_audit`. The archive contains no
per-page fetch outcome for unpromoted provisional sources: `not_recorded`
means exactly that, not a proven block. An old exact URL reappeared in 25/26
new raw/candidate sets; 18/26 had one in the bounded fetch-selection set.
Only Oral-B has a recorded loaded page among matched old exact URLs. All 26
rows include some provider block, and 23 include a provider timeout; neither
count establishes that the relevant manufacturer's page was blocked.

The initial audit identified eleven **confirmed current false refusals**: rows 1 Bosch,
4 Electrolux, 5 AEG, 6 Miele, 16 ASUS, 22 Logitech, 29 Epson, 30 Canon,
37 Makita, 46 adidas and 47 Nike. The reviewed primary product page and
brand-operator evidence for each are in the ledger. Evidence examples:
[BSH's Bosch UK listing](https://media3.bsh-group.com/Documents/9001351957_A.pdf)
and [WAN28254GB](https://www.bosch-home.co.uk/en/product/laundry/washing-machines/front-load-washing-machine/WAN28254GB);
[Electrolux BG terms](https://www.electrolux.bg/overlays/terms-and-conditions/)
and [EOD6P77WX](https://www.electrolux.bg/kitchen/cooking/ovens/oven/eod6p77wx/);
[AEG France terms](https://www.aeg.fr/overlays/shop-terms-and-conditions/)
and [IKE64441FB](https://www.aeg.fr/kitchen/cooking/hobs/induction-hob/ike64441fb/);
[Miele UK corporate listing](https://www.miele.com/de/com/2185.htm)
and [TWD260WP](https://www.miele.co.uk/product/11871790/t1-heat-pump-dryer-twd260wp-8kg-lotus-white);
[ASUS legal terms](https://www.asus.com/terms_of_use_notice_privacy_policy/official-site/)
and [RT-BE88U](https://www.asus.com/us/networking-iot-servers/wifi-routers/asus-gaming-routers/rt-be88u/);
[Logitech's operator statement](https://www.logitech.com/en-us/legal/services-privacy-statement)
and [MX Keys S](https://www.logitech.com/en-us/shop/p/mx-keys-s);
[Epson Europe terms](https://www.epson.eu/en_EU/terms-of-use)
and [L6270](https://www.epson.eu/en_EU/products/printers/inkjet/consumer/ecotank-l6270-multifunction-wi-fi-ink-tank-a4-printer%2C-with-up-to-3-years-of-ink-included/p/30259);
[Canon U.S.A. corporate relation](https://global.canon/ja/news/2017/20170427-2.html)
and [LiDE 400](https://www.usa.canon.com/shop/p/canoscan-lide-400);
[Makita NZ subsidiary statement](https://www.makita.co.nz/about/)
and [GA023GZ](https://www.makita.co.nz/products/model/GA023GZ);
[adidas UAE terms](https://www.adidas.ae/en/terms.html)
and [JH9073](https://www.adidas.ae/en/ultraboost-5-shoes/JH9073.html);
[Nike operator details](https://www.nike.com/be/help/a/bedrijfsgegevens/nike-contact-lijst)
and [FN4231-010](https://www.nike.com/dk/en/t/aeroswift-mens-dri-fit-adv-running-vest-vSX0Gdly/FN4231-010).
This is a current-page adjudication; it cannot reconstruct the 36.6 network
response. The old exact claims in rows 17 (TP-Link Czech distributor) and 49
(Oral-B twin packs) were invalid. The other 13 losses were still unknown at
this point. Samsung's
`/EF` suffix and DEWALT's `-GB` suffix are examples requiring commercial SKU
evidence, not automatic exact matches. LG C4 and similar family inputs cannot
be described as a particular SKU.

The registry now keeps Frostbite and Nautilus `operator_unknown`, and the
decision paths check the seed's product category. Wrong-category cases for
Bosch Professional, Bosch Home, Philips domestic appliances and TP-Link Czech
retail remain unverified. New exact-host seeds have documented operator
evidence; none were copied from an old PASS alone. Source selection remains
host exact and product identity still requires primary-object content.

The [saved-candidate replay](diagnostics/stage36_6_saved_replay.py) passed
all 11 confirmed loss URLs with short, manually transcribed primary-object
fixtures. This checks the authority and identity decision path, not network
availability. The [accepted-URL re-audit](diagnostics/baselines/stage36_6/accepted_url_reaudit.csv)
checks all ten previously accepted URLs. It found no contradicted operator
or main product, but now withholds Frostbite and Nautilus for operator
uncertainty and NETGEAR GS308EP at SKU level: the fetched unique Product
object identifies commercial code `GS308EP-100NAS`. Corsair RM850x and CeraVe
cleanser are family requests; their accepted pages do not establish an exact
SKU for the family.

| Selected indices with explicit benchmark category | Prior 36.6 queries | Guided queries | Prior seconds | Guided seconds | Guided PASS |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1, 5, 17, 22, 30, 49 | 51 | 22 | 312.8 | 222.0 | 1/6 |
| 1, 16, 22, 37, 46, 47 | 56 | 17 | 402.3 | 297.0 | 5/6 |
| 12, 19, 23, 33, 50 | 15 | 15 | 93.9 | 83.2 | 4/5 |

These guided runs are **not comparable with the historical name-only route**:
the harness supplied `PRODUCTS` category labels to `discover_name`, while the
bot and the Stage 36.5/36.6 historical runs supplied only the product string.
The 104-to-48 query comparison and five live recoveries describe the
category-guided diagnostic only; they do not establish user-route recovery.

For the 15 distinct guided inputs, using the latest run for repeated Bosch
and Logitech rows, results by input level were:

| Input level | Rows | PASS | Prior queries | New queries | Prior seconds | New seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SKU | 7 | 4 | 58 | 20 | 452.5 | 355.5 |
| Model | 6 | 3 | 40 | 22 | 197.7 | 134.2 |
| Family | 2 | 2 | 6 | 6 | 51.4 | 34.2 |

The family results mean an exact main-product family page, not an exact
commercial SKU. Among the 11 confirmed false refusals, all 11 pass the saved
decision replay; five (Bosch, ASUS, Logitech, Makita and Nike) also recovered
in selected live runs. Three selected confirmed losses (AEG, Canon, adidas)
remained PARTIAL because the page fetch was unavailable, and three
(Electrolux, Miele, Epson) were not selected for a live rerun.

The selected raw runs were sanitized into separate [first](diagnostics/baselines/stage36_6/limited_live_20260920_sanitized.zip),
[follow-up](diagnostics/baselines/stage36_6/limited_live_followup_20260920_sanitized.zip)
and [negative](diagnostics/baselines/stage36_6/limited_live_negative_20260920_sanitized.zip)
archives with hash manifests in adjacent JSON files. Bosch, ASUS, Logitech,
Makita and Nike passed live. AEG, Canon and adidas remained PARTIAL because
their pages were unavailable to the fetcher in these selected runs. The
negative run rejected Roborock accessories, Razer HyperSpeed, Corsair SHIFT,
and CeraVe refill; Oral-B twin packs and NETGEAR forums were also excluded.
The Logitech run exposed two `Combo` false exact records; a subsequent run
accepted only the base MX Keys S page. The Bosch run exposed a review score
after the SKU misread as a variant; the subsequent run passed. NETGEAR's
commercial suffix caused a deliberate downgrade, as above.

The query increase from 252 to 429 in the original complete repeats was
mostly a change in early stopping: an unverified operator could not stop the
identity query plan. The old/new query mix was identity 85/124, official
36/100, site-restricted 17/72, specs 16/37, specifications 14/37 and
documents 84/59. No row repeated an identical query string, but candidate
duplicates rose from 2,248 to 5,902 across the saved run, showing repeated
retrieval of the same leads. The plan also included near-duplicate `specs` /
`specifications` searches plus document queries without an official host.
The latter two requests were removed; document queries now require a verified
host. The selected guided comparisons cannot be extrapolated to the 50-item
name-only baseline. Page fetch outcomes and actual elapsed time are now
recorded. The synchronous external fetch/Playwright stack cannot forcibly
interrupt active work at precisely 75 seconds; results explicitly expose
`timeout_can_interrupt_active_requests=false` and
`budget_overrun_seconds`. One selected Nike run took 76.9 seconds. A late
fetch now needs eight seconds of remaining budget, but a hard deadline would
require a separately managed worker process and a new comparable benchmark.

The complete suite passed **1,141 tests** and `git diff --check` passed after
this iteration. The 429-query / 2,821.7-second full-run figures remain the
last complete 50-item comparison. A fresh full 50 was not run: 13 transition
causes remain unverified from the saved evidence, some confirmed pages are
still inaccessible to the fetcher, and a strict interruptible 75-second
deadline remains open. **Stage 36.6 remains PARTIAL and PR #1 remains draft.**

## Name-only route correction after review of commit `334ad11`

The benchmark harness in `334ad11` mistakenly sent each row's labelled
`PRODUCTS` category to `discover_name`. The historical 50-item run and the
Telegram `/discover` and discovery-only text paths send the product string
without that argument. With category `unknown`, `find_seed` correctly refused
every scoped seed, so the apparent guided recoveries did not establish
production recovery. The harness now defaults to `discover_name(name)`;
`--explicit-category` is an opt-in counterfactual, recorded in each row.

Before the correction, a public-method replay of the 11 confirmed loss URLs
with saved candidate leads and short current-page primary-object fixtures
returned **0/11 PASS without category**, versus **11/11 with explicit labelled
category**. It used a local saved-result provider, not the live provider chain;
the original Stage 36.6 archive lacks page bodies. This controlled replay
proves the contract gap, not historical network availability.

The production name-only path now treats an exact audited host as a bounded
*inspection lead*, never as verified authority. It fetches at most two such
leads during the search plan. Early stopping requires the fetched page to
identify the exact primary product, a single supported category from the
primary heading, matching Product object or product-specific title, and a
first-party seed whose category includes that observed category. Search
snippets and navigation are excluded. A page with conflicting or ambiguous
category stays unverified; a known out-of-scope host cannot regain authority
through contextual links. Explicit-category pages are also downgraded if
their fetched main product contradicts the supplied scope, or if a
multi-category seed has no product category evidence.

The [public route replay](diagnostics/stage36_6_public_replay.py) and its
[saved results](diagnostics/baselines/stage36_6/public_name_only_replay_20260920.json)
exercise the same `DiscoveryDebugService.discover_name(name)` method as the bot,
with a saved candidate provider and abbreviated page fixtures:

| Saved check | Name only | Explicit category | Interpretation |
| --- | ---: | ---: | --- |
| 11 confirmed loss pages | 11 PASS | 11 PASS | All scoped product decisions now recover without the benchmark label |
| 10 previously accepted URLs | 6 PASS | 7 PASS | Roborock's current primary header/title omit the vacuum category; NETGEAR is SKU-uncertain and Frostbite/Nautilus have unconfirmed operators |
| 6 related-item/variant/forum URLs | 0 exact official | 0 exact official | Accessories, HyperSpeed, SHIFT, refill, twin pack and forum stay outside exact official |

The public service and bot path tests cover Bosch, ASUS, Logitech, Makita and
Nike without a category argument. Negative tests cover TP-Link's independent
Czech distributor, Bosch Professional on a washing-machine page, Philips
domestic appliances on a toothbrush page, an ASUS page outside the two
audited categories, and a search title that says router while the fetched
primary object has no category. The category extractor intentionally leaves
Roborock's currently fetched `S8 MaxV Ultra with 8-in-1 RockDock Ultra`
header/title unknown: neither calls the product a vacuum. Its guided PASS
depends on the explicit category, and the ordinary route remains PARTIAL.

The selected live runs used the **same 15 archived input strings**, a fresh
service per row, reset search hypothesis cache, default provider chain,
global market and nominal 75-second service budget. Both new runs omitted
`product_category`. The older guided runs supplied `PRODUCTS` categories and
were collected under a different network state. Raw outputs were sanitized
into [before](diagnostics/baselines/stage36_6/live_user_route_prefix_20260920_sanitized.zip),
[after 15](diagnostics/baselines/stage36_6/live_user_route_scoped_20260920_sanitized.zip)
and [final Nike/Corsair](diagnostics/baselines/stage36_6/live_user_route_final_20260920_sanitized.zip)
archives with adjacent hash manifests. Per-row statuses, queries, durations
and accepted URLs are in the [initial comparison](diagnostics/baselines/stage36_6/name_only_route_comparison.csv)
and [latest comparison](diagnostics/baselines/stage36_6/name_only_route_final_comparison.csv).

| Input level | Rows | Name-only before PASS | Name-only after 15 PASS | Latest name-only PASS | Older guided PASS |
| --- | ---: | ---: | ---: | ---: | ---: |
| SKU | 7 | 0 | 3 | 4 | 4 |
| Model | 6 | 0 | 3 | 3 | 3 |
| Family | 2 | 0 | 2 | 2 | 2 |
| **Total** | **15** | **0** | **8** | **9** | **9** |

The single 15-row name-only batch moved from **140 to 104 queries** and
**834.0 to 707.4 seconds**. Replacing its Nike and Corsair rows with the
final targeted reruns gives **93 queries, 679.5 seconds and 9 PASS**. The
older category-guided 15-row view was **48 queries, 523.9 seconds and 9
PASS**; that comparison remains a separate diagnostic, not an estimate of
name-only performance. The original archived 15-row Stage 36.6 subset was
104 queries and 701.6 seconds. Network conditions and code changed between
runs, so these selected comparisons do not establish a 50-item speedup.

Name-only live PASS now includes Bosch, ASUS, Logitech, Makita and Nike among
the 11 confirmed losses. AEG, Canon and adidas remained PARTIAL on selected
live checks; Electrolux, Miele and Epson were not included. Razer's base
DeathAdder V3 and Corsair's base RM850x page passed, while HyperSpeed and
SHIFT remained outside exact. Oral-B passed on an individual iO Series 10
toothbrush page; its three twin-pack candidates stayed outside exact. The
NETGEAR forum and commercial SKU variant stayed outside exact. Corsair's
search `position` and `queryID` parameters are now removed during URL
canonicalization; its final rerun produced one accepted base URL. The family
results (Corsair, CeraVe) establish the primary family product, not an exact
commercial SKU.

The 75-second limit still cannot interrupt a running external request;
selected rows again exceeded it. A Playwright helper emitted an `EPIPE` during
cleanup after the 15 row files were written. This remains a runtime concern.
The 13 historically unknown transitions and inaccessible AEG/Canon/adidas
pages still prevent a complete cause audit. A full 50-item repeat is needed
before claiming aggregate Stage 36.6 performance, but is deferred until those
causes and interruptibility are resolved. Stage 36.6 remains **PARTIAL**;
PR #1 stays draft. The complete suite passed **1,149 tests**; Python
compilation and `git diff --check` passed.

## Follow-up: remaining name-only losses and fetch evidence

This follow-up started from clean `8675fd3`, matching the remote branch and
PR head. All live calls below used the archived input string and
`DiscoveryDebugService.discover_name(name)` with no category argument. The
first three-row attempt ran inside a network-restricted sandbox and produced
socket-denial `FAIL` responses; it is excluded from product comparisons.
The archived network-enabled runs and hash manifests are
[remaining three](diagnostics/baselines/stage36_6/name_only_remaining_20260920_sanitized.zip),
[fetch trace](diagnostics/baselines/stage36_6/name_only_fetch_trace_20260920_sanitized.zip),
[unknown before](diagnostics/baselines/stage36_6/name_only_unknown_probe_20260920_sanitized.zip),
[unknown after](diagnostics/baselines/stage36_6/name_only_unknown_scoped_20260920_sanitized.zip),
[Einhell host hint](diagnostics/baselines/stage36_6/name_only_einhell_hint_20260920_sanitized.zip),
and [Tefal reference](diagnostics/baselines/stage36_6/name_only_tefal_reference_20260920_sanitized.zip).
The adjacent JSON summaries state inputs, identity levels, statuses, query
counts, durations, and sanitized archive SHA-256 values.

The [updated transition ledger](diagnostics/baselines/stage36_6/transition_causes.csv)
has **13 confirmed current false refusals**, **2 invalid old exact claims**,
and **11 historically unknown** transitions among the original 26. The two
newly confirmed current refusals are model-level Einhell TC-PL 750 and STIHL
MS 182. In a network-enabled run before the scoped seeds, their product
pages loaded and named the requested main model, but neither operator was
verified. [Einhell UK's company page](https://www.einhell.co.uk/about-us/)
identifies its UK subsidiary and tool range; its [TC-PL 750 page](https://www.einhell.co.uk/p/4345310/)
names the planer and item number 4345310. [STIHL UK's terms](https://www.stihl.co.uk/en/legal-info/terms-of-use)
identify Andreas Stihl Limited as site operator; the [MS 182 page](https://www.stihl.co.uk/en/p/chainsaws-ms-182-petrol-chainsaw-145794)
names the chainsaw. The new seeds cover only Einhell power tools and STIHL
garden/outdoor tools. Product category must come from the fetched primary
object; wrong-category and unknown-category tests stay outside first-party.
This is current evidence, not a reconstruction of the missing historical
fetch responses.

The revised [public saved-candidate replay](diagnostics/baselines/stage36_6/public_name_only_replay_followup_20260920.json)
passes **12/13** confirmed pages without a category. Einhell UK is the one
absent from the original Stage 36.6 candidate archive; a separate public
method fixture passes when that reviewed URL is supplied. A direct fetch of
the current UK page also confirms exact `TC-PL 750`, the `power tools`
category from the primary product, and the scoped operator seed; the normal
search did not supply this page in its latest run. The previously
accepted ten URLs and six related-item/variant/forum negatives keep their
earlier results: 6/10 accepted PASS without category, 7/10 with explicit
category, and 0/6 negative exact official in either mode. Frostbite and
Nautilus remain operator-unknown.

| Current name-only live check | Result | Queries | Seconds | Observed cause |
| --- | --- | ---: | ---: | --- |
| Epson L6270, SKU | PASS | 3 | 19.1 | Fetched exact Epson product and verified European operator |
| Electrolux EOD6P77WX, SKU | PARTIAL | 12 | 68.2 | Bulgarian product GET timed out after 13.0 s; other candidates were not fetched before budget exhaustion |
| Miele TWD260WP, SKU | PARTIAL | 9 | 58.0 | UK, UAE and French product GETs returned HTTP 403 |
| Einhell TC-PL 750, model | PARTIAL | 8 | 37.4 | Reviewed UK product URL absent from this search's candidates, including the bounded site query |
| STIHL MS 182, model | PASS | 3 | 13.3 | Fetched exact model, observed outdoor-tool category, confirmed UK operator |
| Tefal FV9845, labelled SKU | PARTIAL | 10 | 41.3 | Fetched product cards carry the distinct commercial reference FV9845G0 |

The Tefal result revealed a latent false-exact risk: a primary heading can
show base model `FV9845` while the same main card states `Reference: FV9845G0`.
The identity check now uses such a nearby, unambiguous reference only to veto
an exact SKU decision; references under unrelated-product headings do not
count. The final live run classified all four fetched Tefal pages as different
variants. Tefal was not added to the authority registry merely because an
official-looking domain or an operator statement exists.

For fetch diagnostics, both slash spellings now share a single 12-second
request allowance; each attempt records timeout, HTTP block/error, content
type rejection, or load. Loaded pages also record main-product relation,
observed category, scoped-seed result, and JavaScript-shell status. The
Electrolux attempt fell from 25.6 s before this fix to 13.0 s after it;
requests can still exceed a nominal socket deadline, and the 75-second
service budget still **cannot interrupt** an active external call. The
runtime result continues to report this limit explicitly. The two
Electrolux/Miele checks used 18 queries and 136.3 s before the fetch change,
then 21 queries and 126.2 s after it; network variation and extra queries
prevent a general speed claim. The selected Einhell/STIHL/Tefal three-row
probe moved from 33 queries, 164.1 s, 0 PASS to 22 queries, 98.9 s, 1 PASS
after the two scoped seeds; that is a selected-run comparison only.

Across the selected live checks so far, seven distinct confirmed losses have
passed without category: Bosch, ASUS, Logitech, Makita, Nike, Epson, and
STIHL. Einhell remains a search-retrieval miss; Electrolux and Miele are
fetch-limited; AEG, Canon, and adidas remain inaccessible on their selected
runs. The 11 historical unknowns still lack response bodies, and a strict
interruptible 75-second deadline remains unresolved. A full 50-item rerun
would mix those unresolved conditions into aggregate figures, so Stage 36.6
remains **PARTIAL**, PR #1 remains draft, and Stage 37 and retailer fallback
remain deferred.

## Complete 50-row name-only measurement

The full rerun now supersedes the preceding deferral. Results, evidence, limits and verdict are in [the complete Stage 36.6 measurement](STAGE36_6_FULL_NAME_ONLY_20260920.md). The 11 historical transition causes remain unknown; PR #1 remains draft.
