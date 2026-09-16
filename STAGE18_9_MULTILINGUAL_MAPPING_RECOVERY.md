# Stage 18.9 — Multilingual Mapping Recovery

Date: 2026-09-16

Verdict: **PASS**.

Confirmed and fixed the exact bottleneck flagged at the end of Stage
18.8: Dreame's real Russian spec labels (`Мощность Вт`, `Ёмкость батареи`,
`Уровень шума`, `Источник питания`, `Размеры (ШxВxТ) мм`, `Воздушный
фильтр HEPA`) fetch and extract correctly but fail to resolve against the
`wet_dry_vacuum` schema because only fuller Ukrainian/Georgian phrasings
were registered as aliases. Fixed with six generic, category-scoped
alias/normalization additions and two new canonical fields, all justified
by the checklist in the task prompt (rated power, battery capacity,
battery type, noise, filtration, generic dimensions), all tests-first.
Every previously-unmapped raw label that is a genuine marketing bullet,
composite value, or out-of-scope concept (side brush presence, dust-bin
full indicator, share/review-widget noise, etc.) correctly remains
unmapped — no field was invented just to raise the coverage percentage.

## 1. Dreame mapping diagnosis

Live baseline trace (`diagnostics/results/dreame_18_9_baseline.json`,
pre-fix): `raw=31`, and because `core/schema.py::extend_schema_with_discovered`
auto-vivifies a "discovered" field for every unresolved label using the
label itself as both name and alias, the trace's own `mapped_count: 31,
unmapped_count: 0` is misleading — everything trivially "maps" to a
label-shaped shadow field. The real question is how many raw labels
resolved to an actual `wet_dry_vacuum`/universal canonical field. Under
the pre-fix code, replaying the exact same 31 raw attributes through
`map_attributes(..., category="wet_dry_vacuum")` gives only **3 real
fields present** out of 18 expected (`brand`, `manufacturer_article`,
`color`) — everything else fell through to the discovered shadow.

Full per-label classification (reasons per the task's A–I scheme):

| Raw label (RU) | Meaning | Pre-fix | Reason | Stage 18.9 outcome |
|---|---|---|---|---|
| Мощность Вт | rated power | unmapped | A: no bare RU alias for `rated_power` | **mapped → rated_power** |
| Ёмкость батареи | battery capacity | unmapped | A: only UA/KA aliases existed | **mapped → battery_capacity** |
| Размеры (ШxВxТ) мм | product dimensions | unmapped | B+C: bracket legend letter (Т) and trailing unit not recognized as one suffix | **mapped → dimensions** |
| Уровень шума | noise level | unmapped | H: no canonical field existed | **mapped → new `noise_level`** |
| Источник питания | battery type/chemistry | unmapped | H: no canonical field existed | **mapped → new `battery_type`** |
| Воздушный фильтр HEPA | HEPA filter present | unmapped | H: no canonical field existed | **mapped → new `hepa_filter`** |
| Объём пылесборника | dust bin (0.5 L) *and* clean-water tank (1 L) in one string | unmapped | E: composite, two concepts in one value | **left unresolved (correct)** |
| Тип пылесборника | dust-bin type ("combined") | unmapped | G: genuinely new, not in task checklist | **left unresolved (in scope discipline)** |
| Уборка | "dry and wet" (restates category) | unmapped | F: ambiguous / duplicates category info | **left unresolved** |
| Таймер (value "нет") | timer | unmapped | H: not an expected concept for this category (by design, unlike cooktop/air_fryer) | **left unresolved** |
| Защита от столкновений, Голосовое управление, Пульт управления, Дисплей, Возвращение на станцию, Индикатор уровня заполнения, Обеззараживание воздуха, Регулятор мощности, Боковая щетка, Материал корпуса | retailer marketing yes/no bullets, none named in the Part 2 checklist | unmapped | G/I: genuinely out of scope | **left unresolved (no field invented)** |
| Поделиться, Оставить отзыв | share-button count / review widget rating — page-chrome noise, not specs | unmapped | I: extraction noise, not a mapping gap | **left unresolved (correctly — mapping to nothing is the correct behavior)** |
| category/price/priceCurrency/availability/itemCondition | JSON-LD commerce metadata | "mapped" pre-fix only via the discovered-shadow mechanism | I: not a product attribute at all | **unchanged, correctly outside the real schema** |

None of the 21 labels still unresolved after the fix were force-mapped;
each was checked against the Part 2 checklist and left alone when it
didn't correspond to one of the named, genuinely justified concepts.

## 2. Canonical field audit (Part 2)

Went through every checklist group for `wet_dry_vacuum`:

- **suction power / rated power**: both already existed; only `rated_power`
  was missing a bare Russian alias (`мощность`) — added, and verified it
  cannot collide with `suction_power`'s `мощность всасывания` (different
  normalized key, tested explicitly).
- **battery capacity**: existed, only Russian aliases missing.
- **battery type**: no field existed; genuinely distinct from
  `battery_capacity` (chemistry/removability vs. amount) — added
  `battery_type`.
- **runtime / charging time / tanks**: already covered by Ukrainian
  aliases from Stage 18.5/18.8 work; Dreame's page didn't surface these
  labels in this run, nothing to fix.
- **weight / dimensions**: `dimensions` existed; only needed a bare
  Russian alias (`размеры`) plus the bracket-legend generalization.
- **brush/roller parameters**: `Боковая щетка` is a yes/no presence flag,
  not a parameter (speed/diameter/etc.) — deliberately left out of scope,
  no field added.
- **self-cleaning / drying**: already covered, not present on this page.
- **noise**: no field existed — added `noise_level`.
- **filtration**: no field existed — added `hepa_filter` (boolean, same
  shape as the existing `self_cleaning` boolean).
- **operating modes / motor / voltage/frequency**: not present on this
  page; no evidence to act on, nothing changed.

No new field was created without a checklist justification, and no
existing concept was duplicated under a new name.

## 3. Mapping architecture changes

**`core/schema.py`** (`wet_dry_vacuum` category only — no other category
touched):
- `rated_power`: `+ "мощность"`.
- `battery_capacity`: `+ "емкость батареи", "емкость аккумулятора"`.
- `dimensions`: `+ "размеры"`.
- New `battery_type` (`text`, `variant_level`, priority `medium`,
  aliases: `battery type`, `battery chemistry`, `источник питания`,
  `тип аккумулятора`, `тип батареи`).
- New `noise_level` (`number`, `unit_family="sound_pressure"`, priority
  `medium`, aliases: `noise`, `noise level`, `sound level`, `уровень
  шума`, `шум`).
- New `hepa_filter` (`boolean`, priority `low`, `expected=False` — a
  marketing bullet, not a routinely-published spec like the other two —
  aliases: `hepa filter`, `air filter`, `воздушный фильтр hepa`, `фильтр
  hepa`, `воздушный фильтр`).

**`core/normalize.py`** (generic, applies to every category):
- The bracketed/trailing dimension-legend pattern
  (previously the single literal `ш×в×г`/`h×w×d`) is now a repeated
  character class `(?:[швгдт]xX){2}[швгдт]` / `(?:[hwdlt]xX){2}[hwdlt]`,
  so any 3-letter combination of the standard width/height/depth/
  length/thickness abbreviations in either language is recognized, not
  just the one exact order seen before.
- `normalize_attribute_label` now strips trailing unit/legend noise
  **iteratively** (up to 4 passes) instead of once, so a label that
  stacks a bracketed legend *and* a trailing bare unit
  (`"Размеры (ШxВxТ) мм"`) reduces fully instead of leaving the bracket
  behind after the bare-unit pass removes only the trailing token.

**`core/mapping.py`** (generic):
- `дб`/`db` added to `UNIT_ALIASES` → `dB`.
- `емкость` (bare Russian "capacity", the synonym of the already-handled
  `объем`) added to `AMBIGUOUS_LABELS` and to `_ambiguity_candidates`,
  scoped to the same `{battery_capacity, capacity, clean_water_tank,
  dirty_water_tank}` candidate set — so a bare, unqualified "Ёмкость"
  stays explicitly ambiguous rather than being silently dropped or
  guessed.

No product/model-specific string appears anywhere in the diff — every
alias is a normal industry term, and every new field is category-scoped
per the task's own scoping rule.

## 4. Tests (tests-first, RED → fix → GREEN)

All in `tests/test_mapping.py` / `tests/test_schema.py`:

- `test_russian_wet_dry_aliases_map_to_canonical_fields` — RED before the
  fix (0 matches), GREEN after: `Мощность Вт → rated_power`, `Ёмкость
  батареи → battery_capacity (mAh)`, `Уровень шума → noise_level (dB)`,
  `Источник питания → battery_type`, `Воздушный фильтр HEPA →
  hepa_filter`.
- `test_bracket_dimension_legend_generalizes_to_new_letter_orders` — RED
  (the `(ШxВxТ)` + trailing `мм` label stayed unmapped even after adding
  the `размеры` alias, because the old single-pass stripping left the
  bracket behind), GREEN after making suffix-stripping iterative.
- `test_bare_russian_power_is_not_confused_with_suction_power` —
  adversarial safety (Part 6): confirms `Мощность` → `rated_power` and
  `Мощность всасывания` → `suction_power` stay distinct in the same run.
- `test_bare_russian_capacity_synonym_remains_ambiguous_for_wet_dry_vacuum`
  — adversarial safety: bare `Ёмкость` stays ambiguous across
  `{battery_capacity, clean_water_tank, dirty_water_tank}`, never
  silently guessed.
- `test_wet_dry_vacuum_new_multilingual_fields_are_scoped_correctly` —
  the three new fields exist and `hepa_filter` is correctly `expected=False`.
- `test_regression_language_aliases_are_canonical_and_generic` — five new
  `resolve_attribute_definition` cases added alongside the pre-existing
  Ukrainian/Georgian ones (same test, same category).
- Existing `test_ukrainian_wet_dry_aliases_map_to_canonical_fields`,
  `test_korean_identity_labels_map_without_product_specific_rules`,
  `test_unrelated_ukrainian_cleaning_fact_remains_discovered`, and
  `test_aliases_do_not_resolve_to_conflicting_canonical_fields` (which
  iterates every category's full alias index for collisions) all stay
  green unmodified — Stage 18.3/18.5/18.8 Dreame/air_fryer/cooktop
  mapping is unaffected.

## 5. Dreame result: Stage 18.8 → Stage 18.9

Deterministic, same-evidence comparison (the exact 31 raw attributes
captured in the pre-fix live baseline trace, replayed through
`map_attributes` + `analyze_schema_coverage` before and after the fix —
this isolates the mapping change from live source-availability variance):

| | Pre-fix (Stage 18.8 end state) | Post-fix (Stage 18.9) |
|---|---|---|
| Raw attributes | 31 | 31 (unchanged — extraction untouched) |
| Real canonical fields present | 3 (`brand`, `manufacturer_article`, `color`) | **9** (`brand`, `manufacturer_article`, `color`, `rated_power`, `battery_capacity`, `dimensions`, `battery_type`, `noise_level`, `hepa_filter`) |
| Expected-field denominator | 18 | 20 (+`battery_type`, +`noise_level`; `hepa_filter` is `expected=False` so excluded) |
| Presence rate | 16.7% | **45.0%** |

Live cold runs (2/2, `force_refresh` semantics via `repository=None`,
`max_sources=5`, wall-clock budget 90s, outer timeout 120s):

- Run 1 (`14:24:37Z`–`14:26:03Z`, 85.9s): `category_confidence: low`
  — `detect_category` (untouched by this stage) scored below its
  threshold because 2 of 5 sources this run returned `blocked`
  (Yandex Market captcha) rather than the richer source mix the baseline
  trace had; `ru.dreametech.com` itself still fetched fine
  (`extracted_attribute_count: 26`, `http_status: 200`).
- Run 2 (`14:26:11Z`–`14:27:37Z`, 86.3s): identical outcome, same cause.

This is the same class of external-site/search-engine flakiness already
documented for Bosch/Gressel in Stage 18.7 — `detect_category` runs
before mapping and only consumes raw label/value/evidence text and
identity text, never the mapping/schema layer, so it cannot have been
affected by this stage's changes; confirmed by reading `core/category.py`
(no changes made there). Per the stage discipline, this was not "fixed"
by touching category thresholds. The deterministic offline comparison
above is the reliable evidence for this stage's actual claim (mapping
recovery); live variance in which sources happen to answer on a given
90-second budget is a separate, already-tracked bottleneck.

## 6. HONOR regression

Live run, `14:29:46Z`–`14:31:20Z` (93.1s): `status: conflicted`,
`coverage_percent: 47.8`, `category_confidence: high`, 4 genuine
cross-source conflicts (`display_size`, `battery_capacity`, `rear_camera`,
`front_camera` — regional/market listing variance, not multilingual
mapping). `smartphone`'s own `battery_capacity` definition was not
touched (only `wet_dry_vacuum`'s was). No regression.

## 7. Bosch regression

Live run: `status: insufficient`, `coverage_percent: 44.4`,
`confirmed_count: 0`, `category_confidence: high`. Fetch: 2/5 sources
succeeded, 2 blocked, 1 error. Mapping itself worked cleanly (51/53 raw
attributes resolved into some field, 2 correctly ambiguous, 0 crashed).
`confirmed_count: 0` reproduces the already-documented, still-open Stage
18.7 authority-corroboration gap (`discover_global_official_domains`
requiring the literal word "official"/"официаль"), unrelated to this
stage's scope. No new regression.

## 8. Janome regression

Live run: `status: insufficient`, `coverage_percent: 10.0`
(vs. Stage 18.6/18.8's typical ~70%), `category_confidence: high`. Fetch:
only 2/5 sources succeeded, 3 blocked this run. Extraction and mapping
both completed cleanly (69/69 raw attributes resolved, 0 unmapped, 0
ambiguous, no exceptions) — the lower coverage number is fully explained
by fewer rich sources answering within the 90s budget this run, not by
`sewing_machine`'s schema (entirely untouched by this stage). No
deterministic regression.

## 9. Gressel control

Live run: `status: insufficient`, `coverage_percent: 0.0`. Fetch: 0/5
sources returned usable content (2 `not_found`, 3 `blocked`) — 0 raw
attributes extracted, so mapping never ran. Pure external site
unavailability for this run; `air_fryer`'s schema is untouched by this
stage.

## 10. Remaining bottlenecks (unchanged by this stage, not addressed here)

- **Mapping/schema**: `Тип пылесборника`, `Объём пылесборника`
  (composite), `Уборка`, and the ~10 marketing yes/no bullets on Dreame's
  page remain genuinely unmapped by design (Part 6 discipline: no
  confident single-field mapping exists). If a future stage wants them,
  each needs its own semantic justification, not blanket coverage.
- **Authority**: Bosch's 0 confirmed facts despite genuine manufacturer
  fetches — the "official"/"официаль" literal-word gap flagged since
  Stage 18.7, still open.
- **Access/external evidence**: this run's source-blocking variance
  (Dreame category detection, Janome's dropped coverage, Gressel's total
  block) is the same captcha/availability class documented since Stage
  18.4/18.7 — not new, not fixed here, not in scope.
- **Genuine conflicts**: HONOR's 4 cross-source conflicts are real market
  /regional data disagreement, correctly surfaced, not a mapping defect.

## 11. Tests / Git

- Full suite: `python -m unittest discover -s tests` → **630/630 PASS**
  (625 baseline + 5 new test methods; several existing parametrized tests
  also gained new `subTest` cases).
- `git diff --check`: clean (only pre-existing LF/CRLF line-ending
  warnings, no whitespace errors).
- Compile/import check: `core/schema.py`, `core/mapping.py`,
  `core/normalize.py`, and both changed test modules compile cleanly.
- Commit created on `main`; pushed to `origin/main` (Stage 18.9 PASS
  criteria met: proven multilingual gap, generic tests-first fixes,
  ambiguity-safe, no hardcoding, no regression on any of the four control
  products, full suite green).

## 12. Recommendation (not started)

Two independent follow-ups worth a future stage, kept separate since
they're different problem classes:

1. A schema/mapping stage for the remaining Dreame composite/marketing
   labels (`Объём пылесборника` split, whether `Тип пылесборника` and the
   yes/no bullets are worth dedicated fields) — should stay small and
   evidence-driven like this one, not a blanket "map everything" pass.
2. The still-open Stage 18.7 authority-corroboration literal-word gap
   (Bosch/HONOR-style manufacturer pages that never say "official") is a
   bigger, higher-value target since it blocks `confirmed_count` on
   otherwise-correct manufacturer fetches across multiple categories, not
   just one product's spec labels.
