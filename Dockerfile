FROM python:3.13.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/app \
    PRODUCT_VERIFIER_DB_PATH=/data/product-verifier.sqlite3

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --no-deps --requirement requirements.lock \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY --chown=app:app app.py config.py healthcheck.py observability.py ./
COPY --chown=app:app bot/ ./bot/
COPY --chown=app:app core/ ./core/
COPY --chown=app:app services/ ./services/

VOLUME ["/data"]
USER app:app

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-m", "healthcheck"]

CMD ["python", "-m", "bot.telegram_bot"]
