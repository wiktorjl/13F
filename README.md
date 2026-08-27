# 13F Explorer

A local, utilitarian browser for multi-quarter SEC Form 13F data. It turns every `*_form13f.zip` archive in this directory into canonical quarterly manager snapshots, then exposes Fund and Securities research workspaces.

## Run it

```bash
python3 server.py
```

The first run builds `data/13f.sqlite`; this can take a few minutes. The server rebuilds when archive contents, ticker mappings, the market-cap snapshot, the featured research set, or the schema changes.

By default it binds only to `127.0.0.1` on port `8013`. Open <http://127.0.0.1:8013>. To use it from another device on a trusted network, opt in with that machine's LAN address:

```bash
python3 server.py --host 192.168.x.x
```

Non-loopback binding prints a warning. The HTTP server exposes only the app's three static assets and JSON API; source archives, scripts, caches, and the SQLite database are never served.

Use `--port` to choose another port, `--no-build` to require an already-current database, or rebuild manually after changing the archives:

```bash
python3 build_database.py --force
```

The explorer and database builder use only the Python standard library and local source archives.

## Views and controls

- **Funds** browses filing managers and opens a manager's quarter-over-quarter position detail. The default Research universe keeps managers with at least $1 billion in latest disclosed 13F value plus all Featured managers; All filers remains one selection away. The Featured column identifies a fixed 20-manager editorial research set. Fund rows also include a sortable post-disclosure signal return described below.
- **Securities** contains three modes in one workspace: Browse ranks reported exposure by security, Quarterly changes compares securities across adjacent releases, and Positions exposes individual effective manager-quarter holdings.

Known ticker symbols are shown before issuer names; unresolved symbols appear as an em dash. Search accepts ticker, issuer, CUSIP, manager, and CIK where applicable. The shared filter form is collapsed by default; its summary always shows applied scope. Filters cover one reporting period, non-option positions, long puts, long calls, manager, issuer, CUSIP, location, and value range. Quarterly Changes has security, position-type, activity, latest-release breadth, and current market-cap filters. Every main and comparison-table column is sortable, and long results are paginated. Quarter-end periods are intentionally not summed together: repeated snapshots do not represent portfolio value or investment performance.

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

The refresh writes `data/prices.sqlite` and `data/fund_signals.sqlite` atomically. HTTP requests never fetch market data. The score sidecar is used only when its archive and ticker-map identities match the main database; otherwise the app shows the signal as unavailable.

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

The bundled snapshot records its retrieval time and source. Refresh it and rebuild the database with:

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

This compiles the Python sources, checks `app.js` with Node, runs the standard-library unit and HTTP integration suite, proves that the production database is fresh and internally consistent, exercises every API with response-time budgets, and completes a direct headless-Chromium walkthrough. The walkthrough covers fund and security detail navigation, Quarterly Changes metrics/sorting/filtering, Positions sorting/filtering/paging, and desktop/mobile layout checks. Its JSON report and PNG screenshots are written to `artifacts/`.

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
