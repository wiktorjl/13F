#!/usr/bin/env python3
"""Refresh cached daily closes and materialize split-aware fund signal scores.

The score is deliberately a public-disclosure signal, not actual fund P&L.  It
uses inferred changes in SH units between adjacent complete 13F snapshots and
prices those changes from the first market close after disclosure.
"""

from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
MAIN_DB = ROOT / "data" / "13f.sqlite"
PRICE_DB = ROOT / "data" / "prices.sqlite"
OUTPUT_DB = ROOT / "data" / "fund_signals.sqlite"
TICKER_MAP = ROOT / "data" / "cusip_tickers.json"

SOURCE_NAME = "Nasdaq Historical Quotes"
SOURCE_PAGE = "https://www.nasdaq.com/market-activity/quotes/historical"
SOURCE_TEMPLATE = (
    "https://api.nasdaq.com/api/quote/{symbol}/historical"
    "?assetclass=stocks&fromdate={from_date}&todate={to_date}&limit=5000"
)
METRIC_VERSION = "post_disclosure_signal_v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_PRICE_SYMBOLS = 250
RANK_MIN_EVENTS = 10
RANK_MIN_COVERAGE = 80.0
RANK_MIN_EFFECTIVE_BETS = 5.0

PRICE_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL COLLATE NOCASE,
  price_date TEXT NOT NULL,
  close REAL NOT NULL CHECK(close>0),
  PRIMARY KEY(symbol,price_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
"""

SCORE_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE fund_scores (
  cik TEXT NOT NULL, period TEXT NOT NULL, previous_period TEXT,
  filing_date TEXT, reference_date TEXT, mark_date TEXT,
  signal_return REAL, raw_signal_return REAL,
  signal_pnl REAL, raw_signal_pnl REAL, gross_notional REAL,
  candidate_events INTEGER NOT NULL, eligible_events INTEGER NOT NULL,
  priced_events INTEGER NOT NULL, buy_events INTEGER NOT NULL,
  sell_events INTEGER NOT NULL, hit_events INTEGER NOT NULL,
  signal_coverage REAL, effective_bets REAL,
  rankable INTEGER NOT NULL CHECK(rankable IN (0,1)), reason TEXT NOT NULL,
  PRIMARY KEY(cik,period)
) WITHOUT ROWID;
CREATE TABLE scope_values (
  cik TEXT NOT NULL, period TEXT NOT NULL,
  raw_reported_value INTEGER NOT NULL, value_scale INTEGER NOT NULL,
  scope_value INTEGER NOT NULL, evidence_count INTEGER NOT NULL,
  median_value_ratio REAL, scale_inferred INTEGER NOT NULL CHECK(scale_inferred IN (0,1)),
  PRIMARY KEY(cik,period)
) WITHOUT ROWID;
CREATE TABLE metadata (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX fund_scores_period_return ON fund_scores(period,signal_return DESC);
CREATE INDEX fund_scores_period_pnl ON fund_scores(period,signal_pnl DESC);
CREATE INDEX scope_values_period_value ON scope_values(period,scope_value DESC);
"""

# Cumulative split factors commonly seen in U.S. listings.  A factor below one
# represents a reverse split.  Reported value is used only to validate which
# unit basis applies; it is never used in event direction, weight, or return.
SPLIT_FACTORS = (
    0.01, 0.02, 0.025, 0.04, 0.05, 0.1, 0.125, 0.2, 0.25, 1 / 3,
    0.5, 2 / 3, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0,
    25.0, 40.0, 50.0, 100.0,
)


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_replace(temporary: Path, destination: Path) -> None:
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def temporary_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".building", dir=destination.parent
    )
    os.close(descriptor)
    return Path(name)


def completed_market_date(now: datetime | None = None) -> date:
    eastern = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    candidate = eastern.date()
    if eastern.time() < datetime_time(17, 0):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def nasdaq_symbol(ticker: str) -> str:
    # Nasdaq's quote routes use a dot for class shares (the local cache uses
    # slash for the canonical display symbol).
    return ticker.replace("/", ".")


def parse_close(value: object) -> float | None:
    try:
        parsed = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def read_limited(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > limit:
                raise ValueError("Nasdaq response is too large")
        except ValueError as exc:
            if str(exc) == "Nasdaq response is too large":
                raise
    content_type = response.headers.get_content_type()
    if content_type not in {"application/json", "text/json", "text/plain"}:
        raise ValueError(f"Unexpected Nasdaq content type: {content_type}")
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Nasdaq response is too large")
    return payload


def fetch_history(ticker: str, from_date: str, to_date: str, retries: int = 3) -> list[tuple[str, float]]:
    symbol = nasdaq_symbol(ticker)
    if not re.fullmatch(r"[A-Z0-9.\-^]{1,32}", symbol):
        raise ValueError("Unsupported Nasdaq symbol")
    url = SOURCE_TEMPLATE.format(
        symbol=quote(symbol, safe=""), from_date=from_date, to_date=to_date
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "Mozilla/5.0 (compatible; 13F-Explorer/1.0)",
                },
            )
            with urlopen(request, timeout=45) as response:
                payload = json.loads(read_limited(response).decode("utf-8"))
            if payload.get("status", {}).get("rCode") != 200:
                raise ValueError("Nasdaq returned a non-success status")
            rows = payload.get("data", {}).get("tradesTable", {}).get("rows") or []
            parsed: dict[str, float] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    price_date = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date().isoformat()
                except ValueError:
                    continue
                close = parse_close(row.get("close"))
                if close is not None and from_date <= price_date <= to_date:
                    parsed[price_date] = close
            return sorted(parsed.items())
        except Exception as exc:  # retry network and response-shape failures alike
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.75 * (2 ** attempt))
    raise RuntimeError(f"{ticker}: {last_error}")


def trusted_ticker_map(path: Path = TICKER_MAP) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", {})
    overrides = payload.get("manual_overrides", {})
    result: dict[str, str] = {}
    for cusip, record in records.items():
        if not isinstance(record, dict) or record.get("source") != "openfigi":
            continue
        ticker = str(record.get("ticker") or "").strip().upper()
        if ticker:
            result[str(cusip).strip().upper()] = ticker
    for cusip, record in overrides.items():
        if not isinstance(record, dict):
            continue
        ticker = str(record.get("ticker") or "").strip().upper()
        if ticker:
            result[str(cusip).strip().upper()] = ticker
    return result


def main_metadata(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key,value FROM metadata"))


def price_targets(con: sqlite3.Connection, trusted: dict[str, str]) -> tuple[set[str], str]:
    con.execute("CREATE TEMP TABLE trusted_map(cusip TEXT PRIMARY KEY,ticker TEXT NOT NULL) WITHOUT ROWID")
    con.executemany("INSERT INTO trusted_map VALUES (?,?)", trusted.items())
    symbols = {
        row[0]
        for row in con.execute(
            """SELECT DISTINCT t.ticker FROM securities s JOIN trusted_map t ON t.cusip=s.cusip
            JOIN positions p ON p.security_id=s.id
            WHERE p.position_type=0 AND p.shares_type=0 AND p.shares IS NOT NULL"""
        )
    }
    earliest = con.execute("SELECT min(period_date) FROM periods").fetchone()[0]
    if not earliest:
        raise ValueError("No reporting periods are available")
    return symbols, earliest


def refresh_prices(
    main_db: Path, price_db: Path, trusted: dict[str, str], workers: int
) -> tuple[str, int, int]:
    with closing(sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)) as source:
        symbols, earliest = price_targets(source, trusted)
    if not symbols:
        raise ValueError("No trusted priced symbols were found")

    candidate_end = completed_market_date().isoformat()
    probe_start = (date.fromisoformat(candidate_end) - timedelta(days=14)).isoformat()
    probe_symbol = "AAPL" if "AAPL" in symbols else sorted(symbols)[0]
    probe = fetch_history(probe_symbol, probe_start, candidate_end)
    if not probe:
        raise ValueError("Could not establish the latest completed market close")
    mark_date = probe[-1][0]

    temporary = temporary_path(price_db)
    try:
        if price_db.exists():
            shutil.copy2(price_db, temporary)
        with closing(sqlite3.connect(temporary)) as prices:
            prices.executescript(PRICE_SCHEMA)
            failures: list[str] = []
            completed = 0
            print(
                f"Refreshing {len(symbols):,} trusted symbols from {earliest} through {mark_date}…",
                flush=True,
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fetch_history, ticker, earliest, mark_date): ticker
                    for ticker in sorted(symbols)
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        rows = future.result()
                        if rows:
                            prices.executemany(
                                "INSERT OR REPLACE INTO bars(symbol,price_date,close) VALUES (?,?,?)",
                                ((ticker, price_date, close) for price_date, close in rows),
                            )
                        else:
                            failures.append(f"{ticker}: no rows")
                    except Exception as exc:
                        failures.append(str(exc))
                    completed += 1
                    if completed % 50 == 0 or completed == len(symbols):
                        print(
                            f"  {completed:,}/{len(symbols):,} symbols · {len(failures):,} unavailable",
                            flush=True,
                        )
                    if completed % 100 == 0:
                        prices.commit()

            exact_mark = prices.execute(
                "SELECT count(DISTINCT symbol) FROM bars WHERE price_date=?", (mark_date,)
            ).fetchone()[0]
            if exact_mark < min(MIN_PRICE_SYMBOLS, max(1, len(symbols) // 2)):
                raise ValueError(
                    f"Only {exact_mark:,} symbols have the common {mark_date} close"
                )
            prices.execute("DELETE FROM metadata")
            metadata = {
                "schema_version": "1",
                "source": SOURCE_NAME,
                "source_page": SOURCE_PAGE,
                "source_url_template": SOURCE_TEMPLATE,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "from_date": earliest,
                "mark_date": mark_date,
                "requested_symbol_count": str(len(symbols)),
                "mark_symbol_count": str(exact_mark),
                "failed_symbol_count": str(len(failures)),
                "failed_symbols": json.dumps(failures[:200], separators=(",", ":")),
                "price_adjustment": "Nasdaq historical closes are retrospectively split-adjusted; dividends excluded",
            }
            prices.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
            prices.execute("ANALYZE")
            check = [row[0] for row in prices.execute("PRAGMA quick_check")]
            if check != ["ok"]:
                raise ValueError("Price-cache validation failed: " + "; ".join(check))
            prices.commit()
        atomic_replace(temporary, price_db)
    finally:
        temporary.unlink(missing_ok=True)
    return mark_date, len(symbols), exact_mark


class PriceBook:
    def __init__(self, path: Path):
        self.dates: dict[str, list[str]] = defaultdict(list)
        self.values: dict[str, list[float]] = defaultdict(list)
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            for symbol, price_date, close in con.execute(
                "SELECT symbol,price_date,close FROM bars ORDER BY symbol,price_date"
            ):
                self.dates[symbol].append(price_date)
                self.values[symbol].append(float(close))

    def exact(self, symbol: str, price_date: str) -> float | None:
        dates = self.dates.get(symbol)
        if not dates:
            return None
        index = bisect.bisect_left(dates, price_date)
        return self.values[symbol][index] if index < len(dates) and dates[index] == price_date else None

    def on_or_before(self, symbol: str, price_date: str) -> tuple[str, float] | None:
        dates = self.dates.get(symbol)
        if not dates:
            return None
        index = bisect.bisect_right(dates, price_date) - 1
        return (dates[index], self.values[symbol][index]) if index >= 0 else None

    def first_after(self, symbol: str, price_date: str) -> tuple[str, float] | None:
        dates = self.dates.get(symbol)
        if not dates:
            return None
        index = bisect.bisect_right(dates, price_date)
        return (dates[index], self.values[symbol][index]) if index < len(dates) else None


def closest_split_factor(ratio: float) -> float | None:
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    candidate = min(SPLIT_FACTORS, key=lambda factor: abs(math.log(ratio / factor)))
    return candidate if abs(ratio / candidate - 1.0) <= 0.08 else None


def materialize_split_factors(
    con: sqlite3.Connection, periods: list[sqlite3.Row], prices: PriceBook
) -> dict[tuple[int, int], float]:
    factors: dict[tuple[int, int], float] = {}
    print("Validating split-adjusted unit bases…", flush=True)
    for period in periods:
        current_security: int | None = None
        ratios: list[float] = []

        def finish() -> None:
            if current_security is None or not ratios:
                return
            median = statistics.median(ratios)
            factor = closest_split_factor(median)
            if factor is not None:
                factors[(period["id"], current_security)] = factor

        query = """SELECT p.security_id,t.ticker,p.value,p.shares
          FROM positions p JOIN securities s ON s.id=p.security_id
          JOIN trusted_map t ON t.cusip=s.cusip
          WHERE p.period_id=? AND p.position_type=0 AND p.shares_type=0
            AND p.shares>0 AND p.value>0 ORDER BY p.security_id"""
        for security_id, ticker, value, shares in con.execute(query, (period["id"],)):
            if security_id != current_security:
                finish()
                current_security, ratios = security_id, []
            close = prices.on_or_before(ticker, period["period_date"])
            if close:
                ratios.append((float(value) / float(shares)) / close[1])
        finish()
        print(
            f"  {period['label']}: {sum(key[0] == period['id'] for key in factors):,} securities validated",
            flush=True,
        )
    return factors


def materialize_scope_values(
    con: sqlite3.Connection,
    periods: list[sqlite3.Row],
    prices: PriceBook,
    factors: dict[tuple[int, int], float],
) -> list[tuple]:
    totals = {
        (row[0], row[1]): int(row[2])
        for row in con.execute("SELECT manager_id,period_id,total_value FROM manager_period_stats")
    }
    manager_ciks = dict(con.execute("SELECT id,cik FROM managers"))
    period_labels = {row["id"]: row["label"] for row in periods}
    period_dates = {row["id"]: row["period_date"] for row in periods}
    evidence: dict[tuple[int, int], list[float]] = defaultdict(list)
    query = """SELECT p.manager_id,p.period_id,p.security_id,t.ticker,p.value,p.shares
      FROM positions p JOIN securities s ON s.id=p.security_id
      JOIN trusted_map t ON t.cusip=s.cusip
      WHERE p.position_type=0 AND p.shares_type=0 AND p.shares>0 AND p.value>0
      ORDER BY p.manager_id,p.period_id"""
    for manager_id, period_id, security_id, ticker, value, shares in con.execute(query):
        factor = factors.get((period_id, security_id))
        close = prices.on_or_before(ticker, period_dates[period_id])
        if factor is None or close is None:
            continue
        expected = float(shares) * close[1] * factor
        if expected > 0:
            evidence[(manager_id, period_id)].append(float(value) / expected)

    rows = []
    scaled = 0
    for (manager_id, period_id), raw_total in totals.items():
        ratios = evidence.get((manager_id, period_id), [])
        median = statistics.median(ratios) if ratios else None
        scale = 1000 if len(ratios) >= 3 and median is not None and 0.0005 <= median <= 0.002 else 1
        scaled += scale == 1000
        scope_value = min(9_000_000_000_000_000_000, raw_total * scale)
        rows.append(
            (
                manager_ciks[manager_id], period_labels[period_id], raw_total, scale,
                scope_value, len(ratios), median, int(scale != 1),
            )
        )
    print(f"Normalized {scaled:,} obvious legacy-thousands filing totals for cutoff use.", flush=True)
    return rows


def empty_accumulator() -> dict:
    return {
        "candidate": 0, "eligible": 0, "priced": 0, "buys": 0, "sells": 0,
        "hits": 0, "pnl": 0.0, "gross": 0.0, "sumw2": 0.0,
        "reference_dates": [],
    }


def materialize_scores(
    con: sqlite3.Connection,
    periods: list[sqlite3.Row],
    prices: PriceBook,
    factors: dict[tuple[int, int], float],
    mark_date: str,
) -> list[tuple]:
    manager_periods = {
        (row[0], row[1]): {
            "cik": row[2], "coverage": row[3], "filing_date": row[4],
        }
        for row in con.execute(
            """SELECT mp.manager_id,mp.period_id,m.cik,mp.coverage_status,mp.filing_date_iso
            FROM manager_periods mp JOIN managers m ON m.id=mp.manager_id"""
        )
    }
    period_by_id = {row["id"]: row for row in periods}
    results: list[tuple] = []
    print("Computing post-disclosure fund signals…", flush=True)

    for previous, current in zip(periods, periods[1:]):
        accumulators: dict[int, dict] = defaultdict(empty_accumulator)
        query = """WITH c AS (
            SELECT p.manager_id,p.security_id,p.shares
            FROM positions p JOIN manager_periods mp
              ON mp.manager_id=p.manager_id AND mp.period_id=p.period_id
            WHERE p.period_id=? AND p.position_type=0 AND p.shares_type=0
              AND p.shares IS NOT NULL AND mp.coverage_status='COMPLETE'),
          v AS (
            SELECT p.manager_id,p.security_id,p.shares
            FROM positions p JOIN manager_periods mp
              ON mp.manager_id=p.manager_id AND mp.period_id=p.period_id
            WHERE p.period_id=? AND p.position_type=0 AND p.shares_type=0
              AND p.shares IS NOT NULL AND mp.coverage_status='COMPLETE'),
          keys AS (SELECT manager_id,security_id FROM c UNION SELECT manager_id,security_id FROM v)
          SELECT k.manager_id,k.security_id,s.ticker browser_ticker,t.ticker trusted_ticker,
            c.shares current_shares,v.shares previous_shares
          FROM keys k JOIN manager_periods cm ON cm.manager_id=k.manager_id AND cm.period_id=?
          JOIN manager_periods vm ON vm.manager_id=k.manager_id AND vm.period_id=?
          JOIN securities s ON s.id=k.security_id
          LEFT JOIN trusted_map t ON t.cusip=s.cusip
          LEFT JOIN c USING(manager_id,security_id) LEFT JOIN v USING(manager_id,security_id)
          WHERE cm.coverage_status='COMPLETE' AND vm.coverage_status='COMPLETE'
          ORDER BY k.manager_id,k.security_id"""
        for row in con.execute(
            query, (current["id"], previous["id"], current["id"], previous["id"])
        ):
            manager_id, security_id = row[0], row[1]
            current_shares = float(row[4] or 0)
            previous_shares = float(row[5] or 0)
            current_factor = factors.get((current["id"], security_id)) if row[4] is not None else 1.0
            previous_factor = factors.get((previous["id"], security_id)) if row[5] is not None else 1.0
            accumulator = accumulators[manager_id]
            if current_factor is None or previous_factor is None:
                if abs(current_shares - previous_shares) > 1e-8:
                    accumulator["candidate"] += 1
                continue
            delta = current_shares * current_factor - previous_shares * previous_factor
            tolerance = max(1e-8, max(abs(current_shares * current_factor), abs(previous_shares * previous_factor)) * 1e-9)
            if abs(delta) <= tolerance:
                continue
            accumulator["candidate"] += 1
            ticker = row[3]
            if not ticker:
                continue
            accumulator["eligible"] += 1
            filing_date = manager_periods[(manager_id, current["id"])]["filing_date"]
            reference = prices.first_after(ticker, filing_date)
            latest = prices.exact(ticker, mark_date)
            if reference is None or latest is None or reference[0] > mark_date:
                continue
            reference_price = reference[1]
            gross = abs(delta) * reference_price
            if not math.isfinite(gross) or gross <= 0:
                continue
            pnl = delta * (latest - reference_price)
            accumulator["priced"] += 1
            accumulator["buys" if delta > 0 else "sells"] += 1
            accumulator["hits"] += pnl > 0
            accumulator["pnl"] += pnl
            accumulator["gross"] += gross
            accumulator["sumw2"] += gross * gross
            accumulator["reference_dates"].append(reference[0])

        previous_label = previous["label"]
        for (manager_id, period_id), info in manager_periods.items():
            if period_id != current["id"]:
                continue
            accumulator = accumulators.get(manager_id, empty_accumulator())
            eligible = accumulator["eligible"]
            priced = accumulator["priced"]
            coverage = 100.0 * priced / eligible if eligible else 0.0
            gross = accumulator["gross"]
            effective = gross * gross / accumulator["sumw2"] if accumulator["sumw2"] > 0 else 0.0
            raw_return = 100.0 * accumulator["pnl"] / gross if gross > 0 else None
            reasons: list[str] = []
            previous_info = manager_periods.get((manager_id, previous["id"]))
            if info["coverage"] != "COMPLETE" or previous_info is None or previous_info["coverage"] != "COMPLETE":
                reasons.append("adjacent filings are not both complete")
            if priced < RANK_MIN_EVENTS:
                reasons.append(f"fewer than {RANK_MIN_EVENTS} priced signals")
            if coverage < RANK_MIN_COVERAGE:
                reasons.append(f"priced coverage below {RANK_MIN_COVERAGE:.0f}%")
            if effective < RANK_MIN_EFFECTIVE_BETS:
                reasons.append(f"effective bets below {RANK_MIN_EFFECTIVE_BETS:.0f}")
            rankable = not reasons
            reference_dates = accumulator["reference_dates"]
            reference_date = min(reference_dates) if reference_dates else None
            results.append(
                (
                    info["cik"], current["label"], previous_label, info["filing_date"],
                    reference_date, mark_date,
                    raw_return if rankable else None, raw_return,
                    accumulator["pnl"] if rankable else None,
                    accumulator["pnl"] if gross > 0 else None, gross if gross > 0 else None,
                    accumulator["candidate"], eligible, priced, accumulator["buys"],
                    accumulator["sells"], accumulator["hits"], coverage, effective,
                    int(rankable), "; ".join(reasons),
                )
            )
        ranked = sum(row[-2] for row in results if row[1] == current["label"])
        print(f"  {current['label']}: {ranked:,} funds meet ranking guardrails", flush=True)

    # The oldest quarter has no adjacent predecessor and therefore no score.
    oldest = periods[0]
    for (manager_id, period_id), info in manager_periods.items():
        if period_id == oldest["id"]:
            results.append(
                (
                    info["cik"], oldest["label"], None, info["filing_date"], None, mark_date,
                    None, None, None, None, None, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0,
                    "no earlier quarter is available",
                )
            )
    return results


def build_scores(main_db: Path, price_db: Path, output_db: Path, trusted: dict[str, str], mark_date: str) -> None:
    prices = PriceBook(price_db)
    with closing(sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("CREATE TEMP TABLE trusted_map(cusip TEXT PRIMARY KEY,ticker TEXT NOT NULL) WITHOUT ROWID")
        source.executemany("INSERT INTO trusted_map VALUES (?,?)", trusted.items())
        periods = source.execute("SELECT * FROM periods ORDER BY period_date").fetchall()
        factors = materialize_split_factors(source, periods, prices)
        scope_rows = materialize_scope_values(source, periods, prices, factors)
        score_rows = materialize_scores(source, periods, prices, factors, mark_date)
        source_meta = main_metadata(source)

    temporary = temporary_path(output_db)
    try:
        with closing(sqlite3.connect(temporary)) as output:
            output.executescript(SCORE_SCHEMA)
            output.executemany("INSERT INTO fund_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", score_rows)
            output.executemany("INSERT INTO scope_values VALUES (?,?,?,?,?,?,?,?)", scope_rows)
            metadata = {
                "schema_version": "1",
                "metric_version": METRIC_VERSION,
                "available": "1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mark_date": mark_date,
                "price_source": SOURCE_NAME,
                "price_source_page": SOURCE_PAGE,
                "price_cache_sha256": sha256_file(price_db),
                "price_adjustment": "retrospectively split-adjusted close; dividends excluded",
                "reference_policy": "first market close strictly after effective filing date",
                "event_policy": "adjacent COMPLETE filings; non-option SH units; exact OpenFIGI/manual ticker mappings",
                "ranking_policy": (
                    f">={RANK_MIN_EVENTS} priced signals; >={RANK_MIN_COVERAGE:.0f}% event coverage; "
                    f"effective bets >={RANK_MIN_EFFECTIVE_BETS:.0f}"
                ),
                "research_cutoff": "1000000000",
                "main_schema_version": source_meta.get("schema_version", ""),
                "main_source_archive_hashes": source_meta.get("source_archive_hashes", "[]"),
                "main_ticker_map_sha256": source_meta.get("ticker_map_sha256", ""),
                "trusted_ticker_count": str(len(set(trusted.values()))),
                "score_count": str(len(score_rows)),
                "rankable_score_count": str(sum(row[-2] for row in score_rows)),
                "scope_value_count": str(len(scope_rows)),
                "scaled_scope_value_count": str(sum(row[-1] for row in scope_rows)),
            }
            output.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
            output.execute("ANALYZE")
            checks = [row[0] for row in output.execute("PRAGMA quick_check")]
            if checks != ["ok"]:
                raise ValueError("Signal-cache validation failed: " + "; ".join(checks))
            duplicate_check = output.execute(
                "SELECT count(*)-count(DISTINCT cik||':'||period) FROM fund_scores"
            ).fetchone()[0]
            if duplicate_check:
                raise ValueError("Signal-cache validation found duplicate fund-period rows")
            output.commit()
        atomic_replace(temporary, output_db)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=MAIN_DB)
    parser.add_argument("--price-cache", type=Path, default=PRICE_DB)
    parser.add_argument("--output", type=Path, default=OUTPUT_DB)
    parser.add_argument("--ticker-map", type=Path, default=TICKER_MAP)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    if not args.database.exists():
        raise SystemExit(f"Database does not exist: {args.database}")
    trusted = trusted_ticker_map(args.ticker_map)
    if not trusted:
        raise SystemExit("No exact/manual ticker mappings are available")

    started = time.time()
    lock_path = args.output.with_suffix(args.output.suffix + ".lock")
    with exclusive_lock(lock_path):
        mark_date, requested, priced = refresh_prices(
            args.database, args.price_cache, trusted, args.workers
        )
        build_scores(args.database, args.price_cache, args.output, trusted, mark_date)
    print(
        f"Saved fund signals through {mark_date}: {priced:,}/{requested:,} symbols at the common close "
        f"in {time.time() - started:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
