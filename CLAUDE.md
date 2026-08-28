# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

13F Dashboard: a read-only, single-page list of securities (Top Holdings / Fresh Initiations / Top Movers, plus an About page) over multi-quarter SEC Form 13F data. The `*_form13f.zip` archives in the repository root (or `ARCHIVE_DIR`) are the only source of truth; everything in `data/` is derived or cached. It can also run as a Docker container behind a reverse proxy under a URL prefix (`BASE_PATH`) from a `data/` directory built elsewhere (`TRUST_DATABASE`).

The earlier "explorer" SPA (`index.html`/`app.js`/`styles.css` and the `/api/holdings`, `/api/aggregate`, `/api/funds`, `/api/suggest`, `/api/stock-detail`, `/api/fund-detail`, `/api/net-adds` endpoints) has been removed. Its database rollups and the fund-signal sidecar remain — see "Retained but unread" below — so do not re-derive them and do not remove them casually.

**Hard constraint: Python standard library only, and no JavaScript build step or npm dependencies.** There is no `requirements.txt`, `package.json`, or `pyproject.toml`, and `verify.py` compiles every `*.py` and runs `node --check dashboard.js` directly. Do not introduce third-party imports, bundlers, or frameworks. Node is a verification tool, not a runtime dependency.

## Commands

```bash
python3 server.py                    # run (builds data/13f.sqlite on first run; binds 127.0.0.1:8013)
python3 server.py --no-build         # refuse to build; require an already-current database
python3 server.py --host 192.168.x.x --port 9000   # LAN exposure; prints a warning for non-loopback
python3 server.py --base-path /13f   # serve under a URL prefix (or BASE_PATH=/13f); log line shows http://host:port/13f/
python3 server.py --trust-database   # (or TRUST_DATABASE=1) trust data/13f.sqlite by schema_version only; implies --no-build
ARCHIVE_DIR=/mnt/zips python3 server.py            # look for *_form13f.zip elsewhere (also build_database.py --source-dir default)
docker compose config                # parse docker-compose.yml; `docker compose up -d --build` to run (see README)
python3 build_database.py --force    # rebuild the main database (minutes)
make signals                         # refresh data/prices.sqlite (the dashboard's price cache) + the unread data/fund_signals.sqlite (network)
python3 refresh_market_caps.py       # refresh data/market_caps.json + data/sectors.json (network; then rebuild --force)
python3 enrich_tickers.py --limit 250 --update-db   # OpenFIGI/SEC ticker enrichment (network)
```

Verification:

```bash
make verify-fast    # fixture-only; no production DB, no Chromium, no network. Use this by default.
make verify         # full gate: syntax + tests + production DB freshness/invariants + API budgets + Chromium
make test           # python3 -m unittest discover -v -s tests
make browser        # Chromium walkthrough only -> artifacts/
python3 verify.py --skip-browser
python3 verify.py --api-budget 8    # relax the warmed API response-time budget
```

Single test:

```bash
python3 -m unittest tests.test_http_api -v
python3 -m unittest tests.test_server_helpers.ParameterHelperTests -v
python3 -m unittest tests.test_server_helpers.FreshnessTests.test_database_freshness_checks_every_input_digest -v
```

Four suites, all fixture-backed and offline: `test_build_helpers` (parsers, `event_order`/`effective_chain`/`canonical_groups`, snapshot loaders, the validator), `test_server_helpers` (parameter coercion/rejection, dashboard ORDER BY composition, static path mapping and `BASE_PATH` handling, `render_document`, freshness digests, price-cache attachment), `test_signal_helpers` (close parsing, split factors, market-date rollover, ticker trust), `test_http_api` (end-to-end HTTP against a temporary fixture server: static allowlist and 404s, security headers, base-path routing, `/api/meta`, `/api/dashboard`, `/about`).

CI runs `python3 verify.py --ci` (alias for `--fast`) and never builds production data. `make verify` does **not** silently rebuild a stale database — it fails and tells you to run `build_database.py --force`.

Environment variables (all optional). Server/builder: `BASE_PATH` (default for `--base-path`), `TRUST_DATABASE` (`1`/`true`/`yes`; default for `--trust-database`), `ARCHIVE_DIR` (archive directory for `server.database_is_current()` and the `build_database.py --source-dir` default; defaults to `ROOT`). Offline scripts: `CHROMIUM` (walkthrough executable, or `--chromium`), `OPENFIGI_API_KEY` (raises OpenFIGI limits from 25 req/60s and 5 jobs to 25 req/6s and 100 jobs — enrichment is impractically slow without it), `SEC_USER_AGENT` (required politeness header for the SEC company-ticker file).

## Architecture

Four stages, each with its own cache and its own freshness identity:

1. **`build_database.py`** — reads `SUBMISSION/COVERPAGE/SUMMARYPAGE/INFOTABLE.tsv` out of the zips and writes `data/13f.sqlite`. Also folds in four JSON inputs: `data/cusip_tickers.json` (tickers), `data/market_caps.json`, `data/sectors.json` (ticker → sector/name for the dashboard), `data/starred_funds.json` (a fixed 20-manager set persisted as `managers.starred`). Finishes by running `materialize_dashboard_stats()`, which fills the dashboard rollups (`dashboard_period_stats`, `security_weight_stats`) from the canonical snapshots.
2. **`enrich_tickers.py` / `refresh_market_caps.py`** — network-fetching cache builders that write those JSON inputs (`refresh_market_caps.py` writes both `market_caps.json` and `sectors.json` from the Nasdaq stock + ETF screeners; `--skip-sectors` / `--sectors-output`). Never called from the server.
3. **`refresh_fund_signals.py`** — reads the main DB, fetches Nasdaq closes into `data/prices.sqlite` (stocks route first, ETF route on a non-success status; symbols = trusted SH tickers ∪ every non-blank `securities.ticker`, ~1,645 display-tier symbols incl. ETFs; `display_symbol_count` in the price metadata), then writes the `data/fund_signals.sqlite` sidecar. The dashboard needs only `prices.sqlite`; the sidecar is a by-product.
4. **`server.py` + `dashboard.html`/`dashboard.js`/`dashboard.css`** — read-only stdlib HTTP server and one vanilla-JS page.

`verify.py` sits across all of it and is the release gate; `tests/support.py` builds the tiny schema-current fixture that the fast path and HTTP tests share.

### Retained but unread

`build_database.py` and `tests/support.py` are unchanged from the explorer era on purpose: `SCHEMA_VERSION` stays `"9"` and the build still materializes `manager_period_stats`, `security_period_stats`, `stock_changes`, `period_change_totals`, `managers.starred`, and the `starred_funds.json` CIK check even though no endpoint reads them any more (`period_stats` is still read by `/api/meta`). Dropping them is a schema bump plus a ten-minute production rebuild and was deliberately left out of scope; if you do it, bump `SCHEMA_VERSION` in both files and update `validate_database()`; `data/prices.sqlite` is unaffected because `price_cache_is_usable()` checks table structure, not the main DB's identity. Likewise `refresh_fund_signals.py` still scores and writes `data/fund_signals.sqlite`; the server only needs the `data/prices.sqlite` it maintains alongside.

### Freshness is content-hash identity, not timestamps

`server.database_is_current()` compares DB `metadata` rows against live SHA-256 hashes of every archive plus the four JSON inputs, and against `SCHEMA_VERSION`. Any change to an archive, `cusip_tickers.json`, `market_caps.json`, `sectors.json`, or `starred_funds.json` invalidates the database and triggers a full rebuild on next `server.py` start.

Trust mode is the one deliberate exception: `--trust-database` / `TRUST_DATABASE` makes `database_is_current(trust=True)` check only that the file exists and `metadata.schema_version == SCHEMA_VERSION` — no archive hashing, no JSON digests — prints `NOTICE: trusting data/13f.sqlite without re-hashing source archives (--trust-database)` once at startup, and implies `--no-build` (never auto-build in trust mode). It exists for containers that ship `data/` without the archives; `verify.py` always calls the strict default, so gate a snapshot with `make verify` before shipping it.

`SCHEMA_VERSION` is duplicated in **both** `build_database.py` and `server.py` (currently `"9"`). Changing the schema means bumping both, or the server will reject every build it makes.

The price cache (`data/prices.sqlite`) is attached at request time, and only for `/api/dashboard`: `attach_price_cache()` runs in `handle_api` before the request's `BEGIN` (the `EMPTY_PRICE_SCHEMA` stand-in needs `query_only` lifted and `executescript` ends a transaction) and ATTACHes the cache read-only as `prices` when it is usable, otherwise an in-memory stand-in with `available='0'`. The dashboard then reports `price_available: false` and null price fields instead of erroring. `/api/meta` connections never see `prices.*`. The server never opens `data/fund_signals.sqlite` at all (the explorer-era `attach_signal_snapshot()` is gone); `/api/meta` is the `metadata` table (`built_at`, hashes, `schema_version`, …) plus the period list and latest-quarter `period_stats` counts.

### Build invariants

- One archive = one primary reporting period; two archives resolving to the same period is a fatal error.
- Canonical snapshot per (manager, period): latest `BASE`/`RESTATEMENT` resets the part list, later `NEW HOLDINGS` amendments append, `UNKNOWN AMENDMENT` resets and marks the chain non-comparable (`effective_chain()`).
- Coverage is `COMPLETE` / `PARTIAL` / `INFERRED` / `NOTICE`. Only `COMPLETE` filers enter the weight universe and only `COMPLETE` pairs feed adjacent-quarter comparisons; missing coverage is not comparable, never zero.
- Every effective part is reconciled against SEC summary entry/value totals into `filing_part_stats`; a mismatch downgrades to partial.
- Every starred CIK in `starred_funds.json` must exist in the archives, or the build fails.
- Builds are guarded by `data/.13f.sqlite.build.lock`, write to a temp file, and `atomic_replace()` into place. Inputs are re-hashed after the read pass (`assert_inputs_unchanged`) to catch archives mutated mid-build.
- `build_database.validate_database()` is the **single definition of database invariants**, and `verify.py` imports and re-runs it against the production DB rather than restating the rules. New invariants belong there, not in the verifier — that way the builder refuses to swap in a bad database and the release gate catches drift in an existing one. It also recomputes the dashboard rollups for the latest period (weight universe size, `avg_weight`, `new_holder_count`) and must stay pure `SELECT` (it runs under `PRAGMA query_only`).
- `materialize_dashboard_stats(con)` is the single implementation of the dashboard rollups, shared by the builder, the fixture (`tests/support.py`), and the validator; the SQL lives in the `DASHBOARD_*_SQL` module constants. Never hand-enter `security_weight_stats` rows.
- `refresh_fund_signals.py` takes its own `data/fund_signals.sqlite.lock`, requires the main DB to already exist, and also writes atomically. `--workers` is bounded 1–32 (default 12).

### Encodings used everywhere

- `positions.position_type`: `0` shares/other, `1` long put, `2` long call. A dashboard "holding" is any `position_type = 0` row.
- `positions.shares_type`: `0` `SH`, `1` `PRN`, `2` other. The unread `stock_changes` rollup uses `shares_type = 0` only; the dashboard counts every amount type.
- CIKs normalized to 10 digits, CUSIPs uppercased, before any matching.
- Precomputed rollups exist so the API stays inside its response-time budget — extend these in the builder rather than adding expensive per-request aggregation. Read today: `dashboard_period_stats`, `security_weight_stats` (dashboard) and `period_stats` (meta). Retained, unread: `manager_period_stats`, `security_period_stats`, `stock_changes`, `period_change_totals`.
- Dashboard metrics: `avg_weight` is percent, equal-weighted over every `COMPLETE` manager with `total_value > 0` (non-holders count as zero); `new_holder_count` requires `COMPLETE` on both sides of the adjacent pair. Weights are precomputed per period; the movers view diffs two periods at request time.

### Ticker mappings have three trust tiers

`data/cusip_tickers.json` is not a flat map:

- `records[cusip].source == "openfigi"` — exact CUSIP mapping. Trusted everywhere.
- `records[cusip].source == "sec_name"` — resolved by uniquely matching the issuer name against the SEC company-ticker file. Good enough to **display**, and `build_database.py` writes it into `securities.ticker`, but `refresh_fund_signals.trusted_ticker_map()` deliberately ignores it for scoring.
- `manual_overrides[cusip]` — hand-verified, carries `evidence`/`note` provenance. Trusted everywhere, and kept separate so it never erases automated lookup diagnostics.

Consequence: the dashboard's display tier is every non-blank `securities.ticker`, and that is also the set `make signals` prices, so a `sec_name` ticker shows and gets a price; the trusted tier only matters for the unread signal scoring. A blank ticker hides the row unless `?unmapped=include`. To correct a specific bad mapping, add a provenance-bearing entry to `manual_overrides` — do not edit `records` in place. `enrich_tickers.py` never overwrites a non-blank `securities.ticker`, and ambiguous mappings are left blank rather than guessed.

### Server request contract

`server.py` is deliberately hostile by default:

- `API_PARAMETERS` is a per-endpoint allowlist with exactly two entries; `reject_unknown_params()` 400s on anything unexpected. Adding a query parameter means adding it there.
- Endpoints: `/api/meta` (no parameters; a flat object with at least `periods`, `latest_period`, `period_count`, `holding_count`, `total_value`, `distinct_managers`, `distinct_issuers`, `built_at` — the Docker healthcheck probes it and the About page reads it) and `/api/dashboard` (`view` ∈ holdings/initiations/movers, `horizon` 1–4, `side` gainers/losers, `sort` ∈ metric/ticker/name/price/day/ytd/sector default `metric`, `direction` ∈ asc/desc default `desc` except `asc` for movers losers, `page`, `size` default 100, `unmapped` ∈ exclude/include default `exclude`; `horizon`/`side` are validated for every view but only steer movers). `unmapped=exclude` adds `AND s.ticker!=''` to every view's `ranked` CTE — for movers after the union, so a security present only in P−k is still evaluated and dropped only when unmapped — before sorting and paging, so `count` reflects the filter; `include` is the pre-filter behaviour; the response echoes `unmapped`. `dashboard_order()` composes the ORDER BY only from the fixed `DASHBOARD_SORT_SQL`/`DASHBOARD_TIE_BREAKERS`/`SORT_DIRECTIONS` strings — never interpolate request text. `sort=metric` at the default direction is the view's historical order byte-for-byte; every other key runs `NULLS LAST` then that default order; price/day/ytd keys are computed in a `priced` CTE (three correlated `prices.bars` lookups per row, mark/previous/prior-December closes with `price_fields()` semantics) only when a price sort is requested and a mark date exists, otherwise a price sort is the default order. Sorting happens before `LIMIT`, `count` is unaffected, and the response echoes the effective `sort`/`direction`.
- `STATIC_PATHS` is exactly `{dashboard.html, dashboard.js, dashboard.css}` and `HTML_DOCUMENTS` is `{dashboard.html}`. Route map: `static_path_for()` sends the four `DASHBOARD_ROUTES` (`/`, `/initiations`, `/movers`, `/about`) and the three legacy `DASHBOARD_ALIASES` (`/dashboard`, `/dashboard/initiations`, `/dashboard/movers`, kept so old links work) → `dashboard.html`; `/dashboard.html`, `/dashboard.js`, `/dashboard.css` are served by exact name; everything else 404s — trailing slashes (`/initiations/`, `/movers/`, `/about/`, `/dashboard/`), case variants, and the removed explorer paths (`/explorer`, `/index.html`, `/app.js`, `/styles.css`). All of it works under `BASE_PATH` because `request_path()` strips the prefix first (`/13f` and `/13f/` are the root). Archives, scripts, `data/`, and caches are never served.
- Path prefix: `BASE_PATH` (module global; `normalize_base_path()` accepts only `^/[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$`, maps `""`/`/` to `""`, strips one trailing slash, `parser.error` otherwise). `request_path()` is pure: `BASE_PATH` or `BASE_PATH + "/"` → `/`, `BASE_PATH + "/x"` → `/x`, anything else unchanged (so unprefixed paths — a Traefik `stripprefix` deployment — keep working and `/13f-other`/`/13fx/dashboard.js` fall through to the allowlist and 404). `do_GET`/`do_HEAD` apply it before the `/api/` test and the static mapping; the length/parse checks run first, unchanged. Identity when `BASE_PATH` is empty.
- HTML is served by the handler, not `SimpleHTTPRequestHandler`: `render_document("dashboard.html")` reads the file and, only when `BASE_PATH` is set, rewrites `href="/…"`/`src="/…"` to `href="{BASE_PATH}/…"` (never `//…`, `data:`, relative, or already-prefixed values). That is why `dashboard.html` references its assets root-absolute (`/dashboard.css`, `/dashboard.js`) and why its links are root-absolute (`/`, `/initiations`, `/movers?…`, `/about`, `/?sort=…`) — one rewrite rule covers everything (`href="/"` becomes `href="/13f/"`). JS/CSS still go through `SimpleHTTPRequestHandler`. Every non-API response carries `Cache-Control: no-cache` (a stale stylesheet bit us once); API responses stay `no-store`. HEAD sends headers only.
- Startup prints the public prefix URL (`http://host:port{BASE_PATH}/`).
- Strict CSP with no `unsafe-inline`, plus nosniff/DENY/no-referrer headers on every response. `verify.py` fails the build if `dashboard.html` gains an inline `<script>`, references any asset other than `/dashboard.js`/`/dashboard.css` (root-absolute so `/initiations`, `/movers`, `/about` resolve them and `render_document()` can prefix them; the audit tolerates the single leading slash), has duplicate element IDs, or lacks any of the `DASHBOARD_IDS`.
- Read-only SQLite (`mode=ro`), bounded query/parameter lengths, page caps, and a `BoundedSemaphore(4)` on API work.
- Requests never perform network I/O. All market data arrives through the offline refresh scripts.

### Frontend

`dashboard.js` is one plain script for `dashboard.html` — no modules, no framework. Its first line derives `BASE_PATH` from `document.currentScript.src` (the directory of the script URL, `''` at the root); `VIEW_PATHS`, `routeUrl()`, and `api()` prefix with it and `routeFromUrl()` strips it from `url.pathname` (via `unprefixedPath()`, where the bare prefix is the root) before matching. `VIEW_ROUTES` is `''` (root) / `/initiations` / `/movers` / `/about`, so `VIEW_PATHS` is `BASE_PATH + '/'` for holdings (`'/'` unprefixed, `'/13f/'` under a prefix) and `BASE_PATH + '/initiations'` / `'/movers'` / `'/about'`; `LEGACY_ROUTE_VIEWS` maps `/dashboard`, `/dashboard/initiations`, `/dashboard/movers` to the same views for old links, `init()` rewrites such an entry URL in place with `history.replaceState` (query kept), and `pushState`/hrefs only ever emit the canonical paths. Only the root tolerates a trailing slash (`/13f` and `/13f/` are both holdings); `/initiations/` or any unknown path falls back to holdings client-side (the server 404s it anyway). `VIEW_TITLES` sets the document title per view (`13F Dashboard — About` for the About page).

`state` holds view/horizon/side/sort/direction/page/`unmapped` (`'exclude'` default; `?unmapped=include` is parsed by `routeFromUrl`, kept by `routeUrl` across view/sort/page changes, sent to the API only when `include`, and has no visible control — the `dash-missing` blank-ticker rendering path exists for that mode); the three list views live at `/`, `/initiations`, `/movers`, with movers and sort state in the query string (`?horizon=2&side=losers&sort=ticker&direction=asc&page=3`, defaults omitted — `direction` is omitted when it equals the view default, `desc` or `asc` for movers losers) and `popstate` handled. `routeFromUrl` normalises unknown `sort`/`direction` values to the defaults, so a request with invalid values is never sent; requests carry `sort` only when non-metric and `direction` whenever the sort is non-metric or the direction differs from the view default, so default views still send exactly `view`/`horizon`/`side`/`page`/`size`. Nav/toggle/pager/header clicks (plain left-click only) route in-page via `history.pushState`; hrefs stay real URLs and always carry the full route.

The About view (`/about`, canonical, no query state — `?…` is ignored) is static markup in `dashboard.html`: `<section id="dashAbout" class="dash-about" hidden>` after `#dashPager` inside `#dashMain`. When it is active `#dashControls`, `#dashTable`, `#dashStatus`, `#dashPager` are hidden and `#dashAbout` shown; no `/api/dashboard` call is made, but `/api/meta` is fetched once (cached in `state`) to fill `#aboutQuarters` (period count), `#aboutSpan` (`31 Mar 2023 to 31 Mar 2026`, period labels reformatted), and `#aboutManagers` (latest-quarter `distinct_managers` rounded to the nearest hundred with thousands separators, "about 8,700"); the spans show `—` before meta arrives or on failure. Leaving the view restores the table and re-fetches rows as for any view change. Its copy carries the disclosure language and is part of the contract — edit it deliberately.

The list is an ARIA table of divs, one DOM for both layouts: `div#dashTable[role=table]` > `div#dashHead[role=row]` + `div#dashRows[role=rowgroup]` (`tabIndex=-1`, focused on page change). `#dashHead` holds `span.dash-sort-caption[aria-hidden]` ("Sort", `display: none` ≥ 640px, a flex item on phones) followed by the eight `span.dash-cell[role=columnheader]` cells in **row-cell order** — ticker, direction (empty, `aria-label="Direction"`), metric, name, sector, price, day, ytd — so assistive tech pairs each header with its cell; the seven sortable ones are `[data-sort][aria-sort]` > `a.dash-sort(.active)` (+ `span.dash-sort-glyph` `↑`/`↓`). `renderHead()` sets the metric label per view (Avg Weight / New Holders / Weight Change), the active class, `aria-sort`, and each header's href (`sortRoute()`: the active column flips, another column starts at `SORT_DEFAULTS` — asc for ticker/name/sector, desc for price/day/ytd, the view default for metric — and resets `page`), then `revealActiveSort()` scrolls the active cell into `#dashHead`'s side padding with `head.scrollLeft` only (never the document; a no-op on desktop where the header does not scroll). Switching views resets the sort; `moversRoute()` keeps it across side/horizon changes. Rows render into `#dashRows` via `innerHTML` (`rowHtml()`: `div.dash-row[role=row][data-cusip]` > three `div.dash-line.dash-line-1/2/3[role=presentation]` > `span.dash-cell[role=cell].dash-*` — line 1 ticker/direction/metric, line 2 name/sector, line 3 price/day/ytd; the "—" price/day/ytd cells also carry `unpriced`), so every value goes through its own `esc()`; glyphs, classes, and sector abbreviations come from constant maps keyed by validated values. Layout (`dashboard.css`): on desktop `.dash-table` is a grid (`auto auto minmax(0,1fr) auto auto auto auto auto`, 24px gaps), `#dashRows`, `.dash-head` and `.dash-row` are column subgrids, `.dash-line` is `display: contents`, and every cell is placed by an explicit `grid-column` (direction 1, ticker 2, name 3, metric 4, price 5, day 6, ytd 7, sector 8) plus `grid-row: 1`, so DOM order never moves anything; the `@supports not (subgrid)` fallback uses fixed tracks (`14px 62px minmax(0,1fr) 96px 62px 40px 48px 40px`, 16px gaps) per row. Under 640px the header (logo line + tab row) is `position: sticky`, `#dashHead` is one 44px flex line that scrolls sideways (hidden scrollbar; the caption first, the empty direction header pulled back by one gap), rows are blocks whose lines are flex rows, the ticker/name cells do not clip (their `<a>` carries the ellipsis and the 44px hit box), the flat glyph is `color: transparent` (kept for its "Unchanged" label and the 8px rhythm), `.dash-price.unpriced` is taupe like the neutral dashes, and `.dash-day/.dash-ytd:not(.unpriced)::after` add the " today" / " YTD" labels. Formatting rules (1 decimal ≥ 1 else 2, U+2212 minus, `pp` suffix, thousands separators, sector abbreviation map, title-casing of all-caps SEC issuer names) are in the README's Dashboard section and are part of the contract. The IDs in `dashboard.html` (`dashLogo`, `dashNav`, `dashMain`, `dashControls`, `dashSide`, `dashHorizon`, `dashTable`, `dashHead`, `dashRows`, `dashStatus`, `dashPager`, `dashPrev`, `dashNext`, `dashAbout`, `aboutQuarters`, `aboutSpan`, `aboutManagers`) are asserted by `verify.py` (`DASHBOARD_IDS`) and the walkthrough. Walkthrough selectors must stay scoped to `#dashRows .dash-*` / `#dashHead [data-sort=…]` — a bare `.dash-direction` matches the header cell first.

`tests/chromium_walkthrough.py` drives real Chromium over the DevTools protocol with a hand-rolled stdlib WebSocket client. Its entry URL is `app_url` (the server root with a trailing slash). Desktop: holdings header sorting via `dashboard_sorting()` (Ticker asc, flip to desc, metric back to the default URL, all asserting `location.pathname === '/'`), then paging, initiations at `/initiations`, movers at `/movers?horizon=2&side=losers`, About (click `#dashNav a[data-view="about"]` → `#dashAbout` visible, `#dashTable` hidden, `location.pathname === '/about'`, `#aboutQuarters` is a number, then Top Holdings brings the table back), `dashboard-desktop.png`; then a 375×812 `dashboard_viewport_check` (`dashboard-mobile.png`, no horizontal overflow). It keeps `run_walkthrough()`'s signature and the JSON failure report. UI changes that move or rename IDs, columns, nav labels, or `data-*` attributes will break it; update the walkthrough alongside.

### Docker

`Dockerfile` (`python:3.13-slim`, no pip; apt adds only `make` and `nodejs` so `make signals`/`make verify-fast` work inside the container), `.dockerignore` (`*_form13f.zip`, `data/`, `artifacts/`, bytecode, `.github/`, `.git/`), and `docker-compose.yml` (Traefik labels for `soto.wiktor.io/13f`, `BASE_PATH=/13f`, `TRUST_DATABASE=1`, `./data` bind mount, `user: 1000:1000`; the image tag is still `13f-explorer:latest`). The image copies the scripts, `Makefile`, the three dashboard assets, `README.md`, and `tests/`; runs as `app` (uid/gid 1000); declares `VOLUME /app/data` and `EXPOSE 8080`; health-checks `http://127.0.0.1:8080${BASE_PATH}/api/meta` with `urllib`; and its entrypoint is `python3 server.py --host 0.0.0.0 --port 8080` with flags driven by `BASE_PATH`/`TRUST_DATABASE`/`ARCHIVE_DIR`. No stripprefix is needed because the server owns the prefix; the commented labels work too since unprefixed paths are still accepted. The non-loopback warning is expected in the container. `docker compose exec 13f make verify-fast` runs the fixture gate in the image; `docker compose exec 13f make signals` refreshes the price cache into the volume. Keep the Dockerfile's `COPY` list in sync when adding a script or asset; never copy archives or `data/` into the image.

## Domain rules that the code enforces on purpose

These are correctness requirements, not cosmetic copy — the README and the About page document them and the tests check them:

- Quarter-end snapshots are never summed across periods; they are not portfolio value or performance.
- "Change" means change between reported snapshots, not confirmed buys or sells; price moves, splits, and reporting-entity changes all move a weight.
- `Avg Weight` is an equal-weighted average across all complete filers (not among holders), `New Holders` is new-versus-prior-quarter and requires complete filings on both sides of the pair, and `Weight Change` is a difference of two quarter-end averages. None of them is performance, a buy/sell signal, or a recommendation.
- Only filings that reconcile with the SEC summary totals enter the weight universe; amendments and restatements replace originals.
- Nothing in the app is investment advice; keep the disclosure language in the README (Dashboard and Snapshot and disclosure policy sections) and in the About copy in `dashboard.html` intact when touching those areas. The list views deliberately carry no explanatory text — the About page and the README are where the caveats live.
