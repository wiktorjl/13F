# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

13F Explorer: a local, single-user browser for multi-quarter SEC Form 13F data. The `*_form13f.zip` archives in the repository root are the only source of truth; everything in `data/` is derived or cached.

**Hard constraint: Python standard library only, and no JavaScript build step or npm dependencies.** There is no `requirements.txt`, `package.json`, or `pyproject.toml`, and `verify.py` compiles every `*.py` and runs `node --check app.js` directly. Do not introduce third-party imports, bundlers, or frameworks. Node is a verification tool, not a runtime dependency.

## Commands

```bash
python3 server.py                    # run (builds data/13f.sqlite on first run; binds 127.0.0.1:8013)
python3 server.py --no-build         # refuse to build; require an already-current database
python3 server.py --host 192.168.x.x --port 9000   # LAN exposure; prints a warning for non-loopback
python3 build_database.py --force    # rebuild the main database (minutes)
make signals                         # refresh data/prices.sqlite + data/fund_signals.sqlite (network)
python3 refresh_market_caps.py       # refresh data/market_caps.json (network; then rebuild --force)
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

Environment variables (all optional, all for the offline scripts): `CHROMIUM` (walkthrough executable, or `--chromium`), `OPENFIGI_API_KEY` (raises OpenFIGI limits from 25 req/60s and 5 jobs to 25 req/6s and 100 jobs — enrichment is impractically slow without it), `SEC_USER_AGENT` (required politeness header for the SEC company-ticker file).

## Architecture

Four stages, each with its own cache and its own freshness identity:

1. **`build_database.py`** — reads `SUBMISSION/COVERPAGE/SUMMARYPAGE/INFOTABLE.tsv` out of the zips and writes `data/13f.sqlite`. Also folds in three JSON inputs: `data/cusip_tickers.json` (tickers), `data/market_caps.json`, `data/starred_funds.json` (the fixed 20-manager Featured set).
2. **`enrich_tickers.py` / `refresh_market_caps.py`** — network-fetching cache builders that write those JSON inputs. Never called from the server.
3. **`refresh_fund_signals.py`** — reads the main DB, fetches Nasdaq closes into `data/prices.sqlite`, writes the `data/fund_signals.sqlite` sidecar (post-disclosure signal scores + `scope_values`).
4. **`server.py` + `index.html`/`app.js`/`styles.css`** — read-only stdlib HTTP server and a vanilla-JS SPA.

`verify.py` sits across all of it and is the release gate; `tests/support.py` builds the tiny schema-current fixture that the fast path and HTTP tests share.

### Freshness is content-hash identity, not timestamps

`server.database_is_current()` compares DB `metadata` rows against live SHA-256 hashes of every archive plus the three JSON inputs, and against `SCHEMA_VERSION`. Any change to an archive, `cusip_tickers.json`, `market_caps.json`, or `starred_funds.json` invalidates the database and triggers a full rebuild on next `server.py` start.

`SCHEMA_VERSION` is duplicated in **both** `build_database.py` and `server.py` (currently `"8"`). Changing the schema means bumping both, or the server will reject every build it makes.

The signals sidecar is attached at request time by `attach_signal_snapshot()` only when its stored `main_schema_version` / `main_source_archive_hashes` / `main_ticker_map_sha256` match the main DB. On mismatch it attaches an in-memory `EMPTY_SIGNAL_SCHEMA` instead, so signal columns degrade to "unavailable" rather than erroring. Any query touching `signals.*` must keep working against that empty schema.

### Build invariants

- One archive = one primary reporting period; two archives resolving to the same period is a fatal error.
- Canonical snapshot per (manager, period): latest `BASE`/`RESTATEMENT` resets the part list, later `NEW HOLDINGS` amendments append, `UNKNOWN AMENDMENT` resets and marks the chain non-comparable (`effective_chain()`).
- Coverage is `COMPLETE` / `PARTIAL` / `INFERRED` / `NOTICE`. Only `COMPLETE` pairs feed adjacent-quarter comparisons; missing coverage is `NOT COMPARABLE`, never zero.
- Every effective part is reconciled against SEC summary entry/value totals into `filing_part_stats`; a mismatch downgrades to partial but stays browseable.
- Every starred CIK in `starred_funds.json` must exist in the archives, or the build fails.
- Builds are guarded by `data/.13f.sqlite.build.lock`, write to a temp file, and `atomic_replace()` into place. Inputs are re-hashed after the read pass (`assert_inputs_unchanged`) to catch archives mutated mid-build.
- `build_database.validate_database()` is the **single definition of database invariants**, and `verify.py` imports and re-runs it against the production DB rather than restating the rules. New invariants belong there, not in the verifier — that way the builder refuses to swap in a bad database and the release gate catches drift in an existing one.
- `refresh_fund_signals.py` takes its own `data/fund_signals.sqlite.lock`, requires the main DB to already exist, and also writes atomically. `--workers` is bounded 1–32 (default 12).

### Encodings used everywhere

- `positions.position_type`: `0` shares/other, `1` long put, `2` long call.
- `positions.shares_type`: `0` `SH`, `1` `PRN`, `2` other.
- Quarterly-change rankings (`stock_changes`) use `shares_type = 0` only; `PRN` stays visible in Positions and detail views.
- CIKs normalized to 10 digits, CUSIPs uppercased, before any matching.
- Precomputed rollups (`manager_period_stats`, `security_period_stats`, `period_stats`, `stock_changes`, `period_change_totals`) exist so the API stays inside its response-time budget — extend these in the builder rather than adding expensive per-request aggregation.

### Ticker mappings have three trust tiers

`data/cusip_tickers.json` is not a flat map, and the distinction matters across two scripts:

- `records[cusip].source == "openfigi"` — exact CUSIP mapping. Trusted everywhere.
- `records[cusip].source == "sec_name"` — resolved by uniquely matching the issuer name against the SEC company-ticker file. Good enough to **display**, and `build_database.py` writes it into `securities.ticker`, but `refresh_fund_signals.trusted_ticker_map()` deliberately ignores it.
- `manual_overrides[cusip]` — hand-verified, carries `evidence`/`note` provenance. Trusted everywhere, and kept separate so it never erases automated lookup diagnostics.

Consequence: a security can show a ticker in the UI and still be ineligible for pricing and fund-signal scoring. That is intended, and it is the usual reason a fund's signal is unavailable or its coverage sits under the 80% bar. To correct a specific bad mapping, add a provenance-bearing entry to `manual_overrides` — do not edit `records` in place. `enrich_tickers.py` never overwrites a non-blank `securities.ticker`, and ambiguous mappings are left blank rather than guessed.

### Server request contract

`server.py` is deliberately hostile by default:

- `API_PARAMETERS` is a per-endpoint allowlist; `reject_unknown_params()` 400s on anything unexpected. Adding a query parameter means adding it there.
- Endpoints: `/api/meta`, `/api/holdings`, `/api/aggregate`, `/api/funds`, `/api/suggest`, `/api/stock-detail`, `/api/fund-detail`, `/api/net-adds`.
- `STATIC_PATHS` is exactly `{index.html, app.js, styles.css}`. Archives, scripts, `data/`, and caches are never served.
- Strict CSP with no `unsafe-inline`, plus nosniff/DENY/no-referrer headers on every response. `verify.py` fails the build if `index.html` gains an inline `<script>` or references any asset other than `app.js`/`styles.css`, or has duplicate element IDs.
- Read-only SQLite (`mode=ro`), bounded query/parameter lengths, page caps, and a `BoundedSemaphore(4)` on API work.
- Requests never perform network I/O. All market data arrives through the offline refresh scripts.
- The `research` fund scope cutoff (`$1B` disclosed value OR starred) is inline SQL in both `funds()` and `funds_from_stats()`; it reads `signals.scope_values` to correct legacy thousands-convention filers and falls back to raw `total_value` when the sidecar is absent.

### Frontend

`app.js` is one plain script — no modules, no framework. `state` at the top holds view/filter/sort/page state; `VIEW_ROUTES`/`ROUTE_VIEWS` map the four panels (Funds, Securities·Browse, Securities·Quarterly changes, Securities·Positions) to history URLs. Rendering is string templating into `innerHTML`, so every interpolated value must go through `esc()`. The shared filter form serializes via `formParams()`; per-view sort state is handled by `handleSort()`/`markSortHeaders()`. IDs in `index.html` are effectively the component contract — `verify.py` and `tests/chromium_walkthrough.py` both assert against them.

`tests/chromium_walkthrough.py` drives real Chromium over the DevTools protocol with a hand-rolled stdlib WebSocket client. UI changes that move or rename IDs, columns, or tab labels will break it; update the walkthrough alongside.

## Domain rules that the code enforces on purpose

These are correctness requirements, not cosmetic copy — the README documents them and the tests check them:

- Quarter-end snapshots are never summed across periods; they are not portfolio value or performance.
- "Change" means change between reported snapshots, not confirmed buys or sells. Breadth columns count managers whose reported units moved, not buyers.
- `NEW`/`EXITED` require complete filings on both sides of the pair.
- Fund `Signal return` is a hypothetical post-disclosure proxy (`Δu × (Pt − P0)` normalized by `Σ(abs(Δu) × P0)`), measured from the first close strictly after the effective filing date, and is only ranked at ≥10 priced signals, ≥80% coverage, and ≥5 effective bets. It is not P&L. Direction and weight come from split-normalized `SH` units only — never from reported value.
- The Featured set is an editorial research shortcut with cited sources in `data/starred_funds.json`, not a watchlist, ranking, or endorsement.
- Nothing in the app is investment advice; keep disclosure language in `index.html` and the README intact when touching those areas.
