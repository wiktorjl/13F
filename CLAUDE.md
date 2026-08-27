# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

13F Explorer: a local, single-user browser for multi-quarter SEC Form 13F data. The `*_form13f.zip` archives in the repository root (or `ARCHIVE_DIR`) are the only source of truth; everything in `data/` is derived or cached. It can also run as a Docker container behind a reverse proxy under a URL prefix (`BASE_PATH`) from a `data/` directory built elsewhere (`TRUST_DATABASE`).

**Hard constraint: Python standard library only, and no JavaScript build step or npm dependencies.** There is no `requirements.txt`, `package.json`, or `pyproject.toml`, and `verify.py` compiles every `*.py` and runs `node --check app.js` directly. Do not introduce third-party imports, bundlers, or frameworks. Node is a verification tool, not a runtime dependency.

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
make signals                         # refresh data/prices.sqlite + data/fund_signals.sqlite (network)
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

Four suites, all fixture-backed and offline: `test_build_helpers` (parsers, `event_order`/`effective_chain`/`canonical_groups`, snapshot loaders, the validator), `test_server_helpers` (parameter coercion/rejection, LIKE escaping, period defaults, freshness digests), `test_signal_helpers` (close parsing, split factors, market-date rollover, ticker trust), `test_http_api` (end-to-end HTTP against a temporary fixture server).

CI runs `python3 verify.py --ci` (alias for `--fast`) and never builds production data. `make verify` does **not** silently rebuild a stale database — it fails and tells you to run `build_database.py --force`.

Environment variables (all optional). Server/builder: `BASE_PATH` (default for `--base-path`), `TRUST_DATABASE` (`1`/`true`/`yes`; default for `--trust-database`), `ARCHIVE_DIR` (archive directory for `server.database_is_current()` and the `build_database.py --source-dir` default; defaults to `ROOT`). Offline scripts: `CHROMIUM` (walkthrough executable, or `--chromium`), `OPENFIGI_API_KEY` (raises OpenFIGI limits from 25 req/60s and 5 jobs to 25 req/6s and 100 jobs — enrichment is impractically slow without it), `SEC_USER_AGENT` (required politeness header for the SEC company-ticker file).

## Architecture

Four stages, each with its own cache and its own freshness identity:

1. **`build_database.py`** — reads `SUBMISSION/COVERPAGE/SUMMARYPAGE/INFOTABLE.tsv` out of the zips and writes `data/13f.sqlite`. Also folds in four JSON inputs: `data/cusip_tickers.json` (tickers), `data/market_caps.json`, `data/sectors.json` (ticker → sector/name for the dashboard), `data/starred_funds.json` (the fixed 20-manager Featured set). Finishes by running `materialize_dashboard_stats()`, which fills the dashboard rollups (`dashboard_period_stats`, `security_weight_stats`) from the canonical snapshots.
2. **`enrich_tickers.py` / `refresh_market_caps.py`** — network-fetching cache builders that write those JSON inputs (`refresh_market_caps.py` writes both `market_caps.json` and `sectors.json` from the Nasdaq stock + ETF screeners; `--skip-sectors` / `--sectors-output`). Never called from the server.
3. **`refresh_fund_signals.py`** — reads the main DB, fetches Nasdaq closes into `data/prices.sqlite` (stocks route first, ETF route on a non-success status; symbols = trusted SH tickers ∪ every non-blank `securities.ticker`, ~1,645 display-tier symbols incl. ETFs vs ~850 scoring symbols; `display_symbol_count` in the price metadata), writes the `data/fund_signals.sqlite` sidecar (post-disclosure signal scores + `scope_values`).
4. **`server.py` + `index.html`/`app.js`/`styles.css`** (explorer SPA) **+ `dashboard.html`/`dashboard.js`/`dashboard.css`** (dashboard page) — read-only stdlib HTTP server and two vanilla-JS front ends.

`verify.py` sits across all of it and is the release gate; `tests/support.py` builds the tiny schema-current fixture that the fast path and HTTP tests share.

### Freshness is content-hash identity, not timestamps

`server.database_is_current()` compares DB `metadata` rows against live SHA-256 hashes of every archive plus the four JSON inputs, and against `SCHEMA_VERSION`. Any change to an archive, `cusip_tickers.json`, `market_caps.json`, `sectors.json`, or `starred_funds.json` invalidates the database and triggers a full rebuild on next `server.py` start.

Trust mode is the one deliberate exception: `--trust-database` / `TRUST_DATABASE` makes `database_is_current(trust=True)` check only that the file exists and `metadata.schema_version == SCHEMA_VERSION` — no archive hashing, no JSON digests — prints `NOTICE: trusting data/13f.sqlite without re-hashing source archives (--trust-database)` once at startup, and implies `--no-build` (never auto-build in trust mode). It exists for containers that ship `data/` without the archives; `verify.py` always calls the strict default, so gate a snapshot with `make verify` before shipping it.

`SCHEMA_VERSION` is duplicated in **both** `build_database.py` and `server.py` (currently `"9"`). Changing the schema means bumping both, or the server will reject every build it makes. A bump also invalidates `data/fund_signals.sqlite` (its `main_schema_version` no longer matches), so `make signals` must be re-run after the rebuild or every Signal return shows as unavailable.

The signals sidecar is attached at request time by `attach_signal_snapshot()` only when its stored `main_schema_version` / `main_source_archive_hashes` / `main_ticker_map_sha256` match the main DB. On mismatch it attaches an in-memory `EMPTY_SIGNAL_SCHEMA` instead, so signal columns degrade to "unavailable" rather than erroring. Any query touching `signals.*` must keep working against that empty schema.

The price cache (`data/prices.sqlite`) is attached the same way, but only for `/api/dashboard`: `attach_price_cache()` runs in `handle_api` before the request's `BEGIN` (the `EMPTY_PRICE_SCHEMA` stand-in needs `query_only` lifted and `executescript` ends a transaction) and ATTACHes the cache read-only as `prices` when it is usable, otherwise an in-memory stand-in with `available='0'`. The dashboard then reports `price_available: false` and null price fields instead of erroring. Explorer connections never see `prices.*`.

### Build invariants

- One archive = one primary reporting period; two archives resolving to the same period is a fatal error.
- Canonical snapshot per (manager, period): latest `BASE`/`RESTATEMENT` resets the part list, later `NEW HOLDINGS` amendments append, `UNKNOWN AMENDMENT` resets and marks the chain non-comparable (`effective_chain()`).
- Coverage is `COMPLETE` / `PARTIAL` / `INFERRED` / `NOTICE`. Only `COMPLETE` pairs feed adjacent-quarter comparisons; missing coverage is `NOT COMPARABLE`, never zero.
- Every effective part is reconciled against SEC summary entry/value totals into `filing_part_stats`; a mismatch downgrades to partial but stays browseable.
- Every starred CIK in `starred_funds.json` must exist in the archives, or the build fails.
- Builds are guarded by `data/.13f.sqlite.build.lock`, write to a temp file, and `atomic_replace()` into place. Inputs are re-hashed after the read pass (`assert_inputs_unchanged`) to catch archives mutated mid-build.
- `build_database.validate_database()` is the **single definition of database invariants**, and `verify.py` imports and re-runs it against the production DB rather than restating the rules. New invariants belong there, not in the verifier — that way the builder refuses to swap in a bad database and the release gate catches drift in an existing one. It also recomputes the dashboard rollups for the latest period (weight universe size, `avg_weight`, `new_holder_count`) and must stay pure `SELECT` (it runs under `PRAGMA query_only`).
- `materialize_dashboard_stats(con)` is the single implementation of the dashboard rollups, shared by the builder, the fixture (`tests/support.py`), and the validator; the SQL lives in the `DASHBOARD_*_SQL` module constants. Never hand-enter `security_weight_stats` rows.
- `refresh_fund_signals.py` takes its own `data/fund_signals.sqlite.lock`, requires the main DB to already exist, and also writes atomically. `--workers` is bounded 1–32 (default 12).

### Encodings used everywhere

- `positions.position_type`: `0` shares/other, `1` long put, `2` long call.
- `positions.shares_type`: `0` `SH`, `1` `PRN`, `2` other.
- Quarterly-change rankings (`stock_changes`) use `shares_type = 0` only; `PRN` stays visible in Positions and detail views.
- CIKs normalized to 10 digits, CUSIPs uppercased, before any matching.
- Precomputed rollups (`manager_period_stats`, `security_period_stats`, `period_stats`, `stock_changes`, `period_change_totals`, `dashboard_period_stats`, `security_weight_stats`) exist so the API stays inside its response-time budget — extend these in the builder rather than adding expensive per-request aggregation.
- Dashboard metrics: `avg_weight` is percent, equal-weighted over every `COMPLETE` manager with `total_value > 0` (non-holders count as zero); a holding is any `position_type = 0` row; `new_holder_count` requires `COMPLETE` on both sides of the adjacent pair. Weights are precomputed per period; the movers view diffs two periods at request time.

### Ticker mappings have three trust tiers

`data/cusip_tickers.json` is not a flat map, and the distinction matters across two scripts:

- `records[cusip].source == "openfigi"` — exact CUSIP mapping. Trusted everywhere.
- `records[cusip].source == "sec_name"` — resolved by uniquely matching the issuer name against the SEC company-ticker file. Good enough to **display**, and `build_database.py` writes it into `securities.ticker`, but `refresh_fund_signals.trusted_ticker_map()` deliberately ignores it.
- `manual_overrides[cusip]` — hand-verified, carries `evidence`/`note` provenance. Trusted everywhere, and kept separate so it never erases automated lookup diagnostics.

Consequence: a security can show a ticker in the UI and still be ineligible for pricing and fund-signal scoring. That is intended, and it is the usual reason a fund's signal is unavailable or its coverage sits under the 80% bar. To correct a specific bad mapping, add a provenance-bearing entry to `manual_overrides` — do not edit `records` in place. `enrich_tickers.py` never overwrites a non-blank `securities.ticker`, and ambiguous mappings are left blank rather than guessed.

### Server request contract

`server.py` is deliberately hostile by default:

- `API_PARAMETERS` is a per-endpoint allowlist; `reject_unknown_params()` 400s on anything unexpected. Adding a query parameter means adding it there.
- Endpoints: `/api/meta`, `/api/holdings`, `/api/aggregate`, `/api/funds`, `/api/suggest`, `/api/stock-detail`, `/api/fund-detail`, `/api/net-adds`, `/api/dashboard` (`view` ∈ holdings/initiations/movers, `horizon` 1–4, `side` gainers/losers, `sort` ∈ metric/ticker/name/price/day/ytd/sector default `metric`, `direction` ∈ asc/desc default `desc` except `asc` for movers losers, `page`, `size` default 100, `unmapped` ∈ exclude/include default `exclude`; `horizon`/`side` are validated for every view but only steer movers). `unmapped=exclude` adds `AND s.ticker!=''` to every view's `ranked` CTE — for movers after the union, so a security present only in P−k is still evaluated and dropped only when unmapped — before sorting and paging, so `count` reflects the filter; `include` is the pre-filter behaviour; the response echoes `unmapped`. `dashboard_order()` composes the ORDER BY only from the fixed `DASHBOARD_SORT_SQL`/`DASHBOARD_TIE_BREAKERS`/`SORT_DIRECTIONS` strings — never interpolate request text. `sort=metric` at the default direction is the view's historical order byte-for-byte; every other key runs `NULLS LAST` then that default order; price/day/ytd keys are computed in a `priced` CTE (three correlated `prices.bars` lookups per row, mark/previous/prior-December closes with `price_fields()` semantics) only when a price sort is requested and a mark date exists, otherwise a price sort is the default order. Sorting happens before `LIMIT`, `count` is unaffected, and the response echoes the effective `sort`/`direction`.
- `STATIC_PATHS` is exactly `{index.html, app.js, styles.css, dashboard.html, dashboard.js, dashboard.css}`; `static_path_for()` maps `/` → `index.html` and the three `DASHBOARD_ROUTES` (`/dashboard`, `/dashboard/initiations`, `/dashboard/movers`) → `dashboard.html`. Trailing slashes, case variants, and anything else 404. Archives, scripts, `data/`, and caches are never served.
- Path prefix: `BASE_PATH` (module global; `normalize_base_path()` accepts only `^/[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$`, maps `""`/`/` to `""`, strips one trailing slash, `parser.error` otherwise). `request_path()` is pure: `BASE_PATH` or `BASE_PATH + "/"` → `/`, `BASE_PATH + "/x"` → `/x`, anything else unchanged (so unprefixed paths — a Traefik `stripprefix` deployment — keep working and `/13f-other`/`/13fx/app.js` fall through to the allowlist and 404). `do_GET`/`do_HEAD` apply it before the `/api/` test and the static mapping; the length/parse checks run first, unchanged. Identity when `BASE_PATH` is empty.
- HTML is served by the handler, not `SimpleHTTPRequestHandler`: `render_document(name)` reads `index.html`/`dashboard.html` and, only when `BASE_PATH` is set, rewrites `href="/…"`/`src="/…"` to `href="{BASE_PATH}/…"` (never `//…`, `data:`, relative, or already-prefixed values). That is why both documents reference their assets root-absolute (`/styles.css`, `/app.js`, `/dashboard.css`, `/dashboard.js`) and why `dashboard.html` links are `/dashboard…` — one rewrite rule covers everything. JS/CSS still go through `SimpleHTTPRequestHandler`. Every non-API response carries `Cache-Control: no-cache` (a stale `styles.css` bit us once); API responses stay `no-store`. HEAD sends headers only.
- Startup prints the public prefix: `13F Explorer is running at http://host:port{BASE_PATH}/`.
- Strict CSP with no `unsafe-inline`, plus nosniff/DENY/no-referrer headers on every response. `verify.py` fails the build if `index.html` or `dashboard.html` gains an inline `<script>`, references any asset other than its own JS/CSS file (both documents use root-absolute asset paths — `/app.js`/`/styles.css`, `/dashboard.js`/`/dashboard.css` — so dashboard sub-paths resolve and `render_document()` can prefix them; the audit tolerates the single leading slash), has duplicate element IDs, or (dashboard) lacks any of the `DASHBOARD_IDS`.
- Read-only SQLite (`mode=ro`), bounded query/parameter lengths, page caps, and a `BoundedSemaphore(4)` on API work.
- Requests never perform network I/O. All market data arrives through the offline refresh scripts.
- The `research` fund scope cutoff (`$1B` disclosed value OR starred) is inline SQL in both `funds()` and `funds_from_stats()`; it reads `signals.scope_values` to correct legacy thousands-convention filers and falls back to raw `total_value` when the sidecar is absent.

### Frontend

`app.js` is one plain script — no modules, no framework. Its first line derives `BASE_PATH` from `document.currentScript.src` (the directory of the script URL, `''` at the root), and `api()` fetches `BASE_PATH + path`; history URLs are relative queries (`?view=…`, `?stock=…`) and need no prefix. `state` at the top holds view/filter/sort/page state; `VIEW_ROUTES`/`ROUTE_VIEWS` map the four panels (Funds, Securities·Browse, Securities·Quarterly changes, Securities·Positions) to history URLs. Rendering is string templating into `innerHTML`, so every interpolated value must go through `esc()`. The shared filter form serializes via `formParams()`; per-view sort state is handled by `handleSort()`/`markSortHeaders()`. IDs in `index.html` are effectively the component contract — `verify.py` and `tests/chromium_walkthrough.py` both assert against them.

`dashboard.js` is a second plain script for `dashboard.html` (no shared code with `app.js`). It derives `BASE_PATH` the same way; `VIEW_PATHS`, `routeUrl()`, and `api()` prefix with it and `routeFromUrl()` strips it from `url.pathname` before matching (a `BASE_PATH + '/dashboard/'` trailing slash is unknown → holdings, as at the root). `state` holds view/horizon/side/sort/direction/page/`unmapped` (`'exclude'` default; `?unmapped=include` is parsed by `routeFromUrl`, kept by `routeUrl` across view/sort/page changes, sent to the API only when `include`, and has no visible control — the `dash-missing` blank-ticker rendering path exists for that mode); `VIEW_PATHS` maps the three views to `/dashboard`, `/dashboard/initiations`, `/dashboard/movers`, with movers and sort state in the query string (`?horizon=2&side=losers&sort=ticker&direction=asc&page=3`, defaults omitted — `direction` is omitted when it equals the view default, `desc` or `asc` for movers losers) and `popstate` handled. `routeFromUrl` normalises unknown `sort`/`direction` values to the defaults, so a request with invalid values is never sent; requests carry `sort` only when non-metric and `direction` whenever the sort is non-metric or the direction differs from the view default, so default views still send exactly `view`/`horizon`/`side`/`page`/`size`. Nav/toggle/pager/header clicks (plain left-click only) route in-page via `history.pushState`; hrefs stay real URLs and always carry the full route. The list is `<table id="dashTable">` with `<tr id="dashHead">` of `th[scope=col][data-sort][aria-sort]` > `a.dash-sort(.active)` (+ `span.dash-sort-glyph` `↑`/`↓`); `renderHead()` sets the metric label per view (Avg Weight / New Holders / Weight Change), the active class, `aria-sort`, and each header's href (`sortRoute()`: the active column flips, another column starts at `SORT_DEFAULTS` — asc for ticker/name/sector, desc for price/day/ytd, the view default for metric — and resets `page`). Switching views resets the sort; `moversRoute()` keeps it across side/horizon changes. Rows render into `#dashRows` (`<tbody>`, `tr.dash-row[data-cusip]` > `td.dash-direction/.dash-ticker/.dash-name/.dash-metric/.dash-price/.dash-day/.dash-ytd/.dash-sector`) via `innerHTML`, so every value goes through its own `esc()`; glyphs, classes, and sector abbreviations come from constant maps keyed by validated values. Formatting rules (1 decimal ≥ 1 else 2, U+2212 minus, `pp` suffix, thousands separators, sector abbreviation map, title-casing of all-caps SEC issuer names) are in the README's Dashboard section and are part of the contract. The IDs in `dashboard.html` (`dashLogo`, `dashNav`, `dashMain`, `dashControls`, `dashSide`, `dashHorizon`, `dashTable`, `dashHead`, `dashRows`, `dashStatus`, `dashPager`, `dashPrev`, `dashNext`) are asserted by `verify.py` (`DASHBOARD_IDS`) and the walkthrough. Walkthrough selectors must stay scoped to `#dashRows .dash-*` / `#dashHead th[data-sort=…]` — a bare `.dash-direction` now matches the header cell first.

`tests/chromium_walkthrough.py` drives real Chromium over the DevTools protocol with a hand-rolled stdlib WebSocket client. After the explorer steps it runs `dashboard_views()` (holdings header sorting via `dashboard_sorting()` — Ticker asc, flip to desc, metric back to the default URL — then paging, initiations, movers at 2Q/Losers, `dashboard-desktop.png`) and a 375×812 `dashboard_viewport_check` (`dashboard-mobile.png`, no horizontal overflow). UI changes that move or rename IDs, columns, tab labels, or dashboard `data-*` attributes will break it; update the walkthrough alongside.

### Docker

`Dockerfile` (`python:3.13-slim`, no pip; apt adds only `make` and `nodejs` so `make signals`/`make verify-fast` work inside the container), `.dockerignore` (`*_form13f.zip`, `data/`, `artifacts/`, bytecode, `.github/`, `.git/`), and `docker-compose.yml` (Traefik labels for `soto.wiktor.io/13f`, `BASE_PATH=/13f`, `TRUST_DATABASE=1`, `./data` bind mount, `user: 1000:1000`). The image copies the scripts, `Makefile`, the six assets, `README.md`, and `tests/`; runs as `app` (uid/gid 1000); declares `VOLUME /app/data` and `EXPOSE 8080`; health-checks `http://127.0.0.1:8080${BASE_PATH}/api/meta` with `urllib`; and its entrypoint is `python3 server.py --host 0.0.0.0 --port 8080` with flags driven by `BASE_PATH`/`TRUST_DATABASE`/`ARCHIVE_DIR`. No stripprefix is needed because the server owns the prefix; the commented labels work too since unprefixed paths are still accepted. The non-loopback warning is expected in the container. `docker compose exec 13f make verify-fast` runs the fixture gate in the image; `docker compose exec 13f make signals` refreshes the sidecars into the volume. Keep the Dockerfile's `COPY` list in sync when adding a script or asset; never copy archives or `data/` into the image.

## Domain rules that the code enforces on purpose

These are correctness requirements, not cosmetic copy — the README documents them and the tests check them:

- Quarter-end snapshots are never summed across periods; they are not portfolio value or performance.
- "Change" means change between reported snapshots, not confirmed buys or sells. Breadth columns count managers whose reported units moved, not buyers.
- `NEW`/`EXITED` require complete filings on both sides of the pair.
- Fund `Signal return` is a hypothetical post-disclosure proxy (`Δu × (Pt − P0)` normalized by `Σ(abs(Δu) × P0)`), measured from the first close strictly after the effective filing date, and is only ranked at ≥10 priced signals, ≥80% coverage, and ≥5 effective bets. It is not P&L. Direction and weight come from split-normalized `SH` units only — never from reported value.
- The Featured set is an editorial research shortcut with cited sources in `data/starred_funds.json`, not a watchlist, ranking, or endorsement.
- Dashboard `Avg Weight` is an equal-weighted average across all complete filers (not among holders), `New Holders` is new-versus-prior-quarter, and `Weight Change` is a difference of two quarter-end averages. None of them is performance, a buy/sell signal, or a recommendation.
- Nothing in the app is investment advice; keep disclosure language in `index.html` and the README (including its Dashboard section) intact when touching those areas. `dashboard.html` deliberately carries no explanatory text, so the README is where its caveats live.
