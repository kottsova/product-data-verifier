# Product Data Verifier

Сервис для поиска, извлечения и проверки товарных характеристик по модели товара.

## Основной сценарий

Brand + Model + Article
→ Product identity
→ Discover sources
→ Fetch and extract
→ Detect category
→ Build and extend schema
→ Map attributes
→ Analyze gaps and run bounded targeted search
→ Validate and resolve conflicts
→ FinalProductProfile
→ JSON / CSV / tabular output

## Результат

Attribute | Value | Status | Source | Evidence

Статусы:

- Confirmed
- Conflict
- Unresolved

## CLI

```text
python app.py "Bosch" "PUE611BB5E" --format table
python app.py "Bosch" "PUE611BB5E" --format json --pretty
python app.py "Bosch" "PUE611BB5E" --format csv
```

Use `--no-targeted-search` for an initial-source-only run and `--max-sources`
to bound initial fetches.

## Current stage

Stage 8 — Application orchestration.
