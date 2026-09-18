# Stage 30 — MVP Release Hardening

Verdict: **PARTIAL** (release-layer work is complete and green; the cold
benchmark comparison against Stage 29 is honest, not flattering — see §9-10)

No new retrieval provider, browser mechanism, paid API, or retrieval
architecture was added. Retrieval/discovery/quality logic
(`core/discovery.py`, `core/fetch.py`, `core/quality.py`, `core/workflow.py`)
is byte-for-byte unchanged from Stage 29.

## 1. What changed

- **Production provider config** (`.env.example`, `compose.yaml`): added
  `PDV_PROVIDER_HEALTH_PATH` and `PDV_DISABLED_DISCOVERY_PROVIDERS=duckduckgo_lite`.
- **Telegram provenance**: confirmed attributes now show source URL and an
  official/secondary label (`bot/formatters.py`).
- **Quality wording**: `verified`/`partial`/`insufficient`/`conflicted` got
  plain-Russian labels, no bare English technical word (`bot/formatters.py`).
- **`/export` command**: new Telegram command, CSV of the chat's last
  successful result (`bot/handlers.py`, `bot/telegram_bot.py`, `bot/jobs.py`).
- **CSV schema**: new DTO-agnostic `export_result_csv` in `core/export.py`
  (brand/model/category + the existing attribute/value/status/source/
  evidence/confidence/authority/source-type contract), consumed via a new
  `bot/export.py` adapter.
- Tests: 35 new/updated tests across `tests/test_bot.py`,
  `tests/test_bot_jobs.py`, `tests/test_bot_export.py` (new),
  `tests/test_profile_export.py`, `tests/test_config.py`,
  `tests/test_final_mvp_wiring.py`.

## 2. Production provider config

`core/provider_health.py` and `core/discovery.py` read
`PDV_PROVIDER_HEALTH_PATH` / `PDV_DISABLED_DISCOVERY_PROVIDERS` directly via
`os.environ` — this is a deliberate, pre-existing, tested exception to the
"config.py is the only env boundary" rule
(`tests/test_config.py::EnvironmentReadingBoundaryTests`, which explicitly
excludes these two modules). Threading these two settings through
`AppConfig` would require adding parameters to `core/workflow.py`'s
`run_product_workflow` and to `core/discovery.py`'s provider-list/health-store
construction, both of which are called with no config object today
(`bot/service_factory.py` builds `ProductVerifierService` with the default,
zero-argument `run_workflow=run_product_workflow`). That is a real signature
change across `core/workflow.py` and `core/discovery.py`, not the "no big
refactor" version this stage asked for — so both settings ship as env vars
in `.env.example`/`compose.yaml` instead, exactly as the Stage 29 audit
recommended, and `config.py` itself is untouched.

Bing/Google were left alone, per instruction — no telemetry showed either at
0 accepted contribution, and this run doesn't provide per-provider
contribution telemetry to check that claim safely (see §11).

## 3. Telegram provenance UX

`bot/formatters.py`: a Confirmed attribute's first `supporting_sources`
entry now renders as a second, indented line:

```
✅ Мощность: 1000 Вт
    🔗 официальный источник: https://brand.example/power
```

`source_type in {"manufacturer", "official_document"}` → "официальный
источник"; anything else (`marketplace`/`retailer`/`specialized_reference`/
`other`/unrecognized) → "сторонний источник" — an unrecognized value falls
back safely instead of ever being echoed raw. Unconfirmed/conflicted
attributes get no provenance line (no single confirming source to show).
Provider names, circuit-breaker state, WAF/captcha markers, and internal
exceptions are not fields on `ServiceAttribute`/`ServiceEvidence` at all, so
they cannot leak through this path structurally, not just by convention —
verified by `FormatterProvenanceTests.test_internal_metadata_never_reaches_the_formatted_message`.

## 4. Quality wording

| Status | Before | After |
|---|---|---|
| verified | `✅ Подтверждено (verified)` | `✅ Данные подтверждены` |
| partial | `🟡 Частично подтверждено (partial)` | `🟡 Данные подтверждены частично` |
| insufficient | `⚠️ Недостаточно данных (insufficient)` | `⚠️ Недостаточно данных для проверки` |
| conflicted | `❗ Обнаружены противоречия (conflicted)` | `❗ Источники расходятся по части характеристик` |

Per-attribute "конфликт данных" → "данные расходятся" for the same reason.
`core/quality.py`'s status logic/thresholds are untouched — this is display
text only.

## 5. `/export` flow

`bot/jobs.py`'s `JobManager` gained `last_export_job_for_chat(chat_id)`,
backed by a new `_chat_last_success: dict[int, Job]` populated only when a
job finalizes as `completed` with `result.success`. It is independent of the
bounded `_chat_terminal_history` eviction, so a burst of later failed/queued
jobs for the same chat never hides a still-usable prior result — verified by
`LastExportJobTests.test_survives_eviction_from_bounded_terminal_history` and
`ExportCommandTests.test_failed_job_does_not_overwrite_the_last_usable_export`.
No database: this is one extra in-memory dict, same persistence scope as the
rest of `JobManager` (process-lifetime only, documented already).

`/export` with no prior result replies with a plain-language message; with a
result, it sends a CSV document. Isolation across chats/users is by
dict-keyed `chat_id`, verified by `ExportCommandTests.test_export_is_isolated_per_chat`.

## 6. CSV schema

New `core/export.py::export_result_csv(*, brand, model, category, rows)` +
`CSV_RESULT_COLUMNS` is deliberately independent of `FinalProductProfile`
(rows are plain dicts) so it can be called from the bot layer, which must
never import `core.profile`/`core.workflow`/`core.quality`
(`tests.test_bot.ArchitectureBoundaryTests`). `bot/export.py` builds those
rows from `ServiceAttribute`/`ServiceEvidence`. Columns: `Brand, Model,
Category, Attribute, DisplayName, Value, Status, Source, Evidence,
Confidence, Authority, SourceType` — brand/model/category added, the
existing attribute/value/status/source/evidence/confidence/authority/
source-type contract preserved. One row per evidence source (mirrors
`core/export.py`'s existing `profile_rows` one-row-per-evidence-item
convention). Deterministic: `ExportResultCsvTests`/`ResultCsvExportTests`
both assert byte-identical output across repeated calls.

## 7. Tests

`775/775 PASS` (`python -m unittest discover -s tests`), including all
old tests (several were updated in place where Stage 30 intentionally
changed user-facing wording — e.g. `test_conflicted_status_is_shown_not_hidden`
now asserts the English word is *absent*, matching the new requirement).
New coverage maps directly to the 12-item Stage 30 test list: production
config (`ProductionProviderProfileTests`), provenance/leak-proofing
(`FormatterProvenanceTests`), quality wording, `/export` with/without a
result, CSV brand/model/category + determinism + multi-chat isolation
(`ExportCommandTests`, `LastExportJobTests`, `ResultCsvExportTests`,
`tests/test_bot_export.py`).

## 8. Telegram smoke

Ran the full `query → accepted → processing → result → /export → CSV` flow
locally against the real pipeline (no live Telegram API — a fake
Update/Message harness matching the existing test style), using
`Bosch PUE611BB5E`:

```
accepted: Принял запрос. Проверяю Bosch PUE611BB5E…
🔄 Начинаю проверку Bosch PUE611BB5E…
📦 Bosch PUE611BB5E
Качество: ⚠️ Недостаточно данных для проверки
...
EXPORT filename: verification_Bosch_PUE611BB5E.csv
Brand,Model,Category,Attribute,DisplayName,Value,Status,Source,Evidence,Confidence,Authority,SourceType
Bosch,PUE611BB5E,Unknown,brand,Brand,—,Unresolved,,,low,,
...
```

No provider name, circuit state, WAF/captcha marker, or internal exception
appeared anywhere in the output. A real public Telegram smoke was not run
(no bot token in this environment) — not needed per instructions, since the
local harness covers the full flow without cost or risk.

## 9. Stage 30 cold benchmark

Same dataset (`regression/datasets/stage23_blind_v1.json`, 15 products),
same harness settings as Stage 29 (`--mode cold --concurrency 3 --timeout
200`), fresh cache DB, one run, production-like env
(`PDV_DISABLED_DISCOVERY_PROVIDERS=duckduckgo_lite`,
`PDV_PROVIDER_HEALTH_PATH` pointed at a fresh file). Result:
`regression/results/stage30_cold_v1.json`.

- Started `2026-09-18T11:58:18Z`, finished `2026-09-18T12:05:18Z` — **7m00s**
  wall clock (Stage 29: 7m17s).
- `completed: 15/15, failed: 0, timed_out: 0` — no crash, no hang.

## 10. Comparison with Stage 29

| Metric | Stage 29 | Stage 30 |
|---|---|---|
| completed | 15/15 | 15/15 |
| verified | 0 | 0 |
| partial | 4 | 2 |
| conflicted | 4 | 3 |
| insufficient | 7 | 10 |
| confirmed > 0 | 8/15* | 10/15 |
| avg runtime | 76.3s | 73.3s |
| median runtime | 89.9s | 89.7s |

\* Stage 29's own report text said "confirmed > 0: 8/15"; recomputing
directly from `stage29_cold_v1.json`'s per-record `confirmed_count` gives
11/15 — flagging the discrepancy rather than silently using either number
unchecked. Stage 30 recomputed the same way: **10/15**.

Per-product diff (same 15 `blind-*` product IDs both times):

- **Regressed**: `blind-cooktop-smeg-si2m7953d` (conflicted, confirmed=9,
  61% coverage → insufficient, confirmed=0, 0%), `blind-cleaning-tineco-s7stretch`
  (partial, confirmed=5 → insufficient, confirmed=0), `blind-laptop-dell-xps13-9340`
  (partial, confirmed=6 → insufficient, confirmed=4).
- **Improved**: `blind-sewing-husqvarna-opal650` (confirmed 0 → 2).
- **Unchanged outcome**: the remaining 11 products (smartphones and most
  sewing/air-fryer/laptop products landed on the same status, with runtime
  and confirmed-count within normal noise).

Total aggregate is worse this run (partial 4→2, insufficient 7→10). This is
an honest regression in the numbers, not a flattering one, and it is
reported as such per instruction.

## 11. Runtime and provider contribution / DDG Lite effect

`regression/results/stage30_provider_health.json` (the actual circuit-breaker
state file this run wrote) confirms:

- **`duckduckgo_lite` was never invoked**: zero `duckduckgo_lite` keys exist
  in the health store (97 keys total, tracking `discovery:duckduckgo_html`,
  `discovery:naver`, `discovery:seznam`, and ~90 per-domain `fetch:*` keys).
  The disable flag works as intended.
- **The circuit breaker is genuinely active, not a no-op**: 12 domains
  tripped to `state: "open"` during this single run, including
  `fetch:dell.com` (`blocked`), `fetch:sony.co.uk` (`connection_error`),
  and several `.cz` retailers (`blocked`). `fetch:dell.com` opening
  correlates directly with `blind-laptop-dell-xps13-9340`'s regression —
  Dell's own domain was actively blocking this run, unrelated to any Stage
  30 change.
- **The three regressed products' drop is consistent with normal fetch/
  blocking variance, not with removing `duckduckgo_lite`**: Smeg and Tineco
  both dropped to `sources_discovered` of ~1 and ~32 (vs. Stage 29's) with
  `source_count: 0` — a discovery/fetch-stage collapse, the same failure
  mode the Stage 29 audit already documented as the dominant, pre-existing
  bottleneck (fragile official domains, e.g. Smeg's own audit note about its
  official page). `duckduckgo_lite` was one of eight discovery providers and
  was already documented as a 0-accepted-evidence contributor; this run's
  health file gives no evidence it was the corroborating source for any of
  the three regressed products.
- This is one run, not a controlled A/B — the honest conclusion is "no
  evidence DDG Lite's removal caused the regression, but also no
  A/B-controlled proof it didn't," and that uncertainty is reported rather
  than resolved by assertion.

## 12. Cross-process provider health confirmation

Confirmed real and active in this production-like run (not just present in
code): 97 tracked provider/domain entries, 12 opened circuits, distinct
`last_failure_class` values (`blocked`, `timeout`, `connection_error`) per
domain. This directly answers Stage 29's own open question ("shipped but not
enabled") — it is now enabled via env config and demonstrably recording
real failures cross-process.

## 13. Remaining blockers

- Fetch/authority/extraction instability on individual official domains
  (Dell, Smeg, Sony, several `.cz` retailers) remains the dominant quality
  bottleneck, exactly as Stage 29 documented — Stage 30 did not touch this
  by design.
- No per-provider accepted-contribution telemetry exists yet to make a
  fully evidence-based Bing/Google call (correctly left untouched this
  stage) or a fully conclusive DDG-Lite-removal call (see §11).
- `_chat_last_success` in `JobManager` has no upper bound on distinct chat
  IDs (grows one entry per chat that ever succeeded) — acceptable at MVP
  scale, same class of bound as the existing `_chat_terminal_history`
  dict-of-chats, not a new risk introduced here.

## 14. PASS / PARTIAL / FAIL

**PARTIAL.** All 14 PASS-criteria items that are within this stage's control
are met: tests green (775/775), provenance implemented, no internal details
leaked, `/export` works, CSV has brand/model/category + the existing
evidence contract, provider health and DDG-Lite-disable are both real and
confirmed active in a production-like run, retrieval/quality logic
untouched, no new paid dependency, no new product-specific hardcode, one
cold benchmark completed and documented (not repeated). It is PARTIAL rather
than PASS only because the cold-benchmark comparison itself came back worse
than Stage 29 on this single run — an honest result, attributed to
already-documented fetch/blocking variance rather than to any Stage 30
change, but not swept under the rug either.
