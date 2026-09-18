# Future multi-product XLSX export -- contract only (not implemented)

Stage 31.1 fixed single-product `/export` CSV (semantic mapping, dedup,
noise filtering, RU/EN localization). It also asked for the *shape* of a
future multi-product XLSX export to be written down so later work has a
fixed target -- no XLSX writer, dependency, or command is added in this
stage.

## Sheet `Products`

One row per product (brand + model + article + market combination).

Column order:

1. `Brand`
2. `Model`
3. `Article`
4. `Category`
5. one column per canonical attribute for that product's category (see
   `core.schema.get_attribute_schema`), header = the canonical field's
   localized display name (`bot.i18n.display_name`), cell = the confirmed
   value text (same rendering as `bot.formatters.format_attribute_value`),
   blank if unresolved/conflicted.

Only user-facing attributes (`bot.attribute_filter.filter_user_facing`) are
columns; extraction noise and semantic duplicates never appear.

## Sheet `Sources`

One row per evidence item (mirrors today's `/export` CSV's per-evidence
flattening in `core/export.py`'s `profile_rows`/`export_result_csv`).

Column order:

1. `Product` -- brand + model, to join back to the `Products` sheet
2. `Article`
3. `Attribute` -- canonical name
4. `Value`
5. `Source URL`
6. `SourceType`
7. `Authority`
8. `Confidence`
9. `Evidence`

## Notes for the implementer

- Language: the whole workbook is generated in one chat's selected language
  (`bot.i18n.Language`), same as the CSV -- no per-sheet or per-row mixing.
- Row order: `Products` in request order; `Sources` grouped by product then
  by the product's attribute order (schema order, expected-first).
- This is additive: the existing single-product CSV `/export` keeps working
  unchanged when only one product is in play.
