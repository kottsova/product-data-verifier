# Stage 36.5 discovery baseline

This directory preserves the **historical** 50-product discovery run made on
code commit `bd9238e`. `raw.zip` contains sanitized copies of the original
`01.json` through `50.json`; `manifest.json` records SHA-256 hashes of the
untouched originals and of the archive. The original files remain at the
scratchpad path named in the Stage 36.6 task. Import was done with
`python -m diagnostics.import_stage36_5 SOURCE raw.zip manifest.json`.

Run `python -m diagnostics.stage36_5_baseline --archive diagnostics/baselines/stage36_5/raw.zip`
to read the historical automatic totals, or add `--rows` for all recorded
inputs and outcomes. `diagnostics/stage36_5_baseline.py` also contains the
structured 50-product list and a live runner. The live runner writes a new
directory and refuses to overwrite any existing result.

## Conditions and limitations

- The old `bench365.py` called `DiscoveryDebugService().discover_name(name)`
  once per input, called `clear_official_domain_cache()` between
  inputs, waited two seconds after each, and saved each result separately.
  `market` was `global`; the service's default wall-clock budget was 75 s.
  Search-provider health/circuit state and other caches were not explicitly
  reset. The archived results include individual provider outcomes and times.
- The word **cold** in earlier notes can only mean the explicit
  `clear_official_domain_cache()` action, which cleared both the process-local
  official-domain and official-surface caches at `bd9238e`. It does not establish a completely
  cold provider or network environment. Recorded run times sum to 2234.6 s;
  the log has an EPIPE after all 50 JSON files were written.
- The archive stores candidate lists, titles, URLs, discovery trace, provider
  telemetry, page-inspection summaries, and document decisions. It does not
  store fetched HTML or PDF response bodies. A new site fetch cannot be used
  to claim historical content verification. URL query parameters resembling
  credentials were redacted, which can make individual historical links
  unusable for direct replay.
- Historical automatic results: `PASS`/`exact_official_found` **31/50**;
  any automatic official source or document **36/50**; 26 document records
  across the run. These are program outputs, **not manually adjudicated
  precision**. Stage 36.5 did not run attribute extraction. The prior manual
  claims of 26/50 exact product pages, 29/50 any exact official source, and
  8/50 exact documents require a complete source-level audit; they are not
  established by the saved JSON alone.
- The old free-text parser split `The North Face Nuptse` as brand `The North`
  and model `Face Nuptse`. The structured list in the restored harness uses
  `The North Face | Nuptse`; a corrected run must be reported separately.
  Entry 11 contains a replacement character in the archived brand name,
  reflecting an input encoding error. The structured list uses `Kärcher`.
- Input identity levels are listed in the harness. SKU, model and family
  requests need different exactness rules. A family page cannot prove one
  variant's specifications, and a product family cannot be counted as an
  exact SKU source. Uninspected content and HTTP availability are distinct.

`raw.zip` is 539,350 bytes; the 50 uncompressed originals total 6,315,066
bytes. The archive is the compact retained diagnostic artifact, not a new
baseline or a new live run. The commit named above is the code baseline;
`manifest.json` binds the archived evidence to that commit.
