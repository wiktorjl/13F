# 13F Explorer

A local, utilitarian browser for multi-quarter SEC Form 13F data. It turns every `*_form13f.zip` archive in this directory into canonical quarterly manager snapshots, then exposes Fund and Securities research workspaces.

## Run it

```bash
python3 server.py
```

The first run builds `data/13f.sqlite`; this can take a few minutes. The server rebuilds when archive contents, ticker mappings, the market-cap snapshot, the sector snapshot, the featured research set, or the schema changes.

By default it binds only to `127.0.0.1` on port `8013`. Open <http://127.0.0.1:8013>. To use it from another device on a trusted network, opt in with that machine's LAN address:

```bash
python3 server.py --host 192.168.x.x
```

Non-loopback binding prints a warning. The HTTP server exposes only the six static assets of the explorer and the dashboard plus the JSON API; source archives, scripts, caches, and the SQLite database are never served.

Use `--port` to choose another port and `--no-build` to require an already-current database. Two more flags exist for hosting the app behind a reverse proxy — `--base-path /13f` (or `BASE_PATH=/13f`) serves everything under a URL prefix, and `--trust-database` (or `TRUST_DATABASE=1`) starts from a database built elsewhere without re-hashing the source archives; see [Deploying with Docker](#deploying-with-docker). Rebuild manually after changing the archives:

```bash
python3 build_database.py --force
```

The explorer and database builder use only the Python standard library and local source archives.

## Deploying with Docker

The repository ships a `Dockerfile`, a `.dockerignore`, and a `docker-compose.yml` for running the explorer as a read-only container behind a reverse proxy (the compose file is written for Traefik at `https://soto.wiktor.io/13f`). The image is `python:3.13-slim` plus the scripts, the six static assets, the tests, and the README — no `pip` and no `npm`; `make` and Debian's `nodejs` are the only extra packages, installed so that `make signals` and `make verify-fast` also work inside the container. It runs as the non-root user `app` (uid/gid 1000), listens on `0.0.0.0:8080`, and reads everything from the `/app/data` volume. Source archives are never copied into the image: `.dockerignore` excludes `*_form13f.zip`, `data/`, `artifacts/`, bytecode caches, and `.github/`.

### What goes in `data/`

Build on a machine that has the archives, then ship the `data/` directory (about 2.1 GB, most of it `13f.sqlite`):

- `13f.sqlite` — the main database from `python3 build_database.py --force`. Required.
- `cusip_tickers.json`, `market_caps.json`, `sectors.json`, `starred_funds.json` — the build inputs. Keep them next to the database so a `make signals` or a rebuild inside the container sees the same mappings; `starred_funds.json` is bundled, the others come from `enrich_tickers.py` and `refresh_market_caps.py`.
- `prices.sqlite`, `fund_signals.sqlite` — from `make signals`. Optional: without them every price field is `—` and Signal return is unavailable.
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
docker compose logs -f 13f                 # "13F Explorer is running at http://0.0.0.0:8080/13f/"
docker compose ps                          # healthy once ${BASE_PATH}/api/meta answers (start period 30 s, every 60 s)
```

The container prints `WARNING: the explorer is being exposed beyond this computer` because it binds `0.0.0.0` — that is expected inside a container whose only route in is the proxy — and, in trust mode, `NOTICE: trusting data/13f.sqlite without re-hashing source archives (--trust-database)`.

### Configuration

The entrypoint is `python3 server.py --host 0.0.0.0 --port 8080`; everything else comes from the environment (each variable has a matching CLI flag for running outside Docker):

- `BASE_PATH` / `--base-path` — the public URL prefix, e.g. `/13f` (default empty, i.e. served at `/`). Must start with `/`, no trailing slash. The server accepts requests both with and without the prefix, rewrites root-absolute `href`/`src` attributes in the HTML documents to carry it, and the two scripts derive it from their own `<script src>` at load time, so `/api/...` fetches and dashboard routes follow it. History URLs are relative and need nothing.
- `TRUST_DATABASE` / `--trust-database` (`1`, `true`, or `yes`) — start from an existing `data/13f.sqlite` and check only that its `schema_version` matches; skip hashing the source archives and JSON inputs. Implies `--no-build`; the container never builds in this mode. This is what the compose file uses, because the archives are not shipped.
- `ARCHIVE_DIR` — where `server.py` and `build_database.py --source-dir` look for `*_form13f.zip` (default: the application directory). Only needed when the container should verify or rebuild the database itself.

Traefik: the compose file routes ``Host(`soto.wiktor.io`) && (Path(`/13f`) || PathPrefix(`/13f/`))`` straight to port 8080 with `BASE_PATH=/13f`, so no `stripprefix` middleware is needed. The commented `stripprefix` labels also work — unprefixed paths are still served — but keep `BASE_PATH` set either way so the HTML links, the API calls, and the health check carry the right prefix. Without a proxy, publish the port instead (`ports: ["127.0.0.1:8080:8080"]`) and leave `BASE_PATH` empty.

### Refresh and rebuild inside the container

```bash
docker compose exec 13f make signals            # network; rewrites prices.sqlite + fund_signals.sqlite atomically
docker compose exec 13f make verify-fast        # fixture-only gate, runs fine in the image
```

The server opens SQLite per request and attaches the sidecars by identity, so a refreshed `fund_signals.sqlite`/`prices.sqlite` is visible on the next request without a restart. A full rebuild needs the archives: mount them (`./archives:/app/archives:ro`), set `ARCHIVE_DIR=/app/archives`, drop `TRUST_DATABASE`, and run `docker compose exec 13f python3 build_database.py --force` — expect about 1 GB of RAM and roughly ten minutes; the new database is swapped in atomically and served on the next request. Rebuilding locally and `rsync`ing `13f.sqlite` again is usually simpler.

## Views and controls

- **Funds** browses filing managers and opens a manager's quarter-over-quarter position detail. The default Research universe keeps managers with at least $1 billion in latest disclosed 13F value plus all Featured managers; All filers remains one selection away. The Featured column identifies a fixed 20-manager editorial research set. Fund rows also include a sortable post-disclosure signal return described below.
- **Securities** contains three modes in one workspace: Browse ranks reported exposure by security, Quarterly changes compares securities across adjacent releases, and Positions exposes individual effective manager-quarter holdings.

Known ticker symbols are shown before issuer names; unresolved symbols appear as an em dash. Search accepts ticker, issuer, CUSIP, manager, and CIK where applicable. The shared filter form is collapsed by default; its summary always shows applied scope. Filters cover one reporting period, non-option positions, long puts, long calls, manager, issuer, CUSIP, location, and value range. Quarterly Changes has security, position-type, activity, latest-release breadth, and current market-cap filters. Every main and comparison-table column is sortable, and long results are paginated. Quarter-end periods are intentionally not summed together: repeated snapshots do not represent portfolio value or investment performance.

## Dashboard

The same server also serves a second, deliberately minimal page at <http://127.0.0.1:8013/dashboard> (the explorer's title bar links to it). It is a single centered list of securities with three views and no filters, search, charts, or fund metadata: **Top Holdings** (`/dashboard`), **Fresh Initiations** (`/dashboard/initiations`), and **Top Movers** (`/dashboard/movers`, with a Gainers | Losers switch and a 1Q–4Q timeframe). It is built from `dashboard.html`, `dashboard.js`, and `dashboard.css` and reads one endpoint, `/api/dashboard`. The list is a table with a plain-text header row (Ticker, Name, the view's metric, Price, Day, YTD, Sector) whose labels sort the list; every row links its ticker and name to Yahoo Finance.

By default the dashboard lists only securities that have a ticker. Rows whose `securities.ticker` is blank — a CUSIP that neither the exact OpenFIGI lookup nor the SEC issuer-name match resolved — are dropped in all three views before sorting, paging, and counting, so `count` reflects the filtered list (Movers evaluates the P−k side first and drops an unmapped security only afterwards). Append `?unmapped=include` to any dashboard URL to show them; they render with an em dash in the Ticker column and their SEC issuer name. The switch travels with view, sort, and page changes, is omitted from the URL at the default, and has no visible control. `/api/dashboard` takes the same `unmapped` ∈ `exclude`, `include` parameter (default `exclude`), echoes the effective value, and answers 400 to anything else.

The metrics come from per-period rollups (`dashboard_period_stats`, `security_weight_stats`) that the database build materializes from the same canonical snapshots the explorer uses. With P the latest reporting period and P−k the period k quarters earlier:

- **Weight universe.** For each period, every manager whose filing is `COMPLETE` and whose reported total value is positive. N is the size of that set.
- **Holding.** A manager holds a security in a period when it reports at least one non-option position for it, in any amount type and at any value. Long puts and calls never count as holdings.
- **Manager weight.** The summed reported value of the manager's non-option positions in the security divided by the manager's full reported 13F value for that period, options included.
- **Avg Weight** (Top Holdings) is 100 × the sum of manager weights over the whole universe ÷ N, so non-holders contribute zero. It is the equal-weighted average portfolio weight across all complete filers, not the average among holders. Rows are every security held by at least one universe manager in P, ordered by Avg Weight, then holder count, then CUSIP. The arrow compares P with P−1: ▲ more concentrated, ▼ less concentrated, — unchanged (a security absent from P−1 counts as zero there; with no P−1 at all every row is —).
- **New Holders** (Fresh Initiations) counts managers with `COMPLETE` filings in both P and P−1 that hold the security in P and did not hold it in P−1, the same rule as the explorer's `NEW`. It is "new versus the prior quarter", not "never held in any archive". Rows are securities with at least one new holder, ordered by New Holders, then Avg Weight, then CUSIP. The arrow compares the count with the same security's New Holders in P−1: ▲ more first-time holders than last quarter, ▼ fewer, — equal (a security with no prior row counts as zero there).
- **Weight Change** (Top Movers) is Avg Weight in P minus Avg Weight in P−k, in percentage points, over the union of securities with a rollup row in either period; a security missing from one side is zero there. Gainers are positive changes, largest first; Losers are negative changes, most negative first; exact zeros appear on neither side. When P−k predates the oldest archive the view is empty.

Precision: Avg Weight and Weight Change show one decimal at or above 1 and two decimals below (`2.1%`, `0.45%`, `+0.12pp`, `−1.3pp`); New Holders is an integer with thousands separators (`1,045 new`); prices show two decimals; Day Change and YTD show one decimal with an explicit sign. `/api/dashboard` rounds metrics to four decimals. Lists longer than 100 rows page with plain Previous / Next links.

Sorting: clicking a column header sorts the whole list by that column (the API sorts before paging, so `count` never changes and page 1 always holds the extremes); clicking the active header flips the direction. The active header is charcoal and underlined with a trailing `↑`/`↓`, and `aria-sort` reflects it. The metric column starts at the view's own default order (largest first; Losers most negative first), Price, Day and YTD start descending, and Ticker, Name and Sector start ascending. Text sorts are case-insensitive; rows with no ticker (only visible with `unmapped=include`), no sector, or no price (symbol missing from the cache, or no close on the mark date) sort last in both directions and then keep the view's default order, so a price sort against an absent cache is simply the default list. Switching views resets the sort; changing the Movers side or timeframe keeps it. The state travels in the URL as `?sort=<column>&direction=asc|desc` (omitted when equal to the view's defaults), and `/api/dashboard` takes the same two parameters: `sort` ∈ `metric`, `ticker`, `name`, `price`, `day`, `ytd`, `sector` (default `metric`) and `direction` ∈ `asc`, `desc` (default `desc`, or `asc` for `view=movers&side=losers`); the response echoes the effective values and anything else is a 400. This supersedes the original dashboard spec's "no sort buttons" line — the headers are still plain text links, not buttons or icons.

Names and sectors are a static ticker-keyed mapping in `data/sectors.json`, which `refresh_market_caps.py` writes next to the market-cap snapshot from the same [Nasdaq Stock Screener](https://www.nasdaq.com/market-activity/stocks/screener) download merged with the [Nasdaq ETF Screener](https://www.nasdaq.com/market-activity/etf/screener) (ETFs receive the sector `ETF`; stock names are cleaned of `Common Stock`-style suffixes). A security without a screener entry falls back to its SEC issuer name and an empty sector; a security without a ticker does the same but is hidden unless `unmapped=include` is set. Sectors are abbreviated to at most four characters (`Tech`, `Hlth`, `Fin`, `Disc`, `Stpl`, `Indu`, `Enrg`, `Util`, `RE`, `Matl`, `Tele`, `Misc`, `ETF`). Like the other JSON inputs, a changed `sectors.json` invalidates the database:

```bash
python3 refresh_market_caps.py      # writes data/market_caps.json and data/sectors.json
python3 build_database.py --force
```

Price, Day Change, and YTD come from the offline `data/prices.sqlite` cache that the fund-signal refresh maintains; the server attaches it read-only for dashboard requests only and never fetches prices itself. Price is the close on the cache's common mark date; Day Change compares it with the previous cached close; YTD compares it with the last close of the prior December. A ticker with no close on the mark date shows `—`, and when the cache is absent every price field is `—` (`price_available: false`). The price refresh covers every displayed ticker (about 1,645 display-tier symbols including ETFs, up from the roughly 850 trusted scoring symbols), fetching ETF histories through Nasdaq's ETF route, while fund-signal scoring still uses only exact and manually verified mappings. Refresh the cache with:

```bash
make signals
```

The dashboard shows quarter-end reported snapshots and their changes, lagged by the 45-day filing window. Average weights are not portfolio value or performance, weight changes are not confirmed purchases or sales, and a fresh initiation is a disclosed first-time position relative to the prior quarter, not a recommendation. Nothing on the dashboard is investment advice.

## Fund research universe

The default cutoff is **latest disclosed 13F value of at least $1 billion, or Featured**. It is a reversible browsing preset, not a claim about firm AUM, quality, or performance. Form 13F excludes many assets and liabilities, and partial/confidential filings can understate disclosed value. Exact fund-name/CIK searches search the full directory even while the preset is selected.

Some post-2023 filers still submit values in the old thousands convention even though the SEC now specifies nearest-dollar values. The signal refresh validates obvious 1,000× scale anomalies against split-adjusted security closes and normalizes them for cutoff eligibility only. The position table and Reported value column remain the canonical as-filed amounts. All filers preserves access to every manager.

## Post-disclosure fund signal

`Signal return` asks whether a manager's newly disclosed directional changes subsequently moved the right way. It is not actual fund performance or realized P&L:

- Adjacent manager filings must both be complete. Only non-option positions reported in `SH` units with exact OpenFIGI or manually verified ticker mappings are eligible.
- Previous and current units are validated onto the same split-adjusted basis. Reported 13F value is never used to determine event direction, event weight, P&L, or return.
- The reference is the first market close strictly after the effective filing date. This avoids assuming a filing submitted after the bell was public at that day's close.
- Increases/new positions are positive signals. Reductions/exits are negative signals, so their result is avoided exposure versus cash—not realized sale proceeds.
- For signed normalized units `Δu`, reference close `P0`, and the common latest close `Pt`, the proxy is `Δu × (Pt − P0)`. Fund signal return is the sum of those proxies divided by `Σ(abs(Δu) × P0)`.
- A fund is ranked only with at least 10 priced signals, at least 80% event-count price coverage, and an effective-bet count of at least 5. Otherwise its score is unavailable rather than over-precise.

Prices come from Nasdaq's [Historical Quotes interface](https://www.nasdaq.com/market-activity/quotes/historical). The cached closes are retrospectively split-adjusted; dividends, trading costs, exact transaction timing, shorts, and non-13F assets are excluded. Refresh the separate price and score caches without rebuilding the large filing database:

```bash
make signals
```

The refresh writes `data/prices.sqlite` and `data/fund_signals.sqlite` atomically. HTTP requests never fetch market data. The score sidecar is used only when its schema-version, archive, and ticker-map identities match the main database; otherwise the app shows the signal as unavailable. A schema change (a rebuild after upgrading) invalidates the sidecar even when archives and mappings are unchanged, so run `make signals` again after such a rebuild.

## Featured research set

The bundled 20-manager set is an editorial research shortcut—not a personal watchlist, performance ranking, endorsement, or personalized investment advice. It combines current manager prominence and earnings from [Institutional Investor's 2026 Rich List](https://www.institutionalinvestor.com/article/hedge-funds/rich-list-institutional-investors-25th-annual-ranking-highest-earning-hedge), scale from [With Intelligence's H1 2025 top-AUM list](https://www.withintelligence.com/insights/billion-dollar-club-h1-2025/), and widely followed 13F equity managers from [HedgeTrace's 2026 manager list](https://hedgetrace.com/learn/top-hedge-fund-managers). Only active filers represented in the local archives are selected.

The selection, source links, and methodology live in `data/starred_funds.json`. Builds match each entry by its exact normalized SEC CIK, persist `managers.starred` in SQLite, and automatically rebuild when that research-set file changes. This avoids accidentally featuring similarly named or notice-only filers.

## Quarterly-change metrics

Use the switch above Quarterly Changes to choose the measurement unit:

- **Reported value change** is the signed change in aggregate reported end-quarter position value for a security and position type among managers with complete filings in both adjacent quarters. It includes price movement.
- **Portfolio weight change** is the change in that security's share of the paired cohort's aggregate reported 13F portfolio, expressed in percentage points.
- **Change vs. prior value** is signed reported-value change divided by prior aggregate reported position value. A zero prior value is undefined and displays as `—`; a complete exit is `−100%`.

Each release compares its labeled quarter with the immediately preceding quarter. Ranking numerators and breadth counts use only positions reported with the `SH` amount type; `PRN` and other amount types remain available in Positions and detail views. Portfolio-weight denominators include every reported position for the same paired manager cohort. The multi-release column sums additive value and portfolio-weight changes. For Change vs. prior value it is `100 × sum(defined value changes) ÷ sum(defined prior values)`, a prior-value-weighted average quarterly change—not cumulative return. New/zero-base releases are excluded from those percentage sums; defined exits remain included. The trend is latest defined release minus earliest defined release and requires two defined releases. Breadth columns count paired-cohort managers whose reported units increased or decreased in the latest release; they are not confirmed buyers or sellers.

These values describe changes between reported snapshots, not confirmed purchases or sales. Market movement, stock splits, corporate actions, CUSIP changes, and reporting-manager changes can produce apparent changes.

## Market-cap snapshot

Quarterly Changes uses a separately cached USD market-cap snapshot from the [Nasdaq Stock Screener](https://www.nasdaq.com/market-activity/stocks/screener). It is current snapshot data, not historical market cap as of a 13F reporting date. Exact ticker matches receive a market cap; unresolved tickers, funds, debt, and other instruments can remain unavailable. They display as `—` and remain in unfiltered rankings, but either market-cap bound excludes unavailable values.

The bundled snapshot records its retrieval time and source. The same refresh also writes the dashboard's `data/sectors.json` sector and name mapping (`--sectors-output`, or `--skip-sectors` to leave it alone; an ETF-screener failure only warns). Refresh both and rebuild the database with:

```bash
python3 refresh_market_caps.py
python3 build_database.py --force
```

## Ticker enrichment

Database builds read ticker mappings from `data/cusip_tickers.json` when it exists. To build or resume that cache and fill blank ticker fields in the current database:

```bash
python3 enrich_tickers.py --limit 250 --update-db
```

The enrichment process prefers exact CUSIP mappings from OpenFIGI, then uses the official SEC company-ticker file only when issuer matching is uniquely resolvable. Verified exceptional mappings can live in the cache's provenance-bearing `manual_overrides` object without erasing automated lookup diagnostics. Ambiguous or missing mappings remain blank, existing database tickers are not overwritten, and progress is cached atomically so later runs resume safely. Preview candidates without network access or writes with:

```bash
python3 enrich_tickers.py --dry-run --limit 250 --update-db
```

Ticker enrichment, market-cap snapshot refreshes, and outbound SEC filing links require internet access. Browsing the existing local snapshot does not.

## Verification workflow

Run the complete release gate with:

```bash
make verify
```

This compiles the Python sources, checks `app.js` and `dashboard.js` with Node, audits both HTML documents (no inline scripts, no duplicate IDs, only the allowlisted assets), runs the standard-library unit and HTTP integration suite, proves that the production database is fresh and internally consistent, exercises every API with response-time budgets, and completes a direct headless-Chromium walkthrough. The walkthrough covers fund and security detail navigation, Quarterly Changes metrics/sorting/filtering, Positions sorting/filtering/paging, desktop/mobile layout checks, and the dashboard's three views, column sorting, paging, and 375px layout. Its JSON report and PNG screenshots are written to `artifacts/`.

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
- For each manager and report period, the latest original or `RESTATEMENT` is the base; later `NEW HOLDINGS` amendments supplement that base.
- Every effective filing part is reconciled against available SEC summary entry and value totals. A mismatch stays browseable but is downgraded to partial and excluded from complete-filing comparisons.
- Notice reports, combination reports, confidential omissions, summary discrepancies, and unknown amendment chains remain browseable but are marked non-comparable.
- Fund and security detail changes compare reported units rather than inferring trades from value. Shares, puts, calls, `SH`, and `PRN` remain separate.
- Quarterly Changes ranks `SH` amount-type rows only, with shares, puts, and calls separated; `PRN` and other amount types remain browseable in Positions and detail views.
- `NEW` and `EXITED` require complete manager filings in both adjacent quarters. Missing or partial coverage is `NOT COMPARABLE`, not zero.

Form 13F reports gross-long covered securities and long put/call options. It omits short stock and written options, so the explorer cannot determine a manager's complete long/short or hedged exposure. Nothing shown is investment advice.
