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

## Telegram Bot

```text
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456:ABC..."                     # required
export PRODUCT_VERIFIER_DB_PATH="./.cache/product_verifier.sqlite3"  # optional
export PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS="2"                # optional
export PRODUCT_VERIFIER_JOB_HISTORY_LIMIT="20"                 # optional
python -m bot.telegram_bot
```

Send the bot a message like `Bosch PUE611BB5E` or `Bosch | PUE611BB5E` (a
third `| article` part is optional). `/start` and `/help` explain the format.

Verification runs as a **background job**, not inline in the handler: you
get an immediate "Принял..." reply, then a "started" update once processing
actually begins, then the final result -- so one slow check never blocks the
bot from answering other messages. `PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS`
caps how many verifications run at once; extra requests wait as `queued`.
Sending the same product again while it's already queued/running does not
start a second check. `/status` lists your current queued/running jobs;
`/cancel` cancels them (a queued job stops immediately; a running one is
flagged and its result is discarded when the background computation
finishes -- the underlying thread cannot be force-killed).

Persistence: the Stage 11 SQLite product-result cache is durable across
restarts. Telegram job tracking (`/status`, `/cancel`) is **in-memory only**
and is lost on restart -- after a restart, resend your message; if that
product's result is still fresh in the SQLite cache, the check will be fast
even though job tracking was reset. (The CLI does not use this cache --
see the Stage 12 report.)

## Current stage

Stage 13 — Telegram UX & Async Job Handling.
