# 13F Dashboard

A small, read-only web page over multi-quarter SEC Form 13F data. It turns every `*_form13f.zip` archive in this directory into canonical quarterly manager snapshots and lists securities in three views — Top Holdings, Fresh Initiations, Top Movers — plus an About page. Python standard library only; no build step, no npm.

## Run it

```bash
python3 server.py
```

The first run builds `data/13f.sqlite`; this can take a few minutes. The server rebuilds when archive contents, ticker mappings, the market-cap snapshot, the sector snapshot, the featured-managers file, or the schema changes.

By default it binds only to `127.0.0.1` on port `8013`. Open <http://127.0.0.1:8013/> — the root is Top Holdings; `/initiations`, `/movers`, and `/about` are the other pages. To use it from another device on a trusted network, opt in with that machine's LAN address:

```bash
python3 server.py --host 192.168.x.x
```

Non-loopback binding prints a warning. The HTTP server exposes exactly three static assets (`dashboard.html`, `dashboard.js`, `dashboard.css`), the four page routes and their three legacy aliases (`/dashboard`, `/dashboard/initiations`, `/dashboard/movers`), and two JSON endpoints (`/api/meta`, `/api/dashboard`). Everything else — including trailing-slash variants such as `/about/` — is a 404. Source archives, scripts, caches, and the SQLite database are never served.

Use `--port` to choose another port and `--no-build` to require an already-current database. Two more flags exist for hosting the page behind a reverse proxy — `--base-path /13f` (or `BASE_PATH=/13f`) serves everything under a URL prefix, and `--trust-database` (or `TRUST_DATABASE=1`) starts from a database built elsewhere without re-hashing the source archives; see [Deploying with Docker](#deploying-with-docker). Rebuild manually after changing the archives:

```bash
python3 build_database.py --force
```

## Dashboard

The page is deliberately minimal: a single centered list of securities with a plain-text header row (Ticker, Name, the view's metric, Price, Day, YTD, Sector) whose labels sort the list, and no filters, search, charts, or fund metadata. Every row links its ticker and name to Yahoo Finance. The header nav has four links:

- **Top Holdings** (`/`) — every security held by at least one manager in the latest quarter, ordered by average portfolio weight.
- **Fresh Initiations** (`/initiations`) — securities that gained first-time holders versus the prior quarter.
- **Top Movers** (`/movers`) — change in average weight over 1Q–4Q, with a Gainers | Losers switch and a timeframe toggle (`?horizon=2&side=losers`).
- **About** (`/about`) — a static explanation of Form 13F, the three metrics, the data sources, and the snapshot caveats. It carries no query state; the only dynamic parts are three numbers filled from `/api/meta` (number of quarters, the span of period labels formatted as `31 Mar 2023 to 31 Mar 2026`, and the latest quarter's manager count rounded to the nearest hundred), which show `—` until the response arrives or if it fails.

The pre-landing-page URLs `/dashboard`, `/dashboard/initiations`, and `/dashboard/movers` still serve the same views so old links keep working; the page rewrites them to the canonical path on load and never emits them. The page reads one data endpoint, `/api/dashboard`; the About page reads `/api/meta` once.

### Unmapped securities

By default the list contains only securities that have a ticker. Rows whose `securities.ticker` is blank — a CUSIP that neither the exact OpenFIGI lookup nor the SEC issuer-name match resolved — are dropped in all three views before sorting, paging, and counting, so `count` reflects the filtered list (Movers evaluates the P−k side first and drops an unmapped security only afterwards). Append `?unmapped=include` to any view URL to show them; they render with an em dash in the Ticker column and their SEC issuer name. The switch travels with view, sort, and page changes, is omitted from the URL at the default, and has no visible control. `/api/dashboard` takes the same `unmapped` ∈ `exclude`, `include` parameter (default `exclude`), echoes the effective value, and answers 400 to anything else.

### Metric definitions

The metrics come from per-period rollups (`dashboard_period_stats`, `security_weight_stats`) that the database build materializes from the canonical snapshots. With P the latest reporting period and P−k the period k quarters earlier:

- **Weight universe.** For each period, every manager whose filing is `COMPLETE` and whose reported total value is positive. N is the size of that set.
- **Holding.** A manager holds a security in a period when it reports at least one non-option position for it, in any amount type and at any value. Long puts and calls never count as holdings.
- **Manager weight.** The summed reported value of the manager's non-option positions in the security divided by the manager's full reported 13F value for that period, options included.
- **Avg Weight** (Top Holdings) is 100 × the sum of manager weights over the whole universe ÷ N, so non-holders contribute zero. It is the equal-weighted average portfolio weight across all complete filers, not the average among holders. Rows are every security held by at least one universe manager in P, ordered by Avg Weight, then holder count, then CUSIP. The arrow compares P with P−1: ▲ more concentrated, ▼ less concentrated, — unchanged (a security absent from P−1 counts as zero there; with no P−1 at all every row is —).
- **New Holders** (Fresh Initiations) counts managers with `COMPLETE` filings in both P and P−1 that hold the security in P and did not hold it in P−1. It is "new versus the prior quarter", not "never held in any archive". Rows are securities with at least one new holder, ordered by New Holders, then Avg Weight, then CUSIP. The arrow compares the count with the same security's New Holders in P−1: ▲ more first-time holders than last quarter, ▼ fewer, — equal (a security with no prior row counts as zero there).
- **Weight Change** (Top Movers) is Avg Weight in P minus Avg Weight in P−k, in percentage points, over the union of securities with a rollup row in either period; a security missing from one side is zero there. Gainers are positive changes, largest first; Losers are negative changes, most negative first; exact zeros appear on neither side. When P−k predates the oldest archive the view is empty.

Precision: Avg Weight and Weight Change show one decimal at or above 1 and two decimals below (`2.1%`, `0.45%`, `+0.12pp`, `−1.3pp`); New Holders is an integer with thousands separators (`1,045 new`); prices show two decimals; Day Change and YTD show one decimal with an explicit sign. `/api/dashboard` rounds metrics to four decimals. Lists longer than 100 rows page with plain Previous / Next links.

### Sorting

Clicking a column header sorts the whole list by that column (the API sorts before paging, so `count` never changes and page 1 always holds the extremes); clicking the active header flips the direction. The active header is charcoal and underlined with a trailing `↑`/`↓`, and `aria-sort` reflects it. The metric column starts at the view's own default order (largest first; Losers most negative first), Price, Day and YTD start descending, and Ticker, Name and Sector start ascending. Text sorts are case-insensitive; rows with no ticker (only visible with `unmapped=include`), no sector, or no price (symbol missing from the cache, or no close on the mark date) sort last in both directions and then keep the view's default order, so a price sort against an absent cache is simply the default list. Switching views resets the sort; changing the Movers side or timeframe keeps it. The state travels in the URL as `?sort=<column>&direction=asc|desc` (omitted when equal to the view's defaults), and `/api/dashboard` takes the same two parameters: `sort` ∈ `metric`, `ticker`, `name`, `price`, `day`, `ytd`, `sector` (default `metric`) and `direction` ∈ `asc`, `desc` (default `desc`, or `asc` for `view=movers&side=losers`); the response echoes the effective values and anything else is a 400. The headers are plain text links, not buttons or icons.

### Names, sectors, and prices

Names and sectors are a static ticker-keyed mapping in `data/sectors.json` (see [Data sources and refresh](#data-sources-and-refresh)). A security without a screener entry falls back to its SEC issuer name and an empty sector; a security without a ticker does the same but is hidden unless `unmapped=include` is set. Sectors are abbreviated to at most four characters (`Tech`, `Hlth`, `Fin`, `Disc`, `Stpl`, `Indu`, `Enrg`, `Util`, `RE`, `Matl`, `Tele`, `Misc`, `ETF`). All-caps SEC issuer names are title-cased for display.

Price, Day Change, and YTD come from the offline `data/prices.sqlite` cache; the server attaches it read-only for dashboard requests only and never fetches prices itself. Price is the close on the cache's common mark date; Day Change compares it with the previous cached close; YTD compares it with the last close of the prior December. A ticker with no close on the mark date shows `—`, and when the cache is absent every price field is `—` (`price_available: false`).

### What the numbers are not

The dashboard shows quarter-end reported snapshots and their changes, lagged by the 45-day filing window. Average weights are not portfolio value or performance, weight changes are not confirmed purchases or sales — price moves, splits, and a manager changing its reporting entity all move them — and a fresh initiation is a disclosed first-time position relative to the prior quarter, not a recommendation. Nothing on the dashboard is investment advice.

## Data sources and refresh

Everything under `data/` is derived from, or cached for, the archives. Four inputs, four refresh paths:

**Filings → `data/13f.sqlite`.** The `*_form13f.zip` bulk datasets from the SEC are the only source of truth. `python3 server.py` builds the database on first run and rebuilds whenever an archive or any JSON input below changes (identity is a content hash, not a timestamp); `python3 build_database.py --force` rebuilds on demand (roughly ten minutes, about 1 GB of RAM). Besides the dashboard rollups the build still materializes older per-security and per-manager change rollups and honours `data/starred_funds.json` (every listed CIK must exist in the archives or the build fails); the dashboard reads neither, and they are left in place because removing them is a schema bump and a full rebuild.

**Prices → `data/prices.sqlite`.** Daily closes from Nasdaq's [Historical Quotes interface](https://www.nasdaq.com/market-activity/quotes/historical), covering every security with a non-blank ticker (about 1,645 symbols including ETFs, which are fetched through Nasdaq's ETF route). The cached closes are retrospectively split-adjusted. Refresh with:

```bash
make signals
```

The refresh writes `data/prices.sqlite` atomically (and, as a by-product, a `data/fund_signals.sqlite` sidecar that the dashboard does not read). It needs the main database to exist and needs network; HTTP requests never fetch market data, and a refreshed cache is picked up on the next request without a restart.

**Sectors and market caps → `data/sectors.json`, `data/market_caps.json`.** One download of the [Nasdaq Stock Screener](https://www.nasdaq.com/market-activity/stocks/screener) merged with the [Nasdaq ETF Screener](https://www.nasdaq.com/market-activity/etf/screener) (ETFs receive the sector `ETF`; stock names are cleaned of `Common Stock`-style suffixes). The dashboard uses `sectors.json` for names and sectors; `market_caps.json` is a hash-tracked build input the dashboard does not read. Both are snapshot data as of retrieval, and a change to either invalidates the database:

```bash
python3 refresh_market_caps.py      # writes data/market_caps.json and data/sectors.json (--skip-sectors / --sectors-output)
python3 build_database.py --force
```

**Tickers → `data/cusip_tickers.json`.** Database builds read CUSIP → ticker mappings from this cache when it exists. To build or resume it and fill blank ticker fields in the current database:

```bash
python3 enrich_tickers.py --limit 250 --update-db
```

The enrichment prefers exact CUSIP mappings from OpenFIGI, then uses the official SEC company-ticker file only when issuer matching is uniquely resolvable. Verified exceptional mappings live in the cache's provenance-bearing `manual_overrides` object without erasing automated lookup diagnostics. Ambiguous or missing mappings remain blank (and therefore hidden from the dashboard by default), existing database tickers are not overwritten, and progress is cached atomically so later runs resume safely. Preview candidates without network access or writes with `--dry-run`. Set `OPENFIGI_API_KEY` for usable rate limits and `SEC_USER_AGENT` for the SEC file.

Ticker enrichment, the screener refresh, the price refresh, and the outbound Yahoo Finance links require internet access. Browsing the existing local snapshot does not.

## Deploying with Docker

The repository ships a `Dockerfile`, a `.dockerignore`, and a `docker-compose.yml` for running the dashboard as a read-only container behind a reverse proxy (the compose file is written for Traefik at `https://soto.wiktor.io/13f`). The image is `python:3.13-slim` plus the scripts, the three static assets, the tests, and the README — no `pip` and no `npm`; `make` and Debian's `nodejs` are the only extra packages, installed so that `make signals` and `make verify-fast` also work inside the container. It runs as the non-root user `app` (uid/gid 1000), listens on `0.0.0.0:8080`, and reads everything from the `/app/data` volume. Source archives are never copied into the image: `.dockerignore` excludes `*_form13f.zip`, `data/`, `artifacts/`, bytecode caches, and `.github/`.

### What goes in `data/`

Build on a machine that has the archives, then ship the `data/` directory (about 2.1 GB, most of it `13f.sqlite`):

- `13f.sqlite` — the main database from `python3 build_database.py --force`. Required.
- `cusip_tickers.json`, `market_caps.json`, `sectors.json`, `starred_funds.json` — the build inputs. Keep them next to the database so a `make signals` or a rebuild inside the container sees the same mappings; `starred_funds.json` is bundled, the others come from `enrich_tickers.py` and `refresh_market_caps.py`.
- `prices.sqlite` — from `make signals`. Optional: without it every price field is `—`. The same refresh also leaves a `fund_signals.sqlite` the dashboard never reads; shipping it is harmless.
- `company_tickers_exchange.json` — the SEC ticker file cached by `enrich_tickers.py`. Optional.

```bash
python3 build_database.py --force          # locally, with the archives present
make signals                               # optional; needs network
make verify                                # trust mode skips the freshness check, so gate the snapshot here
rsync -av --progress data/ user@vps:/srv/13f/data/
```

### Start it

On the VPS, with the sources and `data/` side by side (the compose file bind-mounts `./data`):

```bash
chown -R 1000:1000 data                    # the container runs as uid/gid 1000
docker compose up -d --build
docker compose logs -f 13f                 # prints the public URL, http://0.0.0.0:8080/13f/
docker compose ps                          # healthy once ${BASE_PATH}/api/meta answers (start period 30 s, every 60 s)
```

The container prints the non-loopback warning because it binds `0.0.0.0` — that is expected inside a container whose only route in is the proxy — and, in trust mode, `NOTICE: trusting data/13f.sqlite without re-hashing source archives (--trust-database)`.

### Configuration

The entrypoint is `python3 server.py --host 0.0.0.0 --port 8080`; everything else comes from the environment (each variable has a matching CLI flag for running outside Docker):

- `BASE_PATH` / `--base-path` — the public URL prefix, e.g. `/13f` (default empty, i.e. served at `/`). Must start with `/`, no trailing slash. The server accepts requests both with and without the prefix, rewrites root-absolute `href`/`src` attributes in `dashboard.html` to carry it, and `dashboard.js` derives it from its own `<script src>` at load time, so `/api/...` fetches and the routes (`/13f/`, `/13f/initiations`, `/13f/movers`, `/13f/about`) follow it.
- `TRUST_DATABASE` / `--trust-database` (`1`, `true`, or `yes`) — start from an existing `data/13f.sqlite` and check only that its `schema_version` matches; skip hashing the source archives and JSON inputs. Implies `--no-build`; the container never builds in this mode. This is what the compose file uses, because the archives are not shipped.
- `ARCHIVE_DIR` — where `server.py` and `build_database.py --source-dir` look for `*_form13f.zip` (default: the application directory). Only needed when the container should verify or rebuild the database itself.

Traefik: the compose file routes ``Host(`soto.wiktor.io`) && (Path(`/13f`) || PathPrefix(`/13f/`))`` straight to port 8080 with `BASE_PATH=/13f`, so no `stripprefix` middleware is needed. The commented `stripprefix` labels also work — unprefixed paths are still served — but keep `BASE_PATH` set either way so the HTML links, the API calls, and the health check carry the right prefix. Without a proxy, publish the port instead (`ports: ["127.0.0.1:8080:8080"]`) and leave `BASE_PATH` empty.

### Refresh and rebuild inside the container

```bash
docker compose exec 13f make signals            # network; rewrites prices.sqlite atomically
docker compose exec 13f make verify-fast        # fixture-only gate, runs fine in the image
```

The server opens SQLite per request and attaches the price cache afresh each time (checking only that it has the expected `bars`/`metadata` tables), so a refreshed `prices.sqlite` is visible on the next request without a restart. A full rebuild needs the archives: mount them (`./archives:/app/archives:ro`), set `ARCHIVE_DIR=/app/archives`, drop `TRUST_DATABASE`, and run `docker compose exec 13f python3 build_database.py --force` — expect about 1 GB of RAM and roughly ten minutes; the new database is swapped in atomically and served on the next request. Rebuilding locally and `rsync`ing `13f.sqlite` again is usually simpler.

## Verification

Run the complete release gate with:

```bash
make verify
```

This compiles the Python sources, checks `dashboard.js` with Node, audits `dashboard.html` (no inline scripts, no duplicate IDs, only the allowlisted assets, every required element ID present), runs the standard-library unit and HTTP integration suite, proves that the production database is fresh and internally consistent, exercises both API endpoints with response-time budgets, and completes a direct headless-Chromium walkthrough: Top Holdings with header sorting and paging, Fresh Initiations, Top Movers at 2Q/Losers, the About page, then a 375×812 mobile layout check with a no-horizontal-overflow assertion. Its JSON report and the `dashboard-desktop.png` / `dashboard-mobile.png` screenshots are written to `artifacts/`.

For a fast deterministic check that never reads, builds, or requires the full production database or Chromium, use:

```bash
make verify-fast
```

The fast path creates a tiny temporary schema-current SQLite fixture, runs the same helper/security/schema tests, validates database invariants, and times fixture-backed API smoke requests. CI invokes this path through `python3 verify.py --ci`; the workflow is defined in `.github/workflows/verify.yml` and never rebuilds full data.

Useful focused commands are:

```bash
make test       # standard-library unittest suite
make browser    # full-data Chromium walkthrough and screenshots
python3 verify.py --skip-browser
python3 verify.py --api-budget 8
```

Set `CHROMIUM` or pass `--chromium /path/to/chromium` when the executable is not on `PATH`. Full verification does not silently rebuild a missing or stale snapshot; it reports the required `python3 build_database.py --force` command instead.

## Snapshot and disclosure policy

- CIKs are normalized to ten digits and CUSIPs to uppercase before matching.
- For each manager and report period, the latest original or `RESTATEMENT` is the base; later `NEW HOLDINGS` amendments supplement that base. Amendments and restatements replace the originals; the dashboard never double-counts a manager-quarter.
- Every effective filing part is reconciled against available SEC summary entry and value totals. A mismatch stays in the database but is downgraded to partial and excluded from the weight universe and from every comparison.
- Notice reports, combination reports, confidential omissions, summary discrepancies, and unknown amendment chains are kept but marked non-comparable, so they never feed Avg Weight, New Holders, or Weight Change.
- A holding is a non-option position; long puts and calls are tracked separately and never count.
- New Holders requires complete manager filings in both adjacent quarters. Missing or partial coverage is not comparable, not zero.
- Quarter-end snapshots are never summed across periods; repeated snapshots are not portfolio value or investment performance.

Form 13F reports gross-long covered securities and long put/call options, as of the last day of the quarter and up to 45 days late. It omits short stock, written options, cash, most bonds, and most non-US holdings, so nothing here can determine a manager's complete long/short or hedged exposure. Nothing shown is investment advice.
