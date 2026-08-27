#!/usr/bin/env python3
"""Build a compact, amendment-aware multi-quarter Form 13F database."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import fcntl
import glob
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "13f.sqlite"
TICKER_MAP = ROOT / "data" / "cusip_tickers.json"
MARKET_CAPS = ROOT / "data" / "market_caps.json"
STARRED_FUNDS = ROOT / "data" / "starred_funds.json"
SCHEMA_VERSION = "8"

SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-262144;
PRAGMA page_size=8192;

CREATE TABLE periods (
  id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE,
  period_date TEXT NOT NULL UNIQUE, source_archive TEXT NOT NULL
);
CREATE TABLE managers (
  id INTEGER PRIMARY KEY, cik TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, state_country TEXT NOT NULL DEFAULT '',
  starred INTEGER NOT NULL DEFAULT 0 CHECK (starred IN (0,1))
);
CREATE TABLE securities (
  id INTEGER PRIMARY KEY, cusip TEXT NOT NULL UNIQUE,
  issuer TEXT NOT NULL, class TEXT NOT NULL DEFAULT '', figi TEXT NOT NULL DEFAULT '',
  ticker TEXT NOT NULL DEFAULT ''
);
CREATE TABLE market_caps (
  ticker TEXT PRIMARY KEY COLLATE NOCASE,
  market_cap INTEGER NOT NULL CHECK (market_cap > 0)
) WITHOUT ROWID;
CREATE TABLE manager_periods (
  manager_id INTEGER NOT NULL, period_id INTEGER NOT NULL,
  accession TEXT NOT NULL, filing_date TEXT NOT NULL, filing_date_iso TEXT NOT NULL,
  source_archive TEXT NOT NULL, report_type TEXT NOT NULL,
  coverage_status TEXT NOT NULL, confidential_omitted INTEGER NOT NULL DEFAULT 0,
  part_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (manager_id, period_id)
) WITHOUT ROWID;
CREATE TABLE effective_filing_parts (
  manager_id INTEGER NOT NULL, period_id INTEGER NOT NULL,
  accession TEXT NOT NULL UNIQUE, part_type TEXT NOT NULL,
  PRIMARY KEY (manager_id, period_id, accession)
) WITHOUT ROWID;
CREATE TABLE positions (
  manager_id INTEGER NOT NULL, period_id INTEGER NOT NULL, security_id INTEGER NOT NULL,
  position_type INTEGER NOT NULL, shares_type INTEGER NOT NULL,
  value INTEGER NOT NULL, shares REAL,
  PRIMARY KEY (manager_id, period_id, security_id, position_type, shares_type)
) WITHOUT ROWID;
CREATE TABLE filing_part_stats (
  accession TEXT PRIMARY KEY, manager_id INTEGER NOT NULL, period_id INTEGER NOT NULL,
  reported_entry_count INTEGER, reported_value INTEGER,
  observed_entry_count INTEGER NOT NULL, observed_value INTEGER NOT NULL,
  entry_count_matches INTEGER CHECK (entry_count_matches IN (0,1)),
  value_matches INTEGER CHECK (value_matches IN (0,1)),
  has_discrepancy INTEGER NOT NULL CHECK (has_discrepancy IN (0,1))
) WITHOUT ROWID;
CREATE TABLE manager_period_stats (
  manager_id INTEGER NOT NULL, period_id INTEGER NOT NULL,
  position_count INTEGER NOT NULL, security_count INTEGER NOT NULL,
  total_value INTEGER NOT NULL,
  PRIMARY KEY (manager_id, period_id)
) WITHOUT ROWID;
CREATE TABLE security_period_stats (
  period_id INTEGER NOT NULL, security_id INTEGER NOT NULL,
  position_count INTEGER NOT NULL, manager_count INTEGER NOT NULL,
  total_value INTEGER NOT NULL,
  PRIMARY KEY (period_id, security_id)
) WITHOUT ROWID;
CREATE TABLE period_stats (
  period_id INTEGER PRIMARY KEY,
  position_count INTEGER NOT NULL, total_value INTEGER NOT NULL,
  position_manager_count INTEGER NOT NULL, security_count INTEGER NOT NULL,
  manager_period_count INTEGER NOT NULL, complete_manager_count INTEGER NOT NULL,
  partial_manager_count INTEGER NOT NULL, inferred_manager_count INTEGER NOT NULL,
  notice_manager_count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE stock_changes (
  current_period_id INTEGER NOT NULL, previous_period_id INTEGER NOT NULL,
  security_id INTEGER NOT NULL, position_type INTEGER NOT NULL,
  comparable_funds INTEGER NOT NULL, adding_funds INTEGER NOT NULL,
  cutting_funds INTEGER NOT NULL, net_add INTEGER NOT NULL,
  current_funds INTEGER NOT NULL, previous_funds INTEGER NOT NULL,
  net_units REAL NOT NULL, current_value INTEGER NOT NULL, value_change INTEGER NOT NULL,
  PRIMARY KEY (current_period_id,position_type,security_id)
) WITHOUT ROWID;
CREATE TABLE period_change_totals (
  current_period_id INTEGER PRIMARY KEY, previous_period_id INTEGER NOT NULL,
  current_value INTEGER NOT NULL, previous_value INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""


def date_iso(value: str) -> str:
    return datetime.strptime(value, "%d-%b-%Y").strftime("%Y-%m-%d")


def integer(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid integer value in source data") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError("Expected an integer in source data")
    result = int(parsed)
    if result < 0 or result > 9_000_000_000_000_000_000:
        raise ValueError("Expected a non-negative integer in source data")
    return result


def number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError("Invalid numeric value in source data") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("Expected a finite non-negative number in source data")
    return result


def normalize_cik(value: str) -> str:
    normalized = value.strip()
    if not normalized.isdigit() or len(normalized) > 10:
        raise ValueError("Invalid CIK in source data")
    return normalized.zfill(10)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_ticker_map_snapshot(path: Path = TICKER_MAP) -> tuple[dict[str, str], str]:
    if not path.exists():
        return {}, ""
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        values = payload.get("tickers", payload)
        tickers = {str(cusip).strip().upper(): str(ticker).strip().upper()
                   for cusip, ticker in values.items() if ticker}
        return tickers, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid ticker map: {path}") from exc


def load_ticker_map(path: Path = TICKER_MAP) -> dict[str, str]:
    """Load just the mapping; retained as the small public helper used by scripts/tests."""
    return load_ticker_map_snapshot(path)[0]


def load_market_cap_snapshot(path: Path = MARKET_CAPS) -> dict:
    if not path.exists():
        return {"source": "", "source_page": "", "retrieved_at": "", "currency": "USD",
                "market_caps": {}, "sha256": ""}
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        values = payload.get("market_caps", {})
        if not isinstance(values, dict):
            raise ValueError("market_caps must be an object")
        market_caps = {
            str(ticker).strip().upper(): int(value)
            for ticker, value in values.items()
            if str(ticker).strip() and int(value) > 0
        }
        return {
            "source": str(payload.get("source", "")),
            "source_page": str(payload.get("source_page", "")),
            "retrieved_at": str(payload.get("retrieved_at", "")),
            "currency": str(payload.get("currency", "USD")),
            "market_caps": market_caps,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid market-cap snapshot: {path}") from exc


def load_starred_funds(path: Path = STARRED_FUNDS) -> dict:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        funds = payload.get("funds")
        sources = payload.get("sources")
        if not isinstance(funds, list) or len(funds) != 20 or not isinstance(sources, list):
            raise ValueError("watchlist must contain exactly 20 funds and a sources list")
        ciks = {normalize_cik(str(fund["cik"])) for fund in funds}
        if len(ciks) != 20:
            raise ValueError("watchlist CIKs must be unique")
        return {
            "name": str(payload.get("name", "")),
            "selected_at": str(payload.get("selected_at", "")),
            "methodology": str(payload.get("methodology", "")),
            "sources": sources,
            "funds": funds,
            "ciks": ciks,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"Invalid starred-fund watchlist: {path}") from exc


def member(zf: zipfile.ZipFile, basename: str) -> str:
    wanted = basename.upper()
    try:
        return next(name for name in zf.namelist() if name.rsplit("/", 1)[-1].upper() == wanted)
    except StopIteration as exc:
        raise ValueError(f"{zf.filename} does not contain {basename}") from exc


def rows_from_zip(zf: zipfile.ZipFile, filename: str):
    with zf.open(member(zf, filename)) as raw:
        text = (line.decode("utf-8-sig", errors="replace") for line in raw)
        yield from csv.DictReader(text, delimiter="\t")


def file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def load_archive_metadata(path: Path) -> dict:
    identity = file_identity(path)
    sha256 = file_sha256(path)
    with zipfile.ZipFile(path) as zf:
        submissions = list(rows_from_zip(zf, "SUBMISSION.tsv"))
        cover_rows = list(rows_from_zip(zf, "COVERPAGE.tsv"))
        summary_rows = list(rows_from_zip(zf, "SUMMARYPAGE.tsv"))
    for table_name, rows in (("SUBMISSION.tsv", submissions), ("COVERPAGE.tsv", cover_rows),
                             ("SUMMARYPAGE.tsv", summary_rows)):
        for row in rows:
            accession = row.get("ACCESSION_NUMBER", "").strip()
            if not accession:
                raise ValueError(f"{path} contains a {table_name} row without an accession")
            row["ACCESSION_NUMBER"] = accession
    covers = {r["ACCESSION_NUMBER"]: r for r in cover_rows}
    summaries = {r["ACCESSION_NUMBER"]: r for r in summary_rows}
    if file_identity(path) != identity:
        raise ValueError(f"Source archive changed while it was being read: {path}")
    dominant = Counter(
        r["PERIODOFREPORT"] for r in submissions if r["SUBMISSIONTYPE"] == "13F-HR"
    ).most_common(1)
    if not dominant:
        raise ValueError(f"No initial 13F-HR filings in {path}")
    return {"path": path, "main_period": dominant[0][0], "submissions": submissions,
            "covers": covers, "summaries": summaries, "sha256": sha256,
            "file_identity": identity}


def event_order(event: dict) -> tuple:
    # On an ambiguous same-day chain, apply a full base/restatement before additions.
    part_rank = 1 if event["part_type"] == "NEW HOLDINGS" else 0
    return (event["filing_date_iso"], event["amendment_no"] or 0, part_rank, event["accession"])


def canonical_groups(archives: list[dict], target_periods: set[str]) -> dict:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for archive in archives:
        for sub in archive["submissions"]:
            period = sub["PERIODOFREPORT"]
            if period not in target_periods or not sub["SUBMISSIONTYPE"].startswith("13F-"):
                continue
            accession = sub["ACCESSION_NUMBER"]
            cover = archive["covers"].get(accession, {})
            summary = archive["summaries"].get(accession, {})
            is_amendment = sub["SUBMISSIONTYPE"].endswith("/A") or cover.get("ISAMENDMENT") == "Y"
            amendment_type = cover.get("AMENDMENTTYPE", "").strip().upper()
            if not is_amendment:
                part_type = "NOTICE" if sub["SUBMISSIONTYPE"].startswith("13F-NT") else "BASE"
            elif amendment_type == "NEW HOLDINGS":
                part_type = "NEW HOLDINGS"
            elif amendment_type == "RESTATEMENT":
                part_type = "RESTATEMENT"
            else:
                part_type = "UNKNOWN AMENDMENT"
            grouped[(normalize_cik(sub["CIK"]), period)].append({
                "accession": accession, "cik": normalize_cik(sub["CIK"]), "period": period,
                "filing_date": sub["FILING_DATE"], "filing_date_iso": date_iso(sub["FILING_DATE"]),
                "submission_type": sub["SUBMISSIONTYPE"], "part_type": part_type,
                "amendment_no": integer(cover.get("AMENDMENTNO")), "cover": cover,
                "summary": summary, "source_archive": archive["path"].name,
            })
    return grouped


def effective_chain(events: list[dict]) -> tuple[list[dict], dict, str]:
    holdings = [e for e in events if e["submission_type"].startswith("13F-HR")]
    if not holdings:
        latest = max(events, key=event_order)
        return [], latest, "NOTICE"
    parts: list[dict] = []
    inferred = False
    for event in sorted(holdings, key=event_order):
        if event["part_type"] in ("BASE", "RESTATEMENT"):
            parts = [event]
        elif event["part_type"] == "NEW HOLDINGS":
            if not parts:
                inferred = True
            parts.append(event)
        else:
            # Unknown amendments are treated as a reset and explicitly marked non-comparable.
            parts = [event]
            inferred = True
    latest = max(holdings, key=event_order)
    report_type = latest["cover"].get("REPORTTYPE", "")
    confidential = any(p["summary"].get("ISCONFIDENTIALOMITTED") == "Y" for p in parts)
    has_full_base = any(p["part_type"] in ("BASE", "RESTATEMENT") for p in parts)
    if inferred or not has_full_base:
        coverage = "INFERRED"
    elif confidential or report_type != "13F HOLDINGS REPORT":
        coverage = "PARTIAL"
    else:
        coverage = "COMPLETE"
    return parts, latest, coverage


@contextmanager
def destination_build_lock(destination: Path):
    """Serialize builders for one destination without using a stale sentinel file."""
    lock_path = destination.with_name(f".{destination.name}.build.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        lock_file = os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    locked = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            lock_file.seek(0)
            holder = lock_file.read().strip()
            detail = f" ({holder})" if holder else ""
            raise SystemExit(f"Another database build is already running{detail}") from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} destination={destination}\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        yield
    finally:
        if locked:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def optional_file_sha256(path: Path) -> str:
    return file_sha256(path) if path.exists() else ""


def assert_inputs_unchanged(archives: list[dict], ticker_map_sha256: str,
                            market_cap_sha256: str, watchlist_sha256: str) -> None:
    for archive in archives:
        if file_identity(archive["path"]) != archive["file_identity"]:
            raise ValueError(f"Source archive changed during build: {archive['path']}")
        if file_sha256(archive["path"]) != archive["sha256"]:
            raise ValueError(f"Source archive content changed during build: {archive['path']}")
    snapshots = (
        (TICKER_MAP, ticker_map_sha256, "Ticker map"),
        (MARKET_CAPS, market_cap_sha256, "Market-cap snapshot"),
        (STARRED_FUNDS, watchlist_sha256, "Starred-fund watchlist"),
    )
    for path, expected, label in snapshots:
        if optional_file_sha256(path) != expected:
            raise ValueError(f"{label} changed during build: {path}")


def validate_database(con: sqlite3.Connection, expected_period_count: int) -> None:
    """Reject an internally inconsistent snapshot before it can replace the live DB."""
    checks = {
        "period count": ("SELECT count(*) FROM periods", expected_period_count),
        "manager-period stats row count": (
            "SELECT count(*) FROM manager_period_stats",
            con.execute("SELECT count(*) FROM manager_periods").fetchone()[0],
        ),
        "security-period stats row count": (
            "SELECT count(*) FROM security_period_stats",
            con.execute("""SELECT count(*) FROM (
              SELECT 1 FROM positions GROUP BY period_id,security_id)""").fetchone()[0],
        ),
        "period stats row count": ("SELECT count(*) FROM period_stats", expected_period_count),
        "period-change total row count": (
            "SELECT count(*) FROM period_change_totals", max(0, expected_period_count - 1),
        ),
        "filing-part stats row count": (
            "SELECT count(*) FROM filing_part_stats",
            con.execute("SELECT count(*) FROM effective_filing_parts").fetchone()[0],
        ),
        "orphan positions": ("""SELECT count(*) FROM positions p
          LEFT JOIN managers m ON m.id=p.manager_id
          LEFT JOIN periods q ON q.id=p.period_id
          LEFT JOIN securities s ON s.id=p.security_id
          LEFT JOIN manager_periods mp
            ON mp.manager_id=p.manager_id AND mp.period_id=p.period_id
          WHERE m.id IS NULL OR q.id IS NULL OR s.id IS NULL OR mp.manager_id IS NULL""", 0),
        "orphan effective filing parts": ("""SELECT count(*) FROM effective_filing_parts f
          LEFT JOIN manager_periods mp
            ON mp.manager_id=f.manager_id AND mp.period_id=f.period_id
          WHERE mp.manager_id IS NULL""", 0),
        "missing latest effective filing parts": ("""SELECT count(*) FROM manager_periods mp
          LEFT JOIN effective_filing_parts f ON f.accession=mp.accession
          WHERE mp.part_count>0 AND f.accession IS NULL""", 0),
        "invalid effective filing part types": ("""SELECT count(*) FROM effective_filing_parts
          WHERE part_type NOT IN ('BASE','RESTATEMENT','NEW HOLDINGS','UNKNOWN AMENDMENT')""", 0),
        "orphan filing-part stats": ("""SELECT count(*) FROM filing_part_stats s
          LEFT JOIN effective_filing_parts f ON f.accession=s.accession
          WHERE f.accession IS NULL OR f.manager_id!=s.manager_id OR f.period_id!=s.period_id""", 0),
        "orphan manager-period stats": ("""SELECT count(*) FROM manager_period_stats s
          LEFT JOIN manager_periods mp
            ON mp.manager_id=s.manager_id AND mp.period_id=s.period_id
          WHERE mp.manager_id IS NULL""", 0),
        "orphan security-period stats": ("""SELECT count(*) FROM security_period_stats s
          LEFT JOIN periods q ON q.id=s.period_id
          LEFT JOIN securities sec ON sec.id=s.security_id
          WHERE q.id IS NULL OR sec.id IS NULL""", 0),
        "orphan period stats": ("""SELECT count(*) FROM period_stats s
          LEFT JOIN periods q ON q.id=s.period_id WHERE q.id IS NULL""", 0),
        "negative position values": ("SELECT count(*) FROM positions WHERE value<0", 0),
        "negative materialized statistics": ("""SELECT
          (SELECT count(*) FROM manager_period_stats
            WHERE position_count<0 OR security_count<0 OR total_value<0)+
          (SELECT count(*) FROM security_period_stats
            WHERE position_count<0 OR manager_count<0 OR total_value<0)+
          (SELECT count(*) FROM period_stats
            WHERE position_count<0 OR total_value<0 OR position_manager_count<0
              OR security_count<0 OR manager_period_count<0 OR complete_manager_count<0
              OR partial_manager_count<0 OR inferred_manager_count<0
              OR notice_manager_count<0)""", 0),
        "invalid coverage statuses": ("""SELECT count(*) FROM manager_periods
          WHERE coverage_status NOT IN ('COMPLETE','PARTIAL','INFERRED','NOTICE')""", 0),
        "invalid summary reconciliation flags": ("""SELECT count(*) FROM filing_part_stats
          WHERE observed_entry_count<0 OR observed_value<0
            OR has_discrepancy!=(coalesce(entry_count_matches=0,0)
              OR coalesce(value_matches=0,0))""", 0),
        "summary discrepancies marked complete": ("""SELECT count(*) FROM filing_part_stats s
          JOIN manager_periods mp
            ON mp.manager_id=s.manager_id AND mp.period_id=s.period_id
          WHERE s.has_discrepancy=1 AND mp.coverage_status='COMPLETE'""", 0),
        "manager stats position total": (
            "SELECT coalesce(sum(position_count),0) FROM manager_period_stats",
            con.execute("SELECT count(*) FROM positions").fetchone()[0],
        ),
        "manager stats value total": (
            "SELECT coalesce(sum(total_value),0) FROM manager_period_stats",
            con.execute("SELECT coalesce(sum(value),0) FROM positions").fetchone()[0],
        ),
        "security stats position total": (
            "SELECT coalesce(sum(position_count),0) FROM security_period_stats",
            con.execute("SELECT count(*) FROM positions").fetchone()[0],
        ),
        "security stats value total": (
            "SELECT coalesce(sum(total_value),0) FROM security_period_stats",
            con.execute("SELECT coalesce(sum(value),0) FROM positions").fetchone()[0],
        ),
    }
    failures = []
    for label, (sql, expected) in checks.items():
        actual = con.execute(sql).fetchone()[0]
        if actual != expected:
            failures.append(f"{label}: expected {expected}, found {actual}")

    period_stat_mismatches = con.execute("""WITH actual AS (
      SELECT q.id period_id,count(p.security_id) position_count,coalesce(sum(p.value),0) total_value,
        count(DISTINCT p.manager_id) position_manager_count,
        count(DISTINCT p.security_id) security_count,
        (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id) manager_period_count,
        (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
          AND mp.coverage_status='COMPLETE') complete_manager_count,
        (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
          AND mp.coverage_status='PARTIAL') partial_manager_count,
        (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
          AND mp.coverage_status='INFERRED') inferred_manager_count,
        (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
          AND mp.coverage_status='NOTICE') notice_manager_count
      FROM periods q LEFT JOIN positions p ON p.period_id=q.id GROUP BY q.id)
      SELECT count(*) FROM (
        SELECT * FROM period_stats EXCEPT SELECT * FROM actual
      )""").fetchone()[0]
    if period_stat_mismatches:
        failures.append(f"period stats mismatches: found {period_stat_mismatches}")

    manager_stat_mismatches = con.execute("""WITH actual AS (
      SELECT mp.manager_id,mp.period_id,count(p.security_id) position_count,
        count(DISTINCT p.security_id) security_count,coalesce(sum(p.value),0) total_value
      FROM manager_periods mp LEFT JOIN positions p
        ON p.manager_id=mp.manager_id AND p.period_id=mp.period_id
      GROUP BY mp.manager_id,mp.period_id)
      SELECT count(*) FROM manager_period_stats s JOIN actual a
        ON a.manager_id=s.manager_id AND a.period_id=s.period_id
      WHERE a.position_count!=s.position_count OR a.security_count!=s.security_count
        OR a.total_value!=s.total_value""").fetchone()[0]
    if manager_stat_mismatches:
        failures.append(f"manager-period stats mismatches: found {manager_stat_mismatches}")

    security_stat_mismatches = con.execute("""WITH actual AS (
      SELECT period_id,security_id,count(*) position_count,
        count(DISTINCT manager_id) manager_count,sum(value) total_value
      FROM positions GROUP BY period_id,security_id)
      SELECT count(*) FROM security_period_stats s JOIN actual a
        ON a.period_id=s.period_id AND a.security_id=s.security_id
      WHERE a.position_count!=s.position_count OR a.manager_count!=s.manager_count
        OR a.total_value!=s.total_value""").fetchone()[0]
    if security_stat_mismatches:
        failures.append(f"security-period stats mismatches: found {security_stat_mismatches}")

    change_total_mismatches = con.execute("""SELECT count(*) FROM period_change_totals totals
      WHERE totals.current_value!=(SELECT coalesce(sum(p.value),0) FROM positions p
        JOIN manager_periods current_mp ON current_mp.manager_id=p.manager_id
          AND current_mp.period_id=totals.current_period_id
        JOIN manager_periods previous_mp ON previous_mp.manager_id=p.manager_id
          AND previous_mp.period_id=totals.previous_period_id
        WHERE p.period_id=totals.current_period_id
          AND current_mp.coverage_status='COMPLETE'
          AND previous_mp.coverage_status='COMPLETE')
      OR totals.previous_value!=(SELECT coalesce(sum(p.value),0) FROM positions p
        JOIN manager_periods current_mp ON current_mp.manager_id=p.manager_id
          AND current_mp.period_id=totals.current_period_id
        JOIN manager_periods previous_mp ON previous_mp.manager_id=p.manager_id
          AND previous_mp.period_id=totals.previous_period_id
        WHERE p.period_id=totals.previous_period_id
          AND current_mp.coverage_status='COMPLETE'
          AND previous_mp.coverage_status='COMPLETE')""").fetchone()[0]
    if change_total_mismatches:
        failures.append(f"period-change portfolio totals mismatches: found {change_total_mismatches}")

    schema_version = con.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if schema_version is None or schema_version[0] != SCHEMA_VERSION:
        failures.append("schema-version metadata is absent or incorrect")

    quick_check = [row[0] for row in con.execute("PRAGMA quick_check")]
    if quick_check != ["ok"]:
        failures.append("PRAGMA quick_check: " + "; ".join(quick_check))
    if failures:
        raise ValueError("Database validation failed: " + " | ".join(failures))


def atomic_replace(temporary: Path, destination: Path) -> None:
    with temporary.open("rb") as database_file:
        os.fsync(database_file.fileno())
    os.replace(temporary, destination)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def populate_database(sources: list[Path], con: sqlite3.Connection) -> int:
    archives = [load_archive_metadata(path) for path in sorted(sources)]
    archive_by_period = {a["main_period"]: a for a in archives}
    if len(archive_by_period) != len(archives):
        raise SystemExit("Multiple archives resolve to the same primary reporting period")
    ordered_periods = sorted(archive_by_period, key=date_iso)
    period_ids = {label: i for i, label in enumerate(ordered_periods, 1)}
    groups = canonical_groups(archives, set(ordered_periods))
    ticker_map, ticker_map_sha256 = load_ticker_map_snapshot()
    market_cap_snapshot = load_market_cap_snapshot()
    starred_funds = load_starred_funds()
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO market_caps(ticker,market_cap) VALUES (?,?)",
        market_cap_snapshot["market_caps"].items(),
    )
    for label in ordered_periods:
        con.execute("INSERT INTO periods VALUES (?,?,?,?)",
                    (period_ids[label], label, date_iso(label), archive_by_period[label]["path"].name))

    manager_ids: dict[str, int] = {}
    accession_context: dict[str, tuple[int, int]] = {}
    accession_quality: dict[str, dict] = {}
    next_manager_id = 1
    for (cik, period), events in sorted(groups.items(), key=lambda item: (date_iso(item[0][1]), item[0][0])):
        parts, latest, coverage = effective_chain(events)
        cover = latest["cover"]
        name = cover.get("FILINGMANAGER_NAME", "") or f"CIK {cik}"
        state = cover.get("FILINGMANAGER_STATEORCOUNTRY", "")
        if cik not in manager_ids:
            manager_ids[cik] = next_manager_id
            con.execute("INSERT INTO managers VALUES (?,?,?,?,?)", (
                next_manager_id, cik, name, state, int(cik in starred_funds["ciks"]),
            ))
            next_manager_id += 1
        else:
            con.execute("UPDATE managers SET name=?,state_country=? WHERE id=?", (name, state, manager_ids[cik]))
        manager_id, period_id = manager_ids[cik], period_ids[period]
        report_type = cover.get("REPORTTYPE", "")
        confidential = 1 if any(p["summary"].get("ISCONFIDENTIALOMITTED") == "Y" for p in parts) else 0
        con.execute("INSERT INTO manager_periods VALUES (?,?,?,?,?,?,?,?,?,?)", (
            manager_id, period_id, latest["accession"], latest["filing_date"], latest["filing_date_iso"],
            latest["source_archive"], report_type, coverage, confidential, len(parts),
        ))
        for part in parts:
            accession_context[part["accession"]] = (manager_id, period_id)
            summary = part["summary"]
            accession_quality[part["accession"]] = {
                "manager_id": manager_id,
                "period_id": period_id,
                "reported_entry_count": integer(summary.get("TABLEENTRYTOTAL")),
                "reported_value": integer(summary.get("TABLEVALUETOTAL")),
                "observed_entry_count": 0,
                "observed_value": 0,
            }
            con.execute("INSERT INTO effective_filing_parts VALUES (?,?,?,?)",
                        (manager_id, period_id, part["accession"], part["part_type"]))
    missing_starred = starred_funds["ciks"].difference(manager_ids)
    if missing_starred:
        raise ValueError(f"Starred manager CIKs missing from source archives: {sorted(missing_starred)}")
    con.commit()

    security_ids: dict[str, int] = {}
    next_security_id = 1
    position_sql = """INSERT INTO positions
      (manager_id,period_id,security_id,position_type,shares_type,value,shares)
      VALUES (?,?,?,?,?,?,?)
      ON CONFLICT(manager_id,period_id,security_id,position_type,shares_type) DO UPDATE SET
        value=positions.value+excluded.value,
        shares=coalesce(positions.shares,0)+coalesce(excluded.shares,0)"""

    for archive in sorted(archives, key=lambda a: min(date_iso(r["FILING_DATE"]) for r in a["submissions"])):
        path = archive["path"]
        print(f"Loading effective holdings from {path.name}…", flush=True)
        batch, loaded = [], 0
        archive_seen: set[str] = set()
        with zipfile.ZipFile(path) as zf:
            for row in rows_from_zip(zf, "INFOTABLE.tsv"):
                accession = row.get("ACCESSION_NUMBER", "").strip()
                if not accession:
                    raise ValueError(f"{path} contains an INFOTABLE.tsv row without an accession")
                context = accession_context.get(accession)
                if context is None:
                    continue
                position_value = integer(row["VALUE"]) or 0
                quality = accession_quality[accession]
                quality["observed_entry_count"] += 1
                quality["observed_value"] += position_value
                cusip = row["CUSIP"].strip().upper()
                if not cusip:
                    raise ValueError(f"{path} contains an effective holding without a CUSIP")
                if cusip not in security_ids:
                    security_id = next_security_id
                    security_ids[cusip] = security_id
                    next_security_id += 1
                    con.execute("INSERT INTO securities VALUES (?,?,?,?,?,?)", (
                        security_id, cusip, row["NAMEOFISSUER"], row["TITLEOFCLASS"], row["FIGI"],
                        ticker_map.get(cusip, ""),
                    ))
                else:
                    security_id = security_ids[cusip]
                    if cusip not in archive_seen:
                        con.execute("UPDATE securities SET issuer=?,class=?,figi=? WHERE id=?", (
                            row["NAMEOFISSUER"], row["TITLEOFCLASS"], row["FIGI"], security_id,
                        ))
                archive_seen.add(cusip)
                option = row["PUTCALL"].strip().upper()
                position_type = 1 if option == "PUT" else 2 if option == "CALL" else 0
                unit = row["SSHPRNAMTTYPE"].strip().upper()
                shares_type = 0 if unit == "SH" else 1 if unit == "PRN" else 2
                batch.append((*context, security_id, position_type, shares_type,
                              position_value, number(row["SSHPRNAMT"])))
                if len(batch) == 25_000:
                    con.executemany(position_sql, batch)
                    loaded += len(batch)
                    batch.clear()
            if batch:
                con.executemany(position_sql, batch)
                loaded += len(batch)
        con.commit()
        print(f"  applied {loaded:,} rows from effective filing parts", flush=True)

    print("Reconciling effective filing parts and materializing summary statistics…", flush=True)
    discrepant_manager_periods: set[tuple[int, int]] = set()
    for accession, quality in accession_quality.items():
        reported_entries = quality["reported_entry_count"]
        reported_value = quality["reported_value"]
        entry_matches = (None if reported_entries is None else
                         int(reported_entries == quality["observed_entry_count"]))
        value_matches = (None if reported_value is None else
                         int(reported_value == quality["observed_value"]))
        has_discrepancy = int(entry_matches == 0 or value_matches == 0)
        con.execute("INSERT INTO filing_part_stats VALUES (?,?,?,?,?,?,?,?,?,?)", (
            accession, quality["manager_id"], quality["period_id"], reported_entries,
            reported_value, quality["observed_entry_count"], quality["observed_value"],
            entry_matches, value_matches, has_discrepancy,
        ))
        if has_discrepancy:
            discrepant_manager_periods.add((quality["manager_id"], quality["period_id"]))
    coverage_downgrade_count = 0
    for manager_id, period_id in discrepant_manager_periods:
        cursor = con.execute("""UPDATE manager_periods SET coverage_status='PARTIAL'
          WHERE manager_id=? AND period_id=? AND coverage_status='COMPLETE'""",
                             (manager_id, period_id))
        coverage_downgrade_count += cursor.rowcount

    con.executescript("""
      INSERT INTO manager_period_stats
        SELECT mp.manager_id,mp.period_id,count(p.security_id),
          count(DISTINCT p.security_id),coalesce(sum(p.value),0)
        FROM manager_periods mp LEFT JOIN positions p
          ON p.manager_id=mp.manager_id AND p.period_id=mp.period_id
        GROUP BY mp.manager_id,mp.period_id;
      INSERT INTO security_period_stats
        SELECT period_id,security_id,count(*),count(DISTINCT manager_id),sum(value)
        FROM positions GROUP BY period_id,security_id;
      INSERT INTO period_stats
        SELECT q.id,count(p.security_id),coalesce(sum(p.value),0),
          count(DISTINCT p.manager_id),count(DISTINCT p.security_id),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
            AND mp.coverage_status='COMPLETE'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
            AND mp.coverage_status='PARTIAL'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
            AND mp.coverage_status='INFERRED'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id
            AND mp.coverage_status='NOTICE')
        FROM periods q LEFT JOIN positions p ON p.period_id=q.id GROUP BY q.id;
    """)
    con.commit()

    print("Creating query indexes…", flush=True)
    con.executescript("""
      CREATE INDEX managers_name_nocase ON managers(name COLLATE NOCASE);
      CREATE INDEX securities_issuer_nocase ON securities(issuer COLLATE NOCASE);
      CREATE INDEX securities_ticker_nocase ON securities(ticker COLLATE NOCASE);
      CREATE INDEX manager_periods_period ON manager_periods(period_id,manager_id);
      CREATE INDEX positions_period_value ON positions(period_id,value DESC);
      CREATE INDEX positions_security_period ON positions(security_id,period_id,manager_id);
      CREATE INDEX positions_period_security ON positions(period_id,security_id,manager_id);
      CREATE INDEX manager_period_stats_period_value
        ON manager_period_stats(period_id,total_value DESC);
      CREATE INDEX security_period_stats_period_value
        ON security_period_stats(period_id,total_value DESC);
    """)
    print("Materializing adjacent-quarter stock rankings…", flush=True)
    change_sql = """INSERT INTO stock_changes
      WITH comparable AS (
        SELECT a.manager_id FROM manager_periods a JOIN manager_periods b ON b.manager_id=a.manager_id
        WHERE a.period_id=? AND b.period_id=? AND a.coverage_status='COMPLETE' AND b.coverage_status='COMPLETE'),
      c AS (SELECT p.manager_id,p.security_id,sum(p.shares) shares,sum(p.value) value
        FROM positions p JOIN comparable x ON x.manager_id=p.manager_id
        WHERE p.period_id=? AND p.position_type=? AND p.shares_type=0 GROUP BY p.manager_id,p.security_id),
      v AS (SELECT p.manager_id,p.security_id,sum(p.shares) shares,sum(p.value) value
        FROM positions p JOIN comparable x ON x.manager_id=p.manager_id
        WHERE p.period_id=? AND p.position_type=? AND p.shares_type=0 GROUP BY p.manager_id,p.security_id),
      keys AS (SELECT manager_id,security_id FROM c UNION SELECT manager_id,security_id FROM v),
      changes AS (SELECT k.security_id,c.manager_id IS NOT NULL has_current,v.manager_id IS NOT NULL has_previous,
        coalesce(c.shares,0) current_shares,coalesce(v.shares,0) previous_shares,
        coalesce(c.value,0) current_value,coalesce(v.value,0) previous_value FROM keys k
        LEFT JOIN c USING(manager_id,security_id) LEFT JOIN v USING(manager_id,security_id))
      SELECT ?,?,security_id,?,count(*),sum(current_shares>previous_shares),
        sum(current_shares<previous_shares),sum(current_shares>previous_shares)-sum(current_shares<previous_shares),
        sum(has_current),sum(has_previous),sum(current_shares)-sum(previous_shares),
        sum(current_value),sum(current_value)-sum(previous_value)
      FROM changes GROUP BY security_id"""
    period_rows = con.execute("SELECT id FROM periods ORDER BY period_date").fetchall()
    for previous_row, current_row in zip(period_rows, period_rows[1:]):
        previous_id, current_id = previous_row[0], current_row[0]
        for position_type in (0,1,2):
            con.execute(change_sql, (current_id,previous_id,current_id,position_type,
                                     previous_id,position_type,current_id,previous_id,position_type))
        # Portfolio-weight denominators cover every reported position held by the
        # same complete, paired manager cohort.  The security ranking itself is
        # deliberately limited to SH-unit rows, but PRN/OTHER positions still
        # belong in the cohort's total reported 13F portfolio value.
        con.execute("""INSERT INTO period_change_totals
          WITH comparable AS (
            SELECT a.manager_id FROM manager_periods a
            JOIN manager_periods b ON b.manager_id=a.manager_id
            WHERE a.period_id=? AND b.period_id=?
              AND a.coverage_status='COMPLETE' AND b.coverage_status='COMPLETE')
          SELECT ?,?,
            coalesce((SELECT sum(p.value) FROM positions p JOIN comparable c
              ON c.manager_id=p.manager_id WHERE p.period_id=?),0),
            coalesce((SELECT sum(p.value) FROM positions p JOIN comparable c
              ON c.manager_id=p.manager_id WHERE p.period_id=?),0)""",
          (current_id, previous_id, current_id, previous_id, current_id, previous_id))
        con.commit()
    con.executescript("""
      CREATE INDEX stock_changes_rank ON stock_changes(current_period_id,position_type,net_add DESC);
      CREATE INDEX stock_changes_value ON stock_changes(current_period_id,position_type,current_value DESC);
      CREATE INDEX stock_changes_security ON stock_changes(position_type,security_id,current_period_id);
      INSERT INTO metadata VALUES ('snapshot_policy','Canonical effective snapshots: latest base/restatement plus later new-holdings amendments');
      INSERT INTO metadata VALUES ('summary_reconciliation_policy','Exact accession-level comparison of available SEC summary entry/value totals; discrepancies remain browseable but are non-comparable');
      INSERT INTO metadata SELECT 'period_count',printf('%d',count(*)) FROM periods;
      INSERT INTO metadata SELECT 'manager_count',printf('%d',count(*)) FROM managers;
      INSERT INTO metadata SELECT 'security_count',printf('%d',count(*)) FROM securities;
      INSERT INTO metadata SELECT 'position_count',printf('%d',count(*)) FROM positions;
      INSERT INTO metadata SELECT 'latest_period',label FROM periods ORDER BY period_date DESC LIMIT 1;
      ANALYZE;
      PRAGMA optimize;
    """)
    con.execute("INSERT INTO metadata VALUES ('schema_version',?)", (SCHEMA_VERSION,))
    con.execute("INSERT INTO metadata VALUES ('source_archives',?)",
                (json.dumps([a["path"].name for a in archives], separators=(",", ":")),))
    con.execute("INSERT INTO metadata VALUES ('source_archive_hashes',?)", (
        json.dumps([{"name": a["path"].name, "sha256": a["sha256"]} for a in archives],
                   separators=(",", ":"), sort_keys=True),
    ))
    con.execute("INSERT INTO metadata VALUES ('ticker_map_sha256',?)", (ticker_map_sha256,))
    con.execute("INSERT INTO metadata VALUES ('built_at',?)",
                (datetime.now(timezone.utc).isoformat(),))
    for key in ("source", "source_page", "retrieved_at", "currency", "sha256"):
        con.execute("INSERT INTO metadata VALUES (?,?)",
                    (f"market_cap_{key}", market_cap_snapshot[key]))
    con.execute("INSERT INTO metadata VALUES ('market_cap_count',?)",
                (str(len(market_cap_snapshot["market_caps"])),))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_name',?)", (starred_funds["name"],))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_selected_at',?)", (starred_funds["selected_at"],))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_methodology',?)", (starred_funds["methodology"],))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_sources',?)",
                (json.dumps(starred_funds["sources"], separators=(",", ":")),))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_count',?)", (str(len(starred_funds["ciks"])),))
    con.execute("INSERT INTO metadata VALUES ('fund_watchlist_sha256',?)", (starred_funds["sha256"],))
    quality_counts = {
        "data_quality_effective_filing_part_count": len(accession_quality),
        "data_quality_summary_discrepancy_part_count": con.execute(
            "SELECT count(*) FROM filing_part_stats WHERE has_discrepancy=1"
        ).fetchone()[0],
        "data_quality_summary_discrepancy_manager_period_count": len(discrepant_manager_periods),
        "data_quality_summary_entry_mismatch_part_count": con.execute(
            "SELECT count(*) FROM filing_part_stats WHERE entry_count_matches=0"
        ).fetchone()[0],
        "data_quality_summary_value_mismatch_part_count": con.execute(
            "SELECT count(*) FROM filing_part_stats WHERE value_matches=0"
        ).fetchone()[0],
        "data_quality_summary_unavailable_field_count": con.execute("""SELECT
          coalesce(sum(entry_count_matches IS NULL),0)+coalesce(sum(value_matches IS NULL),0)
          FROM filing_part_stats""").fetchone()[0],
        "data_quality_coverage_downgrade_count": coverage_downgrade_count,
        "data_quality_missing_ticker_security_count": con.execute(
            "SELECT count(*) FROM securities WHERE ticker=''"
        ).fetchone()[0],
        "data_quality_null_shares_position_count": con.execute(
            "SELECT count(*) FROM positions WHERE shares IS NULL"
        ).fetchone()[0],
        "data_quality_zero_value_position_count": con.execute(
            "SELECT count(*) FROM positions WHERE value=0"
        ).fetchone()[0],
    }
    for status in ("COMPLETE", "PARTIAL", "INFERRED", "NOTICE"):
        quality_counts[f"data_quality_{status.lower()}_manager_period_count"] = con.execute(
            "SELECT count(*) FROM manager_periods WHERE coverage_status=?", (status,)
        ).fetchone()[0]
    con.executemany("INSERT INTO metadata VALUES (?,?)",
                    ((key, str(value)) for key, value in quality_counts.items()))
    con.commit()
    validate_database(con, len(ordered_periods))
    assert_inputs_unchanged(archives, ticker_map_sha256, market_cap_snapshot["sha256"],
                            starred_funds["sha256"])
    return len(archives)


def build(sources: list[Path], destination: Path) -> None:
    if not sources:
        raise SystemExit("No *_form13f.zip archives found")
    started = time.time()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination_build_lock(destination):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".building", dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as con:
                archive_count = populate_database(sources, con)
            atomic_replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Built {destination} from {archive_count} quarters in {time.time()-started:.1f}s",
          flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", type=Path, help="ZIP archive (repeatable)")
    parser.add_argument("--source-dir", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    sources = args.source or [Path(p) for p in glob.glob(str(args.source_dir / "*_form13f.zip"))]
    if args.output.exists() and not args.force:
        print(f"Database already exists: {args.output} (use --force to rebuild)")
        sys.exit(0)
    build(sources, args.output)
