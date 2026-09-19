# Stage 34.1 — Official document identity

**Verdict: PASS.** Only the quality of already-found official documents changed. Page discovery, extraction,
normalization and `PageInspection.spec_location` are untouched.

## Rule (generic, no product knowledge) — `core/document_identity.py`

A link from an official page, an official host, a product-family resemblance or a neighbouring internal
identifier is *context*, not identity. Every **exact PDF document** is now read (text layer, bounded: 25 MB,
200 pages, 25 s) and judged by its own text:

| Verdict | Evidence |
| --- | --- |
| `exact` | text names the requested SKU, the same SKU with a regional/market code, or the base SKU when the requested suffix is a market code (`HX9992` for `HX9992/12`) |
| `probable` | family evidence only: base SKU without a non-regional requested suffix, model stem (`EC685` for `EC685M`), or all product-name words but no SKU |
| `unverified` | nothing identifies the model: different internal identifier, generic text, download failed, unreadable or scanned PDF |

`unverified` documents stay in `documents` (with `identity_evidence` and the reason appended) but sort last, are excluded
from canonical fields (`manual_url`, `declaration_url`, ...) and are ignored by extraction (it only uses exact/probable).
HTML document pages are not checked in this stage.

## Regression (live discovery, 2026-09-19)

| Product | Document | Before | After |
| --- | --- | --- | --- |
| Bosch HBG7741B1 | `21400895_EU_DoC_Bosch_HT6B60F0S.pdf` | exact | **unverified** (`not_in_text`) |
| Philips HX9992/12 | manual 2021 (`43e2cf01…pdf`) | exact | **probable** (`family_words`; SKU not in text) |
| Philips HX9992/12 | EU + UK declarations | exact | exact (`document_text`: lists HX9992 among siblings; `/12` is a market code) |
| Gressel GAF-1825 | instruction manual | exact | exact (`document_text`) |
| Makita DHP484Z / DEWALT DCD796P2 | no documents | - | unchanged (all pages still exact) |
| Dreame HHR32A (info) | `DOC-W2545E_HHR32A.pdf` | exact | exact |

Stage 34 extraction re-run on the new discovery results: identical counts (Bosch 355, Philips 107, Gressel 39,
Makita 15, DEWALT 23); the Bosch declaration is no longer fed to extraction at all.

Tests: 1032 OK (11 new in `tests/test_stage34_1_document_identity.py`). Traces: `diagnostics/results/stage34_1/`.
Harness: `python -m diagnostics.stage34_1_document_identity`.

## Notes

* A download failure marks a document `unverified` (identity cannot be proven); reads retry twice.
* Discovery now downloads exact PDFs (≈1–10 s each, parallel); no change to the search phase.
* Not addressed (out of scope): DeLonghi locale instability, `spec_location=unknown`.
