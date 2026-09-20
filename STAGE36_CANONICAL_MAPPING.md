# Stage 36 — Canonical mapping by category

**Verdict: PASS** (acceptance gate below). Only a new offline layer was added; discovery, extraction and normalisation are untouched.
Not done on purpose: conflict winner selection, secondary enrichment, completeness scoring, Telegram output, large benchmark.

## What was built

| File | Role |
| --- | --- |
| `core/canonical_mapping.py` | pure: concept table (canonical schema), unit families + exact conversion, value analysis, mapping engine, not-a-spec, collisions |
| `services/canonical_mapping.py` | `CanonicalMappingService.map(NormalizationResult)` |
| `core/category.py` | +6 **generic** categories: `oven`, `power_tool`, `oral_care`, `coffee_machine`, `computer_peripheral`, `skincare` (existing: cooktop, air_fryer, …) |
| `diagnostics/stage36_canonical_mapping.py` | offline benchmark over the 8 saved Stage 35 results (`network_forbidden()`), traces in `diagnostics/results/stage36/` |
| `tests/test_stage36_canonical_mapping.py` | 58 tests (units, multilingual, context, category, ambiguity, not-a-spec, collisions, 8 saved products) |

Input: `diagnostics/results/stage35/*-normalized.json`. `network_calls = 0`; no discovery/extraction imports (tested).

## Output record (`MappedField`)

`canonical_key`, `canonical_label`, `normalized_value`, `unit`, `value_type`, `value_min/max`, `category`, `confidence`, `mapping_method`,
`unit_status` (`explicit | converted | implied | none`), `conversion` (original value/number/unit, method, alternates, rounded),
`evidence[]` (mapping signals: label, group_path, category, unit, notes, … **plus one `source_record` per raw record, complete**),
`candidates[]` + `reasons` (ambiguous), `context`, `derived_from`, and the whole original `NormalizedAttribute`.
`status` ∈ `mapped | ambiguous | unmapped | not_a_spec`. One normalized attribute can yield several rows (dimension split, `50/60 Hz` companion).

## How mapping works (generic signals only)

* **Concept table** = the canonical schema. Universal concepts (power, voltage, weight, dimensions, cord length, warranty, …) apply everywhere; category-specific ones
  (`drilling_capacity_*`, `pump_pressure_bar`, `dpi_nominal`, `niche_*`, `is_vegan`, …) apply only to their categories. Unknown category → universal concepts only.
* **Labels**: accent/case-folded regexes with de / fr / it / cs / en / ru aliases. Primary label first; aliases (other locales merged by Stage 35) only *vote*:
  agree → +0.02 each, point to another concept → −0.10 and a note (Bosch `Nischenbreite minimal` carries a French alias for the *height* → 0.88, still mapped, flagged).
* **Group path**: `need_group` concepts (`Steel` only inside *Drilling capacity*, `Hi/Lo` inside *No load speed* vs *Impacts per minute*, `Nominal value` inside *sensor/tracking*).
  Path segments longer than 6 words (page titles) are not context.
* **Unit family** is a signal: `No Load Speed` + `rpm` → `no_load_speed_rpm`, + `bpm` → `impact_rate_ipm`; wrong family → rule rejected, never forced (`Power supply … V` ≠ `power_w`).
* **Value shape**: boolean (also `ja (verfügbar…)`, `Oui`, `Sì`), count (`7 buttons (…)`), measure, dimensions, text; value predicates (power tool `Voltage` ≤ 60 V → `battery_voltage_v`, ≥ 100 V → `voltage_v`, in between → ambiguous).
* **Repeated block** in one section: first occurrence keeps `height_mm`, later ones become `secondary_component_height_mm` (Logitech mouse vs USB receiver: the section text is identical, only order differs).
* **Dimensions**: axis order is split only when the label declares it (`(H x B x T)`); otherwise the composite `dimensions_mm` is kept.
* **Compound values**: `220-240 В, 50/60 Гц` → `voltage_v` + `frequency_hz`; `Rechargeable Li-Po (500 mAh)` → `battery_type` + `battery_capacity_ah = 0.5`; `4 литра`, `14 Tage`, `2 year limited warranty`, `62.000 Bürstenkopfbewegungen/Min`.

**Confidence**: strong label 0.90, category-resolved generic label 0.72, weak label 0.60, group-only 0.78; +0.05 group, +0.03 category, +0.03 explicit unit, ±alias votes,
−0.05…0.08 for text-embedded values. `mapped` needs ≥ 0.70 **and** a 0.10 lead over the next key. Value that cannot be read (prose, single number for several axes, ambiguous unit) is capped at 0.50 → `ambiguous`.
Unit not written → only if exactly one unit of the family is plausible for the concept (weight 37.3 → kg; 141 → ambiguous kg|g; warranty `3` → ambiguous month|year); capped at 0.75, `unit_status = implied`.

**Unit conversion** is exact (`Fraction`): in→mm 25.4, oz→kg, cm→m, days→h, °F→°C; rounding beyond 6 decimals is flagged (`rounded`). A metric source alternate is preferred and kept:
`3.32 in (84.3 mm)` → `width_mm = 84.3` (`source_alternate`, original + alternates in `conversion`); the tolerance follows the primary's rounding (`0.06 oz (1.68 g)` → `0.00168 kg`).

**Not a spec** (only for attributes that stayed unmapped): identity (barcode, origin, product type), variant colours, commercial (`FSA Eligible`), documentation, legal/certification (SRN, `En55011`, directive prose), company/address, PDF fragments, promotional chapter blurbs (`Up to 15 x healthier gums***`). A real spec inside a promotional chapter (`Power supply`) is mapped first.

**Collisions** (never resolved, all candidates stay `mapped`): `same_value`, `equivalent_after_conversion`, `different_values`; list-type keys (`included_accessories`) are `list_field`, not conflicts.

## Benchmark (8/8, offline)

| Product | Category | Normalized specs | Mapped | Ambiguous | Unmapped | Not a spec | Unique canonical fields | Coverage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Gressel GAF-1825 | air_fryer (medium) | 32 | 17 | 6 | 2 | 7 | 17 | 68% (17/25) |
| Bosch HBG7741B1 | oven (high) | 107 | 92 | 1 | 5 | 9 | 46 | 94% (92/98) |
| DEWALT DCD796P2 | power_tool (high) | 18 | 17 | 0 | 0 | 1 | 17 | 100% (17/17) |
| Philips Sonicare 9900 Prestige HX9992/12 | oral_care (high) | 76 | 30 | 0 | 5 | 41 | 12 | 86% (30/35) |
| Makita DHP484Z | power_tool (high) | 15 | 14 | 1 | 0 | 0 | 14 | 93% (14/15) |
| DeLonghi EC685M | coffee_machine (high) | 31 | 30 | 0 | 1 | 0 | 30 | 97% (30/31) |
| Logitech MX Master 3S | computer_peripheral (high) | 23 | 20 | 3 | 0 | 0 | 21 | 87% (20/23) |
| The Ordinary Niacinamide 10% + Zinc 1% | skincare (high) | 9 | 8 | 0 | 0 | 1 | 8 | 100% (8/8) |

Coverage = mapped product specs / (product specs − not_a_spec). Counts are per normalized attribute (a split row does not add to *Mapped*). *Unique canonical fields* counts keys incl. split/companion rows.
Coverage is high partly because the concept table was written after looking at these 8 products; it is a mapping-quality check on known data, not a generalisation estimate (that is what the later larger benchmark is for).

## normalized → canonical (40 of 60; full list in `diagnostics/results/stage36/examples.json`)

| Product | Normalized label = value | → canonical_key = value | Method / conf |
| --- | --- | --- | --- |
| Makita | `Drilling capacity > Steel` = 13 mm | `drilling_capacity_steel_mm` = 13 mm | group_context 0.99 |
| Makita | `Impacts per minute > Hi` = 0–30000 ipm | `impact_rate_high_ipm` = 0–30000 ipm | group_context 0.99 |
| Makita | `No load speed > Hi` = 0–2000 rpm | `no_load_speed_high_rpm` = 0–2000 rpm | group_context 0.99 |
| Makita | `Max fastening torque` = 60 Nm | `max_torque_nm` = 60 Nm | 0.96 |
| Makita | `Voltage` = 18 V | `battery_voltage_v` = 18 V | value_test 0.96 |
| Makita | `Continuous rating input` = 450 W | `power_w` = 450 W | 0.93 |
| Makita | `Sound Level` = 99 dB(A) | `noise_level_dba` = 99 dB(A) | 0.93 |
| DEWALT | `Maximum Speed` = 2000 rpm | `max_speed_rpm` = 2000 rpm | 0.96 |
| DEWALT | `No Load Speed` = 34000 bpm | `impact_rate_ipm` = 34000 ipm | unit family 0.96 |
| DEWALT | `Assembled Product Height` = 20.29 cm | `height_mm` = 202.9 mm | converted 0.93 |
| DEWALT | `Battery Amp Hours` = 5.0 Ah | `battery_capacity_ah` = 5 Ah | 0.93 |
| Logitech | `Width` = 3.32 in (84.3 mm) | `width_mm` = 84.3 mm | source_alternate 0.93 |
| Logitech | `Weight` = 4.97 oz (141 g) | `weight_kg` = 0.141 kg | source_alternate 0.93 |
| Logitech | `Width` = 0.57 in (14.4 mm) (2nd block) | `secondary_component_width_mm` = 14.4 mm | repeated_block 0.83 |
| Logitech | `Nominal value` = 1000 DPI [sensor/tracking] | `dpi_nominal` = 1000 DPI | group_context 0.99 |
| Logitech | `Wireless range` = 32.8 ft (10 m)*… | `wireless_range_m` = 10 m | embedded 0.85 |
| Logitech | `Battery type` = Rechargeable Li-Po (500 mAh) battery | `battery_capacity_ah` = 0.5 Ah (+ `battery_type`) | compound 0.80 |
| Bosch (de) | `Nettogewicht` = 37.3 kg | `weight_kg` = 37.3 kg | 0.97 |
| Bosch (state, EN) | `Weight net` = 37.3 | `weight_kg` = 37.3 kg | implied unit 0.75 |
| Bosch (de) | `Abmessungen des Gerätes (H x B x T)` = 595 × 594 × 548 mm | `height_mm` 595 / `width_mm` 594 / `depth_mm` 548 | dimension_split 0.95 |
| Bosch (de) | `Länge Netzkabel` = 120 cm | `cord_length_m` = 1.2 m | converted 0.97 |
| Bosch (de) | `Nischenhöhe minimal` = 585 mm | `niche_height_min_mm` = 585 mm | 0.99 |
| Bosch (fr) | `Système de nettoyage intégré` = Pyrolisis | `cleaning_system` | 0.95 |
| Bosch (it) | `Sistema di pulizia` = Pirolisi+Idrolisi | `cleaning_system` | 0.95 |
| Bosch (it) | `Preriscaldamento rapido` = true | `fast_preheat` = true | 0.97 |
| Bosch (de) | `Kochassistent in der Home Connect App` = ja (verfügbar…) | `cooking_assistant_app` = true | prefix bool 0.90 |
| Bosch | `Verfügbarer Temperaturbereich` = 30–300 °C | `temperature_range_c` = 30–300 °C | 0.99 |
| DeLonghi (cs) | `Příkon` = 1450 W | `power_w` = 1450 W | 0.93 |
| DeLonghi (cs) | `Jmenovitá frekvence` = 50–60 Hz | `frequency_hz` = 50–60 Hz | 0.93 |
| DeLonghi (cs) | `Kapacita nádržky na vodu` = 1.1 | `water_tank_capacity_l` = 1.1 L | implied unit 0.75 |
| DeLonghi (cs) | `Rozměry výrobku` = 149 × 330 × 305 | `dimensions_mm` = 149 × 330 × 305 mm (axis order unknown, not split) | implied 0.75 |
| DeLonghi (cs) | `Materiál` [colourmate] = Metal | `body_material` = Metal | group_context 0.95 |
| DeLonghi (cs) | `Latte Macchiato` = false | `beverage_latte_macchiato` = false | 0.93 |
| Gressel (ru) | `Мощность` = 2700 Вт | `power_w` = 2700 W | 0.93 |
| Gressel (ru) | `Длина сетевого шнура` = 70 см | `cord_length_m` = 0.7 m | converted 0.93 |
| Gressel (ru) | `Напряжение` = 220-240 В, 50/60 Гц | `voltage_v` 220–240 V + `frequency_hz` 50–60 Hz | compound 0.85 / 0.75 |
| Gressel (ru) | `Срок гарантии` = 12 месяцев | `warranty_months` = 12 month | 0.93 |
| Gressel (ru) | `Терморегулятор` = 80–200 | `temperature_range_c` = 80–200 °C | implied unit 0.75 |
| Philips (en / de) | `Power supply` = 100–240 V / `Stromversorgung` = 100 bis 240 V | `voltage_v` = 100–240 V | 0.93 / 0.85 |
| Philips (en / de) | `Operating time` = 14 days / `Betriebsdauer` = 14 Tage | `battery_runtime_h` = 336 h | converted 0.93 |
| Philips (en / de) | `Warranty` = 2 year limited warranty / `Garantie` = 2 Jahre… | `warranty_months` = 24 month | converted 0.93 |
| Philips (en / de) | `Speed` = 62,000 brush movements/min / `Geschwindigkeit` = 62.000 … | `brush_movements_per_min` = 62000 | value_pattern 0.88 |
| Ordinary | `Highlights > ph` = 5.00–6.50 | `ph_range` = 5–6.5 | 0.93 |
| Ordinary | `alcohol-free` = true | `is_alcohol_free` = true | 0.93 |

Same label, different key by category/context: `Voltage 18 V` → `battery_voltage_v` (power tool) but `voltage_v` (oven, or ≥ 100 V tool); `Timer` → `timer_available` (air fryer) vs `brushing_timer` (oral care); `Capacity` → `usable_volume_l` (oven) vs `bowl_capacity_l` (air fryer) vs unmapped (unknown).

## Ambiguous (kept as candidates, never guessed)

| Product | Label = value | Candidates | Why |
| --- | --- | --- | --- |
| Gressel | `Размер` = 42.5х40.5х33 | dimensions_mm 0.50 | unit missing: plausible as mm and cm |
| Gressel | `Гарантия` = 3 | warranty_months 0.50 | unit missing: month or year |
| Gressel | `Объем чаши` = 4 + 4 (отдельно) | bowl_capacity_l 0.50 | value is not one measure |
| Gressel | `Напряжение питания` = от сети | power_source 0.60, voltage_v 0.50 | weak label; text is not a voltage |
| Gressel | `Материал` = металлическая решетка… | body_material 0.60 | generic label, no context |
| Gressel | `Дисплей` = 2 чаши для одновременного… | display_type 0.50 | value is running prose |
| Bosch (state) | `Connection` = 3600 | power_w 0.60 | weak label |
| Makita | `Overall dimensions` = 182 mm | dimensions_mm 0.50 | single value for a multi-axis label |
| Logitech | `Battery life` = Get three hours of use… | battery_runtime_h 0.50 | duration written in words |
| Logitech | `Compatibility`, `Logi Bolt USB receiver` | compatibility / usb_receiver 0.50 | long prose value |

## Unmapped (input for the next stage)

Bosch: `coolStart` (×2), `Home Connect Features` (fr/en), `Home Connect - … à distance`; Philips: `Energy consumption` (`Standby … <0.06 W`, inequality), `Software support`, `Handstück-Kompatibilität`;
DeLonghi: `Espresso cool`; Gressel: `Чаша` = true, `Тип` = true (label too generic). Not-a-spec: 9 Bosch (colours ×8 + manuals), 41 Philips (promo chapter + SRN/standards), 7 Gressel (barcode, origin, address/legal fragments), `Product Type`, `FSA Eligible`.

## Collisions (kept, not resolved)

Totals: 22 `same_value`, 3 `equivalent_after_conversion`, 19 `different_values`, 2 `list_field`.
* `same_value` — Bosch (JSON-LD de + JSON state EN of the same value): `weight_kg`, `voltage_v`, `dimensions_mm`, `niche_*`, `temperature_range_c`, `usable_volume_l`, `fast_preheat`…; Gressel `bowl_capacity_l` (4 L + 4 L, two bowls).
* `equivalent_after_conversion` — Philips `battery_runtime_h` (14 days = 14 Tage = 336 h), `warranty_months` (24 = 24); Bosch `cord_length_m` (120 cm vs implied).
* `different_values` — almost all are **text in different languages** (Bosch `cooking_methods`, `cleaning_system`, `plug_type` de/fr/it; Philips `battery_type` Lithium ION / Lithium-Ionen) plus Gressel `body_material` (two different components). No numeric conflict exists in this set.
* `list_field` — `included_accessories` (Bosch de/fr/it lists, Philips box contents).

## Known limits (for later stages)

* Text collisions need language-aware comparison; Stage 36 only reports them.
* The primary/secondary decision for repeated blocks assumes the first block is the main product (true for Logitech; it is a positional signal, flagged by `repeated_block_context`).
* Concept table is hand-written and multilingual only for the six required languages; new families/languages = new patterns, not new rules per model.
* Long prose values under a valid label stay ambiguous (`Compatibility`, `USB receiver`) — a later stage decides whether to accept long text fields.
* Bosch alias alignment from Stage 35 can attach a wrong-axis alias (flagged, confidence lowered, not fixed here).

## Acceptance gate

| Criterion | Result |
| --- | --- |
| 8/8 processed offline | yes (`network_forbidden()`, `network_calls = 0`, no discovery/extraction imports — tested) |
| category-aware mapping | yes (6 new generic categories; same label → different key by category; category-specific concepts do not leak into other categories) |
| multilingual labels collapse | yes (de/fr/it/cs/en/ru → one key: weight, power, voltage, frequency, volume, cleaning, warranty, runtime …; tested) |
| group/path used | yes (`drilling_capacity_*`, `no_load_speed_*` vs `impact_rate_*`, `dpi_nominal`, `body_material`, repeated block) |
| safe, tested unit conversion | yes (exact fractions, alternates preserved, implied unit only if unique and ≤ 0.75) |
| ambiguous not guessed | yes (11 ambiguous attributes with candidates + reasons; low confidence never `mapped`) |
| marketing / metadata not mapped | yes (`not_a_spec`; tested on the saved products) |
| collisions preserved | yes (all candidates stay `mapped`, 46 collision groups, no winner) |
| provenance kept | yes (every row embeds all source records; tested) |
| no brand / product hardcode | yes (source scan test) |
| tests | 1115 OK (58 new) |
