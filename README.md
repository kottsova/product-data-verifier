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

## Telegram Bot (MVP)

```text
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456:ABC..."       # required
export PRODUCT_VERIFIER_DB_PATH="./.cache/product_verifier.sqlite3"  # optional
python -m bot.telegram_bot
```

Send the bot a message like `Bosch PUE611BB5E` or `Bosch | PUE611BB5E` (a
third `| article` part is optional). `/start` and `/help` explain the format.
Results are cached in the Stage 11 SQLite store; a cached reply is marked
accordingly. (The CLI does not use this cache -- see the Stage 12 report.)

## Current stage

Stage 12 — Telegram Bot MVP.
