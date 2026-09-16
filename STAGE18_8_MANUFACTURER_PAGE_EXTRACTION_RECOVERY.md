# Stage 18.8 — Manufacturer Page Extraction Recovery

Date: 2026-09-16

Verdict: **PASS**.

Diagnosed and fixed the exact cause of Dreame's `raw=0` from authority-
verified manufacturer pages: not a missing extraction capability, but a
fetch-stage false positive that discarded already-complete static HTML in
favor of a Playwright render that then timed out. A second, related
extraction-safety gap (review-widget names misread as spec labels) was
found and fixed while tracing the same real page. Both fixes are generic
(word-boundary/utility-CSS-token and review-triple pattern recognition),
not Dreame-specific, and are covered by fixtures built from a sanitized
version of the real page. Live cold runs confirm Dreame now yields
confirmed facts for the first time in the Stage 18.x series (2/2 runs),
with Bosch, HONOR, Janome, and Gressel showing no regression.

## 1. Dreame extraction diagnosis

**Source**: `https://ru.dreametech.com/product/moyushchiy-besprovodnoy-pylesos-dreame-g12pro`
(the real, localized RU product page — distinct from the two
`global.dreametech.com/pages/...` URLs, which are genuine manual/PDF
download landing pages with no specification content at all — confirmed by
direct inspection: page body is only a country/region selector plus a
"User Manual" heading and download links; correctly `raw=0`, not a bug).

**Where the specs physically live**: in plain, static HTML (no rendering
required) — 24 real specification rows in a Tailwind-CSS "grid" of
`<div>` rows, each with exactly two direct `<div>` children (label
paragraph, value paragraph), e.g. `Мощность Вт → 315`, `Ёмкость батареи →
2500 мАч`, `Уровень шума → 76 дБ`. This matches the task's case **A** —
"specs есть в static HTML, extractor их пропускает" — but the actual loss
point was one stage earlier than extraction itself.

**Why the old pipeline returned `raw=0`** (confirmed by direct, reproducible
testing against the live-captured HTML, not speculation):

1. `core/fetch.py::_html_content_complete` decides whether a page's static
   HTML is "complete enough" to skip a Playwright render. It scans tag
   `id`/`class` strings with loose, unbounded regexes:
   - `spec_marker = re.compile(r"spec|attribute|...")` matched the
     substring `"spec"` **inside** the unrelated Tailwind class
     `"aspect-square"` (an image-gallery thumbnail class — "a**spec**t"),
     wrongly flagging thumbnail elements as specification containers.
   - `skeleton_marker = re.compile(r"skeleton|placeholder|loading|shimmer")`
     matched the whole word `"placeholder"` inside Tailwind **variant**
     classes on an unrelated newsletter-signup `<input>`/`<label>` —
     `"peer-disabled:placeholder:text-inactive"` and
     `"peer-placeholder-shown:translate-y-0"`. These are pure CSS
     state-styling tokens (`::placeholder` pseudo-element, "when this
     peer's placeholder is shown") with no relation to a loading skeleton.
   - Two such matches reached the heuristic's `skeletons >= 2` rule, so
     `_html_content_complete` returned `False` even though the page's real
     specification grid was fully present and non-empty in the static
     response.
2. This triggered an unnecessary escalation to `_fetch_with_playwright`,
   which then hit `Page.goto: Timeout 30000ms exceeded` on this specific
   site (observed live in the Stage 18.7 trace), so the entire fetch was
   marked `status="error"` — **discarding the already-successful,
   already-complete static HTML** that extraction never got a chance to
   run on.
3. Directly testing `core.extract.extract_attributes` against the real
   captured HTML (bypassing the fetch-stage gate) confirmed the existing
   extractor's generic `_extract_repeated_blocks`/`label_value` mechanism
   **already** handles this exact two-child-row DOM shape correctly with no
   changes needed — it pulled all 24 real spec rows on the first try.

**Confidence: high.** Reproduced deterministically against the real,
captured page HTML both before and after the fix; RED/GREEN tests use a
sanitized fixture built from the same real structure.

## 2. Files changed

- `core/fetch.py` — `_tag_signals` excludes colon-containing (Tailwind-
  variant) class tokens; `spec_marker`/`skeleton_marker` gained word
  boundaries.
- `core/extract.py` — `_pairs_from_lines`'s plain-text fallback pairing no
  longer treats a bare 1-5 rating immediately followed by a date line as a
  spec value (review-widget name/rating/date triple).
- `tests/test_fetch.py` — 1 new RED→GREEN test (sanitized real-page
  fixture).
- `tests/test_extract.py` — 1 new RED→GREEN test (sanitized review-widget
  fixture).

No changes to `core/discovery.py`, `core/authority.py`, `core/validation.py`,
`core/quality.py`, `core/schema.py`, or any threshold.

## 3. Extraction architecture changes

- **Static DOM**: unchanged — already correct once it receives the real
  HTML; the existing `_extract_repeated_blocks`/`_extract_label_values`
  generic two-column/row pairing mechanism was sufficient.
- **Fetch-stage completeness gate**: fixed to stop false-triggering on
  common utility-CSS (Tailwind) naming collisions. This is a framework-
  level fix (word boundaries; excluding colon-containing variant tokens
  from heuristic keyword matching), not Dreame-specific — any site using
  Tailwind-style utility classes with an "aspect-*" or "*placeholder*"
  variant token near real spec content would have hit the same false
  escalation.
- **Extraction safety (review metadata)**: the fallback plain-text pairing
  heuristic now recognizes a review-entry's structural signature (name
  line → bare 1-5 rating → date line) generically, since the real widget
  had no semantic class/id to filter on (pure Tailwind utility markup) —
  content-pattern recognition was the only viable generic signal.
- Structured data (JSON-LD), embedded application state, rendered-DOM, and
  API/XHR extraction paths were not touched — not needed for this case;
  the manufacturer page's specs were in static HTML the whole time.

## 4. Tests

| Bug | RED test | Fix | GREEN |
|---|---|---|---|
| Tailwind utility-class collisions ("aspect-square", "placeholder:" variants) falsely trigger Playwright escalation on a page whose static HTML is already complete | `tests/test_fetch.py::test_tailwind_utility_classes_do_not_trigger_false_incompleteness` (confirmed failing pre-fix: `_fetch_with_playwright` called once, expected not called) | Word-boundary regexes + colon-token exclusion in `_tag_signals`/`_html_content_complete` | Green; full `test_fetch.py` 17/17 including the pre-existing `test_empty_spec_values_require_playwright` regression guard (genuine incomplete pages still correctly detected) |
| Review widget "name → bare rating → date" triples misread as spec label/value pairs by the plain-text fallback | `tests/test_extract.py::test_review_widget_names_and_ratings_are_not_spec_pairs` (confirmed failing pre-fix: reviewer names present in extracted attribute names) | Reject the fallback pair when the value is a bare 1-5 rating immediately followed by a date-shaped line | Green; full `test_extract.py` unaffected elsewhere |

Focused run: `tests/test_fetch.py` + `tests/test_extract.py` = 63/63 PASS.
Full suite: **625/625 PASS** (623 Stage 18.7 baseline + 2 new).

## 5. Dreame live result

| Stage | Coverage | Confirmed | Category | Notes |
|---|---:|---:|---|---|
| Stage 18.7 (both cold runs) | 0.0% | 0 | unknown | `ru.dreametech.com` fetch marked `error` (Playwright timeout); `global.dreametech.com` pages fetched but are manual-download pages with no specs |
| Stage 18.8 run 1 | **5.6%** (1/18 expected) | **1** (28 of all canonical facts) | wet_dry_vacuum/medium | 2026-09-16T13:44:54.627–13:46:26.485Z, 91.858s. `ru.dreametech.com` fetched via `requests` (no Playwright), 26 raw attributes; new `ge.dreametech.com` (Georgian variant) also fetched, 5 attributes. 31 raw total. |
| Stage 18.8 run 2 (full gate) | 16.7% (1/6 expected — narrower extended schema this run) | **1** (23 of all canonical facts) | unknown/low | 2026-09-16T13:49:43.251–13:51:07.430Z, 84.179s. `ru.dreametech.com` reproduced identically: `requests`, 26 raw attributes. Category stayed undetermined this run (a separate, schema/keyword-matching limitation — most of the recovered spec labels are Russian words without a matching category-detection keyword, unrelated to extraction). |

This is the first time in the entire Stage 18.x series that Dreame has
produced any confirmed fact. Both runs independently reproduce
`ru.dreametech.com` fetching via `requests` and yielding 26 raw attributes
— the fix is deterministic and repeatable, not a one-off. Remaining low
coverage is a **mapping/schema-alias** limitation (most of the recovered
Russian labels, e.g. "Мощность", "Ёмкость батареи", don't match the
existing `wet_dry_vacuum` schema's registered Russian aliases, e.g.
"номинальная мощность") — explicitly out of this stage's scope per its own
mandate ("без нового доказанного semantic bug"), and flagged as next scope
(§10, §12).

## 6. HONOR regression

2026-09-16T13:45:48.231–13:47:18.569Z, 90.338s. `smartphone/high`
category intact. `honor.com/global/...` fetched successfully with **73**
raw attributes (up from Stage 18.7, consistent with the extraction fixes
also helping this page). 34 confirmed facts, 13 conflicts — matching Stage
18.7's 35/13 within normal run-to-run source-availability variance (this
run only 2 of 4 `honor.com` regional pages were fetched, vs. 4 before).
No regression.

## 7. Bosch regression

2026-09-16T13:44:59.396–13:45:47.541Z, 48.146s. `cooktop/high` category
intact. `bosch-home.co.uk` fetched successfully with 53 raw attributes
(existing table-based extraction unaffected). Coverage 44.4%, confirmed 0
— unchanged from Stage 18.7, correctly still limited by the separate
authority "official wording" narrowness gap already flagged and explicitly
out of this stage's scope.

## 8. Janome regression

2026-09-16T13:47:19–13:48:49Z, 89.739s. `sewing_machine/high` intact.
`janome-official.by` discovered and reached `verified` authority this run
(43 raw attributes), `janome.club` also fetched (35 raw attributes). **70.0%
coverage, 13 confirmed schema fields, 0 conflicts** — the best Janome
result across every Stage 18.x run to date. The Stage 18.6 accessories/
presser-feet schema split remains intact (0 conflicts). This result
reflects Stage 18.7's authority fix plus favorable discovery this run more
than this stage's extraction fixes specifically, but confirms no
regression from either.

## 9. Runtime/reliability

| Product | Runtime | Outer 120s hit? |
|---|---:|---|
| Bosch | 48.146s | No |
| HONOR | 90.338s | No |
| Janome | 89.739s | No |
| Gressel | 52.602s | No |
| Dreame run 1 (standalone) | 91.858s | No |
| Dreame run 2 (gate) | 84.179s | No |

No run reached the 120s outer kill boundary. No uncontrolled browser
interactions were added (Playwright usage only decreased, since the fetch-
stage fix means fewer *unnecessary* escalations — genuine JS-shell pages,
per `test_js_shell_uses_playwright`, still correctly render). No new
JSON/script scanning or API/XHR fallback was added, so no new traversal or
request-volume risk exists.

## 10. Remaining bottlenecks

| Product | Deterministic extraction | Authority | Access | External evidence availability | Mapping/schema | Genuine conflicts |
|---|---|---|---|---|---|---|
| Bosch | none open (extraction confirmed working, 53 raw attrs) | official-wording narrowness (Stage 18.7 finding, unchanged) | 1/5 fetched | n/a | n/a | none |
| HONOR | none open (73 raw attrs) | working (Stage 18.7 fix) | 1/2 fetched this run | sufficient | n/a | 13, likely genuine regional-variant differences (not re-audited this stage) |
| Janome | none open (78 raw attrs across 2 sources) | working (Stage 18.7 fix) | 2/5 fetched | sufficient | none (Stage 18.6 fix intact) | 0 this run |
| Gressel | not exercised (0/5 fetched) | n/a | 0/5 fetched | manufacturer URLs 404, retailers blocked | n/a | n/a |
| Dreame | **none open** (fixed this stage: 26-31 raw attrs, reproduced 2/2) | working (Stage 18.7 fix, `ru.dreametech.com`/`ge.dreametech.com` reach `verified`) | 1-2/5 fetched | sufficient on the reached page | **most recovered Russian labels don't match existing schema aliases** (new-scope finding) | none |

## 11. Tests / Git

- Full suite: **625/625 PASS**.
- `git diff --check`: clean (CRLF/LF advisory warnings only).
- `py_compile` over `core/`, `services/`, `adapters/`, `bot/`: clean.
- Commit created for the fetch-completeness and review-noise fixes.
- Pushed to `origin/main`: Stage 18.8's PASS criteria (Dreame `raw=0`
  cause proven and fixed with deterministic tests, extraction-safety guard
  added, no regression on Bosch/HONOR/Janome/Gressel, full suite green)
  were met.
- Working tree clean after commit.

## 12. Recommendation for next scope (not started)

1. **Russian-language schema alias coverage for `wet_dry_vacuum`** (and
   likely other categories): `ru.dreametech.com`'s real spec labels
   ("Мощность", "Ёмкость батареи", "Уровень шума", "Материал корпуса", ...)
   mostly don't match the existing schema's registered Russian aliases
   (which expect fuller phrasings like "номинальная мощность"). Now that
   extraction reliably recovers these labels, expanding the alias list
   would directly convert them into schema-level coverage. Same likely
   applies to `ge.dreametech.com`'s Georgian labels.
2. **Category detection from Russian-only spec sets**: Dreame run 2 fetched
   real, correct specs but category detection still returned `unknown` —
   worth checking whether category evidence rules require English/known-
   alias keyword matches that these Russian labels don't satisfy.
3. Both are evidence-backed by this stage's live runs; neither was started
   here, per the extraction-only mandate ("не компенсировать extraction
   failure изменениями в ... mapping/schema без нового доказанного semantic
   bug").

## 13. Git

- HEAD before this stage: `8411235a88ad39221719c3cc01b5efce7aaa5bd1`.
- Deterministic fixes applied and verified (625/625 PASS, clean diff-check).
- See repository history for the exact commit hash and `origin/main` state
  after this stage's push.
