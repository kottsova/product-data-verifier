# Stage 31.4 — Official-source priority regression

Trigger: Pixel 9 Pro retest. An exact official page existed and was reachable
(`fi.google.com/about/phones/pixel-9-pro`) but the user-facing profile was built
from GSMArena.

## 1. Diagnosis (live trace, `diagnostics/stage31_4_official_trace.py`)

| Question | Finding |
|---|---|
| Discovered? | **No.** Providers returned `fi.google.com/about/phones/pixel-9-pro` and `.../pixel-9-pro-specs` (Naver/Seznam, title "Get the new Pixel 9 Pro…"), but `rank_candidates` dropped both silently: they appeared in neither candidates nor rejected. |
| Fetched / identity / extracted? | Never reached. The official pages that *were* fetched were useless: `support.google.com` (Korean regulatory help page, `hl=ko`, 85 raw attrs, none usable) and `store.google.com/be/product/pixel_9_pro` (redirects to the phones category page; 4 attrs). |
| Which stage dropped official evidence? | **Discovery** (two root causes below), then **extraction** (the fi page states specs in marketing prose, which no extractor read), then **identity** (a category page was accepted as exact-model official evidence). Authority/dedupe/final selection were correct: `core.validation` already ranks verified official (4) above specialized reference (3). |
| Why GSMArena displayed? | It was the only source with mappable canonical values. Nothing outranked it because nothing official reached validation. |

Discovery root causes (both generic, no Google specifics):

1. `about` is in `BLOCKED_PATH_SEGMENTS` (corporate "About us" filter), so any
   `/about/phones/<model>` URL was discarded before ranking.
2. `candidate_model_match` vetoed a page whose *title* listed a sibling
   ("…Pixel 9 Pro and the Pixel 9 Pro XL") even though the URL slug was exactly
   the requested model.

## 2. Fixes

- `core/discovery.py`: `about` no longer blocks a URL that itself names the
  requested model (`blog`/`news`/… still do). Official-ecosystem re-query with
  already-seen hosts excluded (`"<model>" site:<domain> -site:<host>…`), so a
  busy store/help-centre cannot crowd out a sibling host. No host is hardcoded.
- `core/match.py`: title sibling-veto relaxed only when the URL's last segment
  is exactly the model *and* the title also names the model unmodified. A title
  naming only the variant still vetoes.
- `core/official_source.py` (new):
  - **Official Source Resolution Gate** — the 8 required fields
    (`official_domain_candidate`, `_verified`, `_accessible`,
    `official_exact_product_page_found`, `official_product_page_accessible`,
    `official_fetch_status`, `official_attributes_extracted_count`,
    `official_failure_reason`) plus `official_failure_detail`,
    `official_canonical_attributes_count` (schema fields with usable values
    only) and `official_final_profile_attribute_count` /
    `secondary_final_profile_attribute_count`. Failure vocabulary:
    `domain_not_found`, `domain_verification_failed`, `exact_model_not_found`,
    `inaccessible`, `fetch_failed`, `identity_rejected`, `extraction_failed`,
    `no_usable_canonical_attributes`. Stored in profile metadata
    `official_source_resolution`.
  - Prose-fact extractor for verified exact-model official pages (display size,
    refresh rate, processor, RAM, rear/front camera). Sibling-variant guard:
    with several distinct values it keeps one only if the words before it name
    this model; otherwise it emits nothing.
  - Redirect-away rejection: an official URL that lands on a different page
    not naming the model loses its exact-model identity (`identity_rejected`).
  - Official image selection (https raster only; drops logos/icons/banners/
    svg/small renditions; dedupes sized variants; model-naming alt text first;
    max 10) and auxiliary-link extraction (Review/Pictures/Opinions/Compare/
    Prices; "Related devices" is never surfaced). Stored as metadata
    `product_images`, `auxiliary_links`, never as attributes.
- `core/workflow.py`: wires the above; adds `source_priority` metadata.
- `bot/`: "🔗 Источники" block (official first, then secondary), and a
  "📸 Скачать все фото" button shown only when official image URLs exist
  (chat-scoped, falls back to plain links if Telegram rejects the album).

Validation/authority thresholds were not changed.

## 3. Tests

`tests/test_stage31_4_official_priority.py` (30 tests): official wins
display/source; secondary fills only missing; secondary cannot overwrite;
conflict keeps both, official authority higher; preview lists official first;
image selection prefers official CDN and filters junk; auxiliary links stay out
of attributes; official domain before ranking + sibling-host expansion;
inaccessible official falls back with a stated reason; exact-model mismatch not
accepted; regional (`.co.uk`) domain accepted; the two discovery regressions;
photo button (only with images, albums of 10, link fallback, chat scoping).
Full suite: **832/832 PASS**.

## 4. Pixel RU smoke (Pixel cache row cleared first; 3 other rows untouched)

- `fi.google.com/about/phones/pixel-9-pro` discovered by the pipeline, fetched,
  identity accepted, listed **first** among sources.
- Filled from the official page: display size 6.3", refresh rate 120 Hz,
  processor Google Tensor G4, rear camera 50+48+48 MP, front camera 42 MP.
- GSMArena now supplies only what the official page lacks (resolution,
  Bluetooth, GPU). Confirmed 6 → 10 of 25.
- 10 official product photos found; 5 GSMArena auxiliary links stored apart.

## 5. Remaining

- **RAM stays Unresolved** (`insufficient_identity`): the schema marks RAM
  variant-level and requires an exact-variant page; the official page is
  base-model. Left unchanged (validation rule); decide separately.
- Battery/charging: the fi page gives no mAh/W figure, so they stay secondary
  or unresolved.
- Korean `support.google.com?hl=ko` page is still fetched and yields noise
  (hidden by the Stage 31.1 user-facing filter). Locale-aware selection of
  regional variants is not done.
- `fi.google.com` discovery depends on a provider returning it; in this run
  DuckDuckGo was bot-blocked and Naver/Seznam supplied it.
