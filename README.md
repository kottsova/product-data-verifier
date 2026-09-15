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
python -m playwright install chromium
export TELEGRAM_BOT_TOKEN="123456:ABC..."   # required for the bot only, see below
python -m bot.telegram_bot
```

### Configuration

All application settings and secrets are read from environment variables in
exactly one place: [config.py](config.py) (`AppConfig` / `TelegramConfig`).
No other module reads `os.environ`/`os.getenv` for application settings.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes, for `python -m bot.telegram_bot` only | none | Telegram bot secret. Never logged, never in a repr/to_dict. `python app.py` does not need it. |
| `PRODUCT_VERIFIER_DB_PATH` | No | `./.cache/product_verifier.sqlite3` | Path to the Stage 11 SQLite result cache (relative or absolute). |
| `PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS` | No | `2` | Max verifications the bot runs at once; integer >= 1. |
| `PRODUCT_VERIFIER_JOB_HISTORY_LIMIT` | No | `20` | How many finished jobs per chat the bot keeps for `/status`; integer >= 0. |
| `PRODUCT_VERIFIER_CACHE_TTL_SECONDS` | No | `3600` | Freshness window for the SQLite result cache; number >= 0. |
| `PRODUCT_VERIFIER_LOG_LEVEL` | No | `INFO` | Central application log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `PRODUCT_VERIFIER_LOG_FORMAT` | No | `text` | Operational log format: `text` or single-line `json`. |

Invalid values (non-numeric, or below the stated minimum) fail fast with a
`ConfigurationError`, before the bot starts polling. The Telegram token is
validated separately from the rest of the settings, so a plain
`python app.py` run never requires it.

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

### Observability and diagnostics (Stage 15)

The Telegram process writes centralized operational logs to standard error.
Stable events cover bot lifecycle/actions, background jobs, verification, and
cache outcomes. A job ID is also the verification correlation ID; direct
service calls receive their own opaque correlation ID. Durations use a
monotonic clock and in-process metrics aggregate verification/cache/job counts
and timings.

Known secrets (including `TELEGRAM_BOT_TOKEN`) and authorization-like values
are redacted from normal messages, structured fields, and server-side exception
tracebacks. Logs never include complete Telegram updates, user profile fields,
result payloads, or evidence/provenance bodies. Runtime log files remain ignored
by `.gitignore` (`*.log`).

Diagnostics are internal only: no public `/health` or `/metrics` Telegram
commands and no HTTP server are added. The framework-independent snapshot is
available from `JobManager.diagnostics().to_dict()` for future deployment
healthcheck integration; it contains uptime, counters/timing aggregates,
active/queued job counts, repository state, and the cache schema version.

## Docker deployment (Stage 16)

The production image runs one non-root bot process on pinned Python 3.13.13,
includes the Playwright Chromium runtime, writes logs only to stdout/stderr,
and starts with `python -m bot.telegram_bot`. Build it with:

```text
docker build -t product-data-verifier:stage16 .
```

Create a persistent volume and pass the token from the current environment;
the token is not copied into the image:

```text
docker volume create product-verifier-data
docker run -d --name product-data-verifier \
  --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN \
  -e PRODUCT_VERIFIER_DB_PATH=/data/product-verifier.sqlite3 \
  -v product-verifier-data:/data \
  product-data-verifier:stage16
```

The image default for `PRODUCT_VERIFIER_DB_PATH` is
`/data/product-verifier.sqlite3`; the local Python default remains unchanged.
The named volume inherits the image's `/data` ownership for UID/GID `10001`.
If a bind mount is used instead, make its host directory writable by that ID.

For the included production-like Compose setup, export
`TELEGRAM_BOT_TOKEN` in the shell (or provide it through the deployment
platform's environment/secret facility), then run:

```text
docker compose up -d --build
docker compose logs -f bot
```

Compose mounts the `product-verifier-data` named volume, uses
`restart: unless-stopped`, and supplies all Stage 14/15 settings through the
existing environment configuration. It contains no token value.

### Container operations

Docker runs `python -m healthcheck` as its healthcheck. The command validates
`AppConfig`, SQLite availability, and the diagnostics snapshot without needing
a Telegram token, calling Telegram, or running a product verification:

```text
docker compose exec -T bot python -m healthcheck
docker inspect --format '{{json .State.Health}}' product-data-verifier
```

`SIGTERM`/`SIGINT` are handled by python-telegram-bot. Its shutdown hook stops
new work and gives `JobManager` up to 30 seconds to finish/cancel jobs; Compose
allows 45 seconds before enforcing container termination. Running verification
threads are not force-killed inside Python.

For a simple consistent SQLite backup, stop the bot, copy the database, then
restart it. The named volume and database survive container recreation:

```text
docker compose stop bot
docker compose cp bot:/data/product-verifier.sqlite3 ./product-verifier.backup.sqlite3
docker compose start bot
```

## Current stage

Stage 16 — Deployment.
