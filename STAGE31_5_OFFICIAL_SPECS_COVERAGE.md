# Stage 31.5 — Official specs coverage + rich source UX

Trigger: Pixel 9 Pro RU retest. The official spec page
`fi.google.com/about/phones/pixel-9-pro-specs` was reachable, but the profile
still showed 8/23 fields, and the GSMArena rich preview and the photo button
were gone.

## 1. Diagnosis (Pixel 9 Pro, live)

| Question | Finding |
|---|---|
| Discovered? | **Unreliable.** Providers surfaced it in some runs (DDG blocked; Naver/Seznam supplied it) and not in others. The retest run only got the marketing page `/pixel-9-pro`, which *links* to it ("Tech Specs"). |
| Ranked/accepted as exact official? | When surfaced, yes (`manufacturer`, verified, `exact`). One latent bug: a family title ("…Pixel 9 Pro and the Pixel 9 Pro XL") vetoed the URL `…/pixel-9-pro-specs`, because only a slug *equal* to the model counted; a `-specs` suffix was not recognised as "a page of that product". |
| Fetched? | Yes when selected (HTTP 200, 1.9 MB HTML, one accessible comparison `<table>`). |
| Sections parsed? | The page is one table: `th scope=col` = Pixel 9 Pro / Pixel 9 Pro XL, `th scope=colgroup` = sections (Display, Dimensions and Weight, Battery and Charging, Memory and Storage, Processors, Rear/Front Camera, Materials and durability, OS, Ports, Wireless & Location, …), `td headers=` binds each cell to a column. The generic extractors flattened it into **misaligned junk** (`Battery Share = 16 GB RAM`; `Rear Camera = 8K video recording…` from the *Video* section) — 23 raw attributes, almost none mappable, some wrong. |
| Canonical fields extracted? | 5–6 (from marketing prose). |
| Lost where? | **Extraction** (column-blind readers), then **validation**: RAM/storage/SIM/charging/OS are `variant_level`, which requires `exact_variant` identity; an official page for the model is `same_base_model`, so a correct value became Unresolved. Mapping/export were correct. Korean help-centre sentences mapped onto `wifi` produced a false Conflict. |

## 2. Fixes

- `core/official_spec_table.py` (new): generic model-column-bound section
  extractor. Picks the column whose header **is** the requested model
  (`Pixel 9 Pro XL` is never the `Pixel 9 Pro` column; a model absent from the
  columns yields nothing; unlabeled columns contribute only values they all
  agree on). Classifies each section heading **by meaning** (regexes over
  heading words, not selectors) and reads canonical facts from that column's
  lines by value shape: display size/type/resolution/refresh, dimensions
  (H×W×D, metric preferred), weight, battery (typical over minimum), charging
  power, RAM, storage options, processor (not the security coprocessor), rear
  camera (lens roles, camera section only), front camera, IP rating, OS, USB,
  Wi-Fi, Bluetooth, SIM. Emits schema-alias labels; no brand/product value is
  hardcoded. `official_spec_table` metadata records tables, columns, section
  roles and facts.
- `core/workflow.py`: bound facts replace the generic table/spec-block reads of
  that page and take precedence over marketing prose. An accepted official
  product page's own **Tech Specs link** is followed (same registrable domain,
  reads as a specs link, path names *exactly this model* — never a sibling such
  as `…-xl-specs`) and fetched as an unverified-identity official candidate.
- `core/extract.py`, `core/mapping.py`, `core/validation.py`: `model_wide` flag.
  A value stated for the exact model column without a variant qualifier is
  accepted for variant-level fields at `same_base_model` identity; unbound facts
  are still rejected. CJK/Hangul/Kana strings are no longer "concrete" values,
  so help-page prose cannot create a false Conflict.
- `core/match.py`: a URL whose last segment is the model plus a page-role word
  (`-specs`, `-overview`, …) still names that model.
- Images: `product_image_records` (`url`, `role`, `source`). Official first;
  with fewer than 4 official photos the album is topped up from an exact-model
  secondary reference page (og image / model-naming alt only), labelled
  `secondary`. https only; no logo/icon/banner/sprite/svg; deduplicated; max 10;
  oversized CDN renditions clamped to 2048 px (Telegram limits).
- Bot preview = `Brand Model / Category / Найдено X/Y / Sources`, then only found
  `Attribute = Value` lines, one compact conflict warning at the bottom, no
  unresolved list. The result message has link preview off. Below it a
  **secondary card** (`📎 …`) keeps the GSMArena-style rich preview (Telegram
  `link_preview_options.url` = the secondary page) with Review / Opinions /
  Compare / Pictures / Prices. "Related devices" and site-wide index links
  ("Reviews", "Videos") are never shown. `📸 Скачать все фото` appears whenever
  photo records exist; the prompt states official/secondary counts and each
  album is captioned with its source kind and host.
- RU (`bot/localize.py`): `Смартфон`, `6,3 дюйма`, `1280 × 2856 пикселей`, `16 ГБ`,
  `… / 1 ТБ`, `мА·ч`, `Вт`, `Гц`, `г`, `Мп`, Да/Нет, camera roles, SIM phrases.
  Chip names, USB Type-C 3.2, Wi-Fi 7, Bluetooth 5.3, IP68 and model codes are
  untouched. Applied to preview and CSV; EN unchanged.
- Found count = confirmed user-facing attributes = populated columns of the wide
  CSV (test-enforced).

## 3. Photo button

The button was wired, not broken: it needs photo records. The retest run had none
because only pages that were actually fetched contribute photos, and only the
marketing page had been fetched; a result cached before Stage 31.4 has none
either. Now the specs page (fetched via the Tech Specs link) contributes photos,
and secondary photos top up a short album.

## 4. Tests

`tests/test_stage31_5_official_specs.py` plus updated expectations in existing
bot tests (concise preview, RU units, RU category). Full suite: 877/877 PASS.

## 5. Pixel RU smoke (Pixel cache row cleared; 3 other rows untouched)

`diagnostics/stage31_5_smoke.py`: 8/23 → **20/23**. Official
`…/pixel-9-pro-specs?hl=en-US` first, GSMArena second; GPU (Mali-G715 MC7) from
GSMArena; 10 official photos; secondary card with GSMArena
review/opinions/compare/pictures/prices; `/export` wide CSV is one row with
20 populated cells + 3 «Не найдено» (colour, package dimensions, gross weight —
not on the page).

## 6. Not changed / open

- Storage is the option list the official page states
  (`128 ГБ / 256 ГБ / 512 ГБ / 1 ТБ`), not one configuration.
- The bot chooses which URL Telegram previews; whether the preview renders
  (Instant View) is up to Telegram.
- Cached results from before this stage keep their old attributes until the TTL
  expires or the row is cleared.
- Dell / Smeg not started (per instruction).
