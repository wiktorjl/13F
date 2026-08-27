# 13F Explorer — read-only stdlib server in a container.
#
# Python standard library only: there is no pip install step. The image ships the
# scripts and the six static assets; everything the server reads at runtime
# (13f.sqlite, prices.sqlite, fund_signals.sqlite and the JSON inputs) lives in
# /app/data, which is a volume — bind-mount a data/ directory built elsewhere.
# Source archives (*_form13f.zip) are deliberately not copied; run with
# TRUST_DATABASE=1, or mount them and point ARCHIVE_DIR at the mount.
FROM python:3.13-slim

# make and nodejs are optional system tools for `make signals` / `make verify-fast`
# inside the container (verify.py runs `node --check`). Neither is a runtime
# dependency of server.py; drop this layer for a smaller image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends make nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BASE_PATH="" \
    TRUST_DATABASE=""

WORKDIR /app

# Non-root runtime user. uid/gid 1000 matches `user: "1000:1000"` in
# docker-compose.yml and the expected owner of the bind-mounted ./data.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-log-init --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --chown=app:app \
    server.py build_database.py refresh_fund_signals.py refresh_market_caps.py \
    enrich_tickers.py verify.py Makefile \
    index.html app.js styles.css dashboard.html dashboard.js dashboard.css \
    README.md \
    ./
COPY --chown=app:app tests/ ./tests/

USER app

VOLUME ["/app/data"]
EXPOSE 8080

# Probe the public prefix so a misconfigured BASE_PATH shows up as unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import os, sys, urllib.request; url = 'http://127.0.0.1:8080' + os.environ.get('BASE_PATH', '') + '/api/meta'; sys.exit(0 if urllib.request.urlopen(url, timeout=5).status == 200 else 1)"]

# Flags come from the environment: BASE_PATH (--base-path), TRUST_DATABASE
# (--trust-database, implies --no-build), ARCHIVE_DIR. Compose may append
# extra flags such as --no-build through `command:`.
ENTRYPOINT ["python3", "server.py", "--host", "0.0.0.0", "--port", "8080"]
CMD []
