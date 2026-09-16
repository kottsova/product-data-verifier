# Stage 18.3 — Deterministic Downstream Recovery

## Result

**PASS** for the Stage 18.3 deterministic recovery gate.

This is not a claim that either product is fully verified. HONOR remains
`insufficient` and Dreame is `partial` because the live sources retain unknown
authority and therefore produce no Confirmed facts. The pass means that the
proven extraction/category/schema/mapping losses were removed without weakening
authority, validation, quality thresholds, or the expected schema.

Janome/Gressel discovery, provider ordering, provider deadlines, the 90-second
global budget, circuit breaking, and targeted-search limits were not changed.

## Exact diagnosis

### HONOR X8d

The retained Stage 18.2 trace contained 82 raw facts from four successful
fetches, but category detection returned `unknown`. The concrete loss chain was:

1. Candidate titles exposed the product taxonomy `Phones`, while the category
   detector only recognized stronger phrases such as `smartphone`.
2. Product feature cards contained multiple mutually reinforcing smartphone
   signals (`Peak Brightness`, `Refresh Rate`, and `Battery`), but those signals
   were not combined with the title taxonomy.
3. Feature-card layout inverted measurements and labels. Examples included
   `3000nits -> Peak Brightness`, `120Hz -> Refresh Rate 15`, and
   `Ultra-slim Design & -> 7000mAh Battery`.
4. Weak line reconstruction could create the incorrect facts
   `Peak Brightness=120Hz` and `Refresh Rate=15`.
5. With category `unknown`, the pipeline selected only the universal expected
   schema; smartphone semantics survived as discovered attributes but did not
   populate smartphone expected fields.
6. In one final pre-fix cold run, 4/5 initial fetches succeeded but all fetches
   were completed before extraction began. They consumed the remaining global
   budget, producing `raw=0` from already downloaded pages.

The generic fixes are:

- combine weak `phone/phones` taxonomy evidence with independent display and
  battery attribute signals, while requiring a two-point lead over a conflicting
  category;
- recover only explicitly labelled feature measurements for refresh rate, peak
  brightness, and battery capacity;
- suppress weak reconstructed values when a stronger structured fact with the
  same label exists;
- extract each successful initial source before starting later fetches, so a
  later slow fetch cannot erase already available downstream evidence.

Live official-page examples after the fix:

| Source text | Normalized raw fact | Canonical key | Final state |
|---|---|---|---|
| `120Hz` + `Refresh Rate 15` | `Refresh Rate = 120 Hz` | `refresh_rate` | Unresolved, `insufficient_source_quality` |
| `7000mAh Battery` | `Battery Capacity = 7000 mAh` | `battery_capacity` | Unresolved, `insufficient_source_quality` |
| `3000nits` + `Peak Brightness` | `Peak Brightness = 3000 nits` | discovered (not expected) | Preserved |

Confidence in this root cause is high: the pre-fix tests failed, the same
official HTML produces the corrected values after the fix, and the final cold
workflow recovered the smartphone category and expected-schema population.

### Dreame G12 Pro / HHR32A

The retained Stage 18.2 trace contained 107 mapped facts, but most Ukrainian
and Korean labels were preserved as distinct discovered keys. Only 1/18
expected fields had usable evidence. The concrete mapping lineage was:

| Raw label | Raw value | Normalized canonical key | Normalized unit/state |
|---|---|---|---|
| `Сила всмоктування` | `23000 Па` | `suction_power` | `23000 Pa` |
| `Потужність споживання` | `300 Вт` | `rated_power` | `300 W` |
| `Ємність аккумулятору` | `2500 мА·год` | `battery_capacity` | `2500 mAh`; rejected as `insufficient_identity` for a same-base source |
| `Час роботи на одному заряді` | `35 хв` | `runtime` | `35 min` |
| `Час повної зарядки` | `3 год` | `charging_time` | `3 h` |
| `Об'єм резервуару для чистої води` | `800 мл` | `clean_water_tank` | `800 ml` |
| `Об'єм резервуару для відпрацьованої води` | `700 мл` | `dirty_water_tank` | `700 ml` |
| `Режими прибирання` | mode list | `modes` | preserved list |
| `Бренд` / `브랜드` | `Dreame` | `brand` | Unresolved, unknown authority |
| `모델` | `G12 Pro` | `model` | Unresolved, unknown authority |

The value `Сила всмоктування = "-"` exposed a separate validation bug: a
placeholder was considered concrete and could outrank a real value if attached
to a stronger source. Placeholders are now rejected before grouping and are not
retained as supporting evidence.

No identity or authority rule was relaxed. The HHR32A battery fact remains
ineligible when its source relation is only `same_base_model`, and all facts
from unknown-authority sources remain Unresolved. The noisy `g10-pro-flex` URL
is still a discovery-quality concern; its title/body identify `G12 PRO Flex`, so
the current pipeline retains only same-base, provisional evidence from it.

Confidence in the mapping root cause is high: regression tests reproduce each
raw label, the retained Stage 18.2 facts replay from 1/18 to 10 mapped expected
keys, and the final cold workflow has 9/18 expected fields with usable evidence.

## Regression tests and implementation

The tests were added before each implementation change and observed failing:

| Failure | Regression test | Fix |
|---|---|---|
| HONOR category remained unknown | phone taxonomy + independent display signature | weighted generic category signals and conflict margin |
| Close smartphone/vacuum evidence selected a low-margin winner | conflicting category signature | return `unknown` when the winner lead is below two points |
| Ukrainian wet/dry evidence was unknown | Ukrainian product type and attribute signature | generic multilingual category terms |
| Ukrainian/Korean keys stayed discovered | alias tables for wet/dry and identity terms | existing schema alias architecture extended |
| Cyrillic/Ukrainian units were not split/canonicalized | Pa, mAh, minutes, hours, ml, W cases | extraction and mapping unit aliases |
| HONOR feature values were inverted or footnotes became values | three feature-card cases | explicit measurement+label recovery and weak-duplicate suppression |
| Placeholder `-` could become authoritative | official placeholder vs concrete weak value | concrete-value rejection and eligible-only supporting facts |
| Fetches could consume all time before extraction | deterministic fake clock | interleaved initial fetch/extract |
| Alias additions could conflict | all category alias indexes | deterministic uniqueness assertion |
| Unknown facts could be over-mapped | unrelated Ukrainian drying fact | remains discovered/unmapped |

## Category and mapping behavior

Category confidence remains conservative:

- a single weak `phone/phones` taxonomy term is below the acceptance threshold;
- independent semantic evidence is required;
- close conflicting signatures return `unknown`;
- explicit Ukrainian wet/dry product text remains a strong signal.

Mapping uses only category-level aliases. No brand, model, article, benchmark,
or source URL is present in a mapping rule. Unknown raw fields continue to be
added to the extended schema as non-expected discovered attributes.

## Validation findings

- Unknown-authority manufacturer-looking and generic sources remain weak.
- Retailer, marketplace, distributor, and generic evidence cannot become
  Confirmed merely through repetition.
- Variant-sensitive fields still require `exact_variant`.
- Different-model, missing-provenance, placeholder, ambiguous, and blocked
  evidence remains excluded.
- No quality or authority threshold changed.

The final HONOR and Dreame runs both have 0 Confirmed facts for these reasons.
That is correct validation behavior, not a remaining downstream conversion bug.

## Live comparison

All Stage 18.3 runs were cold (`force_refresh=True`), used `max_sources=5`, and
ran without repository/cache reuse.

| Product / stage | Category | Expected population | Confirmed / unresolved / conflicts | Coverage | Quality | Runtime |
|---|---|---:|---:|---:|---|---:|
| HONOR Stage 9 | smartphone | 14/23 by retained 60.9% baseline | not retained | 60.9% | historical baseline | not retained |
| HONOR Stage 18.2 | unknown/low | 0/6 | 0 / 68 total / 0 | 0.0% | insufficient | 90.588 s |
| HONOR Stage 18.3 | smartphone/high | 2/23 | 0 / 40 total (23 expected) / 0 | 8.7% | insufficient | 90.465 s |
| Dreame Stage 9 | wet_dry_vacuum | 12/18 by retained 66.7% baseline | not retained | 66.7% | historical baseline | not retained |
| Dreame Stage 18.2 | wet_dry_vacuum/high | 1/18 | 0 / 98 total / 0 | 5.6% | insufficient | 73.970 s |
| Dreame Stage 18.3 | wet_dry_vacuum/high | 9/18 usable (10 mapped) | 0 / 88 total (18 expected) / 0 | 50.0% | partial | 74.842 s |

Final HONOR trace: 27 accepted, 5 selected/fetched, 4 fetch successes,
50 raw facts, 50 mapped facts, one targeted query, and no outer timeout.

Final Dreame trace: 11 accepted, 5 selected/fetched, 4 fetch successes and one
block, 106 raw facts, 106 mapped facts, 10 targeted queries, and no outer timeout.

## Files changed

- `core/category.py`
- `core/extract.py`
- `core/mapping.py`
- `core/schema.py`
- `core/validation.py`
- `core/workflow.py`
- `tests/test_category.py`
- `tests/test_extract.py`
- `tests/test_mapping.py`
- `tests/test_schema.py`
- `tests/test_validation.py`
- `tests/test_workflow.py`
- `STAGE18_3_DETERMINISTIC_DOWNSTREAM_RECOVERY.md`

## Remaining issues

### Deterministic fixed

- HONOR category recovery from trustworthy page context and repeated semantic
  evidence;
- HONOR feature-card measurement extraction and weak footnote suppression;
- Ukrainian/Korean canonical mapping and unit normalization;
- placeholder values entering validation groups;
- initial fetches starving extraction at the global-budget boundary.

### Deterministic still open

- HONOR evidence does not yet cover most of the 23-field smartphone schema.
- Candidate ranking can still spend the five-source limit on weak or noisy
  same-base pages.
- The live diagnostic trace does not serialize every supporting raw value in
  its validation summary; lineage was reconstructed from its raw and mapping
  sections plus direct official-page verification.

### External/provider-related

- Google timed out and DuckDuckGo Lite was blocked in both final runs; Naver was
  the only successful discovery provider.
- HONOR official pages and global Dreame pages can return empty extraction in
  some runs even when HTTP fetch reports success.
- Authority discovery did not prove the HONOR/Dreame domains, so their facts
  correctly remain unknown-authority.

## Recommendation

The next stage should be a separate discovery recovery stage for
Bosch/Janome/Gressel, with candidate-quality work for HONOR/Dreame kept explicit.
It should not be started as part of Stage 18.3.
