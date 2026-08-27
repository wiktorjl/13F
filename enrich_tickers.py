#!/usr/bin/env python3
"""Safely enrich 13F securities with ticker symbols.

The script maintains a resumable JSON cache whose top-level ``tickers`` object is a
plain CUSIP-to-ticker mapping.  Exact CUSIP mappings from OpenFIGI are preferred.
The SEC company ticker file is used only as a conservative fallback when both
the SEC identity and the local issuer identity are unique.

Nothing is guessed when OpenFIGI returns multiple tickers.  Database updates
are opt-in and only fill blank ``securities.ticker`` values; existing values are
never overwritten.

Examples:

    # Preview the next 250 exact lookups. No network or writes.
    python3 enrich_tickers.py --dry-run --limit 250

    # Build/resume data/cusip_tickers.json (about two minutes unauthenticated).
    python3 enrich_tickers.py --limit 250

    # Also fill blank values in an existing securities.ticker column.
    python3 enrich_tickers.py --limit 250 --update-db

Set OPENFIGI_API_KEY for the authenticated OpenFIGI limits and SEC_USER_AGENT
to a descriptive organization/contact value for the SEC download.
"""

from __future__ import annotations

import argparse
import collections
from contextlib import contextmanager
import dataclasses
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "13f.sqlite"
DEFAULT_CACHE = ROOT / "data" / "cusip_tickers.json"
DEFAULT_SEC_CACHE = ROOT / "data" / "company_tickers_exchange.json"

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CACHE_VERSION = 1

UNAUTH_REQUESTS = 25
UNAUTH_WINDOW_SECONDS = 60.0
UNAUTH_MAX_JOBS = 5
AUTH_REQUESTS = 25
AUTH_WINDOW_SECONDS = 6.0
AUTH_MAX_JOBS = 100
MAX_OPENFIGI_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEC_RESPONSE_BYTES = 32 * 1024 * 1024

TERMINAL_OPENFIGI_STATUSES = {
    "resolved",
    "not_found",
    "ambiguous",
    "figi_mismatch",
    "no_ticker",
    "invalid_cusip",
    "rejected",
}
HARD_AMBIGUITY_STATUSES = {"ambiguous", "figi_mismatch"}
CUSIP_RE = re.compile(r"^[A-Z0-9*@#]{9}$")


@dataclasses.dataclass(frozen=True)
class Security:
    id: int
    cusip: str
    issuer: str
    security_class: str
    figi: str
    ticker: str
    latest_value: int
    latest_funds: int


class EnrichmentError(RuntimeError):
    """Expected command-line/runtime failure with a concise message."""


def read_response_limited(response: Any, *, limit: int, source: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > limit:
            raise EnrichmentError(f"{source} response exceeds {limit:,} bytes")
    get_content_type = getattr(response.headers, "get_content_type", None)
    content_type = (get_content_type() if get_content_type else
                    str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower())
    if content_type and content_type not in {"application/json", "text/json", "text/plain"}:
        raise EnrichmentError(f"{source} returned unexpected content type {content_type}")
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise EnrichmentError(f"{source} response exceeds {limit:,} bytes")
    return raw


@contextmanager
def exclusive_file_lock(path: Path, description: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EnrichmentError(f"Cannot open {description} lock {path}: {exc}") from exc
    stream = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EnrichmentError(f"Another process holds the {description} lock: {path}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(f"pid={os.getpid()}\n")
        stream.flush()
        os.fsync(stream.fileno())
        yield
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def normalize_cusip(value: object) -> str:
    return str(value or "").strip().upper()


def normalize_ticker(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    value = re.sub(r"\s+", " ", value)
    if not value or len(value) > 32 or any(ord(ch) < 32 for ch in value):
        return ""
    return value


def normalize_issuer(value: object) -> str:
    """Normalize presentation differences without removing meaningful words."""

    value = unicodedata.normalize("NFKD", str(value or "")).upper()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("&", " AND ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return " ".join(value.split())


def chunks(items: Sequence[Security], size: int) -> Iterable[Sequence[Security]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def connect_database(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise EnrichmentError(f"SQLite database not found: {path}")
    if writable:
        con = sqlite3.connect(path)
    else:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_securities(con: sqlite3.Connection) -> tuple[list[Security], bool]:
    if not table_exists(con, "securities"):
        raise EnrichmentError("SQLite database has no securities table")
    columns = table_columns(con, "securities")
    required = {"id", "cusip", "issuer", "class", "figi"}
    missing = sorted(required - columns)
    if missing:
        raise EnrichmentError(
            "securities table is missing required columns: " + ", ".join(missing)
        )
    has_ticker = "ticker" in columns
    ticker_sql = "COALESCE(s.ticker, '')" if has_ticker else "''"

    if table_exists(con, "positions") and table_exists(con, "periods"):
        sql = f"""
            WITH latest_period AS (
              SELECT id FROM periods ORDER BY period_date DESC LIMIT 1
            ), relevance AS (
              SELECT p.security_id,
                     COALESCE(SUM(p.value), 0) AS latest_value,
                     COUNT(DISTINCT p.manager_id) AS latest_funds
              FROM positions p
              JOIN latest_period lp ON lp.id = p.period_id
              GROUP BY p.security_id
            )
            SELECT s.id, s.cusip, s.issuer, s.class, s.figi,
                   {ticker_sql} AS ticker,
                   COALESCE(r.latest_value, 0) AS latest_value,
                   COALESCE(r.latest_funds, 0) AS latest_funds
            FROM securities s
            LEFT JOIN relevance r ON r.security_id = s.id
            ORDER BY r.latest_value IS NULL, r.latest_value DESC,
                     r.latest_funds DESC, s.cusip
        """
    else:
        sql = f"""
            SELECT s.id, s.cusip, s.issuer, s.class, s.figi,
                   {ticker_sql} AS ticker, 0 AS latest_value, 0 AS latest_funds
            FROM securities s
            ORDER BY s.id
        """

    securities = [
        Security(
            id=int(row["id"]),
            cusip=normalize_cusip(row["cusip"]),
            issuer=str(row["issuer"] or "").strip(),
            security_class=str(row["class"] or "").strip(),
            figi=str(row["figi"] or "").strip().upper(),
            ticker=normalize_ticker(row["ticker"]),
            latest_value=int(row["latest_value"] or 0),
            latest_funds=int(row["latest_funds"] or 0),
        )
        for row in con.execute(sql)
    ]
    return securities, has_ticker


def new_cache() -> dict[str, Any]:
    now = utc_now()
    return {
        "version": CACHE_VERSION,
        "created_at": now,
        "updated_at": now,
        "tickers": {},
        "records": {},
        "manual_overrides": {},
        "sources": {
            "openfigi": OPENFIGI_URL,
            "sec_company_tickers": SEC_TICKERS_URL,
        },
        "stats": {},
    }


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_cache()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"Cannot read ticker cache {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise EnrichmentError(f"Ticker cache must contain a JSON object: {path}")

    # Accept a legacy/simple {CUSIP: ticker}, {mappings: {...}}, or
    # {tickers: {...}} object without losing data.
    if "records" not in loaded:
        if isinstance(loaded.get("tickers"), dict):
            source_mapping = loaded["tickers"]
        elif isinstance(loaded.get("mappings"), dict):
            source_mapping = loaded["mappings"]
        else:
            source_mapping = loaded
        simple = {
            normalize_cusip(cusip): normalize_ticker(ticker)
            for cusip, ticker in source_mapping.items()
            if CUSIP_RE.fullmatch(normalize_cusip(cusip))
            and normalize_ticker(ticker)
        }
        loaded = new_cache()
        for cusip, ticker in simple.items():
            loaded["records"][cusip] = {
                "ticker": ticker,
                "source": "legacy_cache",
                "openfigi_status": "unqueried",
            }

    version = loaded.get("version", CACHE_VERSION)
    if version != CACHE_VERSION:
        raise EnrichmentError(
            f"Unsupported ticker cache version {version!r}; expected {CACHE_VERSION}"
        )
    if not isinstance(loaded.get("records"), dict):
        raise EnrichmentError(f"Ticker cache records must be an object: {path}")
    loaded.setdefault("manual_overrides", {})
    if not isinstance(loaded["manual_overrides"], dict):
        raise EnrichmentError(f"Ticker cache manual_overrides must be an object: {path}")
    ticker_mapping = loaded.get("tickers", loaded.get("mappings", {}))
    if not isinstance(ticker_mapping, dict):
        raise EnrichmentError(f"Ticker cache tickers must be an object: {path}")
    for raw_cusip, raw_ticker in ticker_mapping.items():
        cusip = normalize_cusip(raw_cusip)
        ticker = normalize_ticker(raw_ticker)
        if CUSIP_RE.fullmatch(cusip) and ticker:
            if cusip in loaded["manual_overrides"]:
                continue
            loaded["records"].setdefault(
                cusip,
                {
                    "ticker": ticker,
                    "source": "legacy_cache",
                    "openfigi_status": "unqueried",
                },
            )
    loaded.pop("mappings", None)
    loaded.setdefault("created_at", utc_now())
    loaded.setdefault("sources", {})
    loaded.setdefault("stats", {})
    return loaded


def sync_cache(cache: dict[str, Any]) -> None:
    records = cache["records"]
    manual_overrides = cache.setdefault("manual_overrides", {})
    tickers: dict[str, str] = {}
    statuses: collections.Counter[str] = collections.Counter()
    sources: collections.Counter[str] = collections.Counter()
    for raw_cusip, raw_record in list(records.items()):
        cusip = normalize_cusip(raw_cusip)
        if cusip != raw_cusip:
            records.pop(raw_cusip)
        if not isinstance(raw_record, dict):
            continue
        records[cusip] = raw_record
        ticker = normalize_ticker(raw_record.get("ticker"))
        if ticker:
            raw_record["ticker"] = ticker
            tickers[cusip] = ticker
            sources[str(raw_record.get("source") or "unknown")] += 1
        else:
            raw_record.pop("ticker", None)
            raw_record.pop("source", None)
        statuses[str(raw_record.get("openfigi_status") or "unqueried")] += 1
    for raw_cusip, raw_override in list(manual_overrides.items()):
        cusip = normalize_cusip(raw_cusip)
        if cusip != raw_cusip:
            manual_overrides.pop(raw_cusip)
        if not CUSIP_RE.fullmatch(cusip) or not isinstance(raw_override, dict):
            raise EnrichmentError(f"Invalid manual ticker override for {raw_cusip!r}")
        ticker = normalize_ticker(raw_override.get("ticker"))
        if not ticker:
            raise EnrichmentError(f"Manual ticker override lacks ticker for {cusip}")
        raw_override["ticker"] = ticker
        manual_overrides[cusip] = raw_override
        tickers[cusip] = ticker
        sources[str(raw_override.get("source") or "manual_verified")] += 1
    cache["tickers"] = dict(sorted(tickers.items()))
    cache.pop("mappings", None)
    cache["updated_at"] = utc_now()
    cache["sources"].update(
        {"openfigi": OPENFIGI_URL, "sec_company_tickers": SEC_TICKERS_URL}
    )
    cache["stats"] = {
        "mapped": len(tickers),
        "records": len(records),
        "by_source": dict(sorted(sources.items())),
        "openfigi_status": dict(sorted(statuses.items())),
    }


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def seed_existing_tickers(
    cache: dict[str, Any], securities: Sequence[Security]
) -> int:
    seeded = 0
    records = cache["records"]
    manual_cusips = {normalize_cusip(value) for value in cache.get("manual_overrides", {})}
    for security in securities:
        if not security.ticker:
            continue
        if security.cusip in manual_cusips:
            continue
        record = records.setdefault(security.cusip, {})
        cached = normalize_ticker(record.get("ticker"))
        if cached and cached != security.ticker:
            eprint(
                f"warning: existing DB ticker conflict for {security.cusip}: "
                f"DB={security.ticker}, cache={cached}; preserving both sources and DB"
            )
            continue
        if not cached:
            record.update(
                {
                    "ticker": security.ticker,
                    "source": "existing_db",
                    "openfigi_status": record.get("openfigi_status", "unqueried"),
                    "issuer": security.issuer,
                }
            )
            seeded += 1
    return seeded


def select_openfigi_candidates(
    securities: Sequence[Security],
    cache: dict[str, Any],
    limit: int,
    *,
    refresh: bool,
    exchange_code: str,
) -> list[Security]:
    selected: list[Security] = []
    records = cache["records"]
    for security in securities:
        if len(selected) >= limit:
            break
        record = records.get(security.cusip, {})
        status = str(record.get("openfigi_status") or "unqueried")
        source = str(record.get("source") or "")
        checked_exchange = str(record.get("openfigi_exchange") or "").upper()
        query_changed = (
            bool(record.get("checked_at") or status in TERMINAL_OPENFIGI_STATUSES)
            and checked_exchange != exchange_code
        )
        # An existing DB value with no provenance in this cache is authoritative
        # for purposes of a fill-only utility. SEC fallbacks remain eligible for
        # later exact verification on resumed runs.
        if security.ticker and source != "sec_name" and not refresh and not query_changed:
            continue
        if status in TERMINAL_OPENFIGI_STATUSES and not refresh and not query_changed:
            continue
        selected.append(security)
    return selected


class OpenFigiClient:
    def __init__(
        self,
        *,
        api_key: str,
        exchange_code: str,
        timeout: float,
        retries: int,
        verbose: bool,
    ) -> None:
        self.api_key = api_key.strip()
        self.exchange_code = exchange_code.strip().upper()
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        window = AUTH_WINDOW_SECONDS if self.api_key else UNAUTH_WINDOW_SECONDS
        requests = AUTH_REQUESTS if self.api_key else UNAUTH_REQUESTS
        self.minimum_interval = window / requests
        self.last_request_at: float | None = None

    @property
    def max_jobs(self) -> int:
        return AUTH_MAX_JOBS if self.api_key else UNAUTH_MAX_JOBS

    def _wait_for_slot(self) -> None:
        if self.last_request_at is None:
            return
        remaining = self.minimum_interval - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            if self.verbose:
                print(f"OpenFIGI rate-limit pause: {remaining:.2f}s")
            time.sleep(remaining)

    @staticmethod
    def _header_delay(headers: Any) -> float:
        for name in ("Retry-After", "ratelimit-reset"):
            raw = headers.get(name) if headers is not None else None
            if raw is None:
                continue
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return 0.0

    def map(self, securities: Sequence[Security]) -> list[dict[str, Any]]:
        jobs = [{"idType": "ID_CUSIP", "idValue": item.cusip} for item in securities]
        if self.exchange_code:
            for job in jobs:
                job["exchCode"] = self.exchange_code
        payload = json.dumps(jobs).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "13F-Atlas-ticker-enrichment/1.0",
        }
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_for_slot()
            request = urllib.request.Request(
                OPENFIGI_URL, data=payload, headers=headers, method="POST"
            )
            try:
                self.last_request_at = time.monotonic()
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = read_response_limited(
                        response, limit=MAX_OPENFIGI_RESPONSE_BYTES, source="OpenFIGI")
                    parsed = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed, list) or len(parsed) != len(securities):
                        raise EnrichmentError(
                            "OpenFIGI returned a response with the wrong number of jobs"
                        )
                    return [item if isinstance(item, dict) else {} for item in parsed]
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    detail = exc.read(500).decode("utf-8", errors="replace")
                    raise EnrichmentError(
                        f"OpenFIGI HTTP {exc.code}: {detail or exc.reason}"
                    ) from exc
                delay = max(
                    self.minimum_interval,
                    self._header_delay(exc.headers),
                    float(2**attempt),
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise EnrichmentError(f"OpenFIGI request failed: {exc}") from exc
                delay = max(self.minimum_interval, float(2**attempt))
            if self.verbose:
                print(f"Retrying OpenFIGI request in {delay:.1f}s: {last_error}")
            time.sleep(delay)
        raise EnrichmentError(f"OpenFIGI request failed: {last_error}")


def resolve_openfigi_result(
    security: Security, result: dict[str, Any], exchange_code: str
) -> dict[str, Any]:
    now = utc_now()
    base: dict[str, Any] = {
        "issuer": security.issuer,
        "class": security.security_class,
        "figi": security.figi,
        "checked_at": now,
        "openfigi_exchange": exchange_code,
    }
    error = str(result.get("error") or "").strip()
    data = result.get("data")
    if error and not isinstance(data, list):
        lowered = error.lower()
        status = "not_found" if "not found" in lowered or "no identifier" in lowered else "rejected"
        return {**base, "openfigi_status": status, "openfigi_message": error}
    if not isinstance(data, list) or not data:
        return {**base, "openfigi_status": "not_found"}

    rows = [row for row in data if isinstance(row, dict)]
    considered = rows
    if security.figi:
        considered = [
            row for row in rows if str(row.get("figi") or "").upper() == security.figi
        ]
        if not considered:
            returned_figis = sorted(
                {str(row.get("figi") or "").upper() for row in rows if row.get("figi")}
            )
            return {
                **base,
                "openfigi_status": "figi_mismatch",
                "result_count": len(rows),
                "returned_figis": returned_figis[:20],
            }

    tickers = sorted(
        {normalize_ticker(row.get("ticker")) for row in considered if normalize_ticker(row.get("ticker"))}
    )
    result_meta = {
        **base,
        "result_count": len(rows),
        "considered_count": len(considered),
    }
    if len(tickers) == 1:
        matched_figis = sorted(
            {str(row.get("figi") or "").upper() for row in considered if row.get("figi")}
        )
        return {
            **result_meta,
            "openfigi_status": "resolved",
            "ticker": tickers[0],
            "source": "openfigi",
            "matched_figis": matched_figis[:20],
        }
    if len(tickers) > 1:
        return {
            **result_meta,
            "openfigi_status": "ambiguous",
            "candidate_tickers": tickers,
        }
    return {**result_meta, "openfigi_status": "no_ticker"}


def merge_openfigi_record(
    existing: dict[str, Any], resolved: dict[str, Any]
) -> dict[str, Any]:
    status = str(resolved.get("openfigi_status") or "error")
    merged = dict(existing)
    # Remove stale diagnostic fields before recording a fresh exact lookup.
    for key in (
        "candidate_tickers",
        "returned_figis",
        "matched_figis",
        "openfigi_message",
        "result_count",
        "considered_count",
    ):
        merged.pop(key, None)
    if status == "resolved":
        merged.update(resolved)
    else:
        if merged.get("source") == "openfigi" or status in HARD_AMBIGUITY_STATUSES:
            merged.pop("ticker", None)
            merged.pop("source", None)
        merged.update({key: value for key, value in resolved.items() if key not in {"ticker", "source"}})
    return merged


def process_openfigi(
    selected: Sequence[Security],
    cache: dict[str, Any],
    cache_path: Path,
    client: OpenFigiClient,
    batch_size: int,
) -> tuple[int, bool]:
    processed = 0
    network_failed = False
    records = cache["records"]
    for batch_number, batch in enumerate(chunks(selected, batch_size), start=1):
        valid: list[Security] = []
        for security in batch:
            if CUSIP_RE.fullmatch(security.cusip):
                valid.append(security)
            else:
                existing = records.get(security.cusip, {})
                records[security.cusip] = merge_openfigi_record(
                    existing,
                    {
                        "issuer": security.issuer,
                        "class": security.security_class,
                        "figi": security.figi,
                        "checked_at": utc_now(),
                        "openfigi_exchange": client.exchange_code,
                        "openfigi_status": "invalid_cusip",
                    },
                )
                processed += 1
        if not valid:
            sync_cache(cache)
            atomic_write_json(cache_path, cache)
            continue
        print(
            f"OpenFIGI batch {batch_number}: {len(valid)} exact CUSIP lookup"
            f"{'s' if len(valid) != 1 else ''}"
        )
        try:
            results = client.map(valid)
        except EnrichmentError as exc:
            now = utc_now()
            for security in valid:
                record = records.setdefault(security.cusip, {})
                record.update(
                    {
                        "issuer": security.issuer,
                        "class": security.security_class,
                        "figi": security.figi,
                        "openfigi_status": "error",
                        "last_error": str(exc),
                        "last_attempt_at": now,
                    }
                )
            sync_cache(cache)
            atomic_write_json(cache_path, cache)
            eprint(f"warning: {exc}; stopping exact lookups so this run can resume later")
            network_failed = True
            break
        for security, result in zip(valid, results):
            resolved = resolve_openfigi_result(
                security, result, client.exchange_code
            )
            existing = records.get(security.cusip, {})
            records[security.cusip] = merge_openfigi_record(existing, resolved)
            records[security.cusip].pop("last_error", None)
            records[security.cusip].pop("last_attempt_at", None)
            processed += 1
        sync_cache(cache)
        atomic_write_json(cache_path, cache)
    return processed, network_failed


def load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"Cannot read JSON file {path}: {exc}") from exc


def load_sec_dataset(
    path: Path,
    *,
    max_age_days: float,
    user_agent: str,
    timeout: float,
    verbose: bool,
) -> Any | None:
    fresh = False
    if path.is_file():
        age_seconds = max(0.0, time.time() - path.stat().st_mtime)
        fresh = age_seconds <= max_age_days * 86400
    if fresh:
        try:
            cached = load_json_file(path)
            validate_sec_dataset(cached)
            if verbose:
                print(f"Using cached SEC company ticker file: {path}")
            return cached
        except EnrichmentError as exc:
            eprint(f"warning: cached SEC company ticker file is invalid ({exc}); refreshing")

    request = urllib.request.Request(
        SEC_TICKERS_URL,
        headers={"Accept": "application/json", "User-Agent": user_agent},
    )
    try:
        print("Downloading official SEC company_tickers_exchange.json …")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = read_response_limited(
                response, limit=MAX_SEC_RESPONSE_BYTES, source="SEC company ticker")
        parsed = json.loads(raw.decode("utf-8-sig"))
        validate_sec_dataset(parsed)
        atomic_write_bytes(path, raw)
        return parsed
    except (OSError, urllib.error.URLError, json.JSONDecodeError, EnrichmentError) as exc:
        if path.is_file():
            eprint(f"warning: SEC download failed ({exc}); using stale cache {path}")
            cached = load_json_file(path)
            validate_sec_dataset(cached)
            return cached
        eprint(f"warning: SEC company ticker supplement unavailable: {exc}")
        return None


def sec_rows(dataset: Any) -> Iterable[tuple[str, str, str]]:
    """Yield (CIK, name, ticker) across the SEC file's documented shapes."""

    if isinstance(dataset, dict) and isinstance(dataset.get("fields"), list):
        fields = [str(value).strip().lower() for value in dataset["fields"]]
        try:
            cik_index = fields.index("cik")
            name_index = fields.index("name")
            ticker_index = fields.index("ticker")
        except ValueError as exc:
            raise EnrichmentError(
                "SEC company ticker file lacks cik/name/ticker fields"
            ) from exc
        for row in dataset.get("data", []):
            if not isinstance(row, list) or len(row) <= max(cik_index, name_index, ticker_index):
                continue
            yield str(row[cik_index] or ""), str(row[name_index] or ""), str(row[ticker_index] or "")
        return

    records: Iterable[Any]
    if isinstance(dataset, list):
        records = dataset
    elif isinstance(dataset, dict):
        records = dataset.values()
    else:
        raise EnrichmentError("Unsupported SEC company ticker JSON shape")
    for row in records:
        if not isinstance(row, dict):
            continue
        yield (
            str(row.get("cik") or row.get("cik_str") or ""),
            str(row.get("name") or row.get("title") or ""),
            str(row.get("ticker") or ""),
        )


def validate_sec_dataset(dataset: Any) -> None:
    if not isinstance(dataset, (dict, list)):
        raise EnrichmentError("SEC company ticker response is not a JSON object/array")
    valid_rows = set()
    for cik, name, ticker in sec_rows(dataset):
        normalized_cik = cik.strip().lstrip("0") or "0"
        normalized_ticker = normalize_ticker(ticker)
        if normalized_cik.isdigit() and name.strip() and normalized_ticker:
            valid_rows.add((normalized_cik, normalized_ticker))
    if len(valid_rows) < 1_000:
        raise EnrichmentError(
            f"SEC company ticker response has only {len(valid_rows):,} valid unique identities")


def build_safe_sec_name_index(dataset: Any) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, set[str]]] = {}
    original_names: dict[str, str] = {}
    for cik, name, ticker_value in sec_rows(dataset):
        normalized_name = normalize_issuer(name)
        ticker = normalize_ticker(ticker_value)
        normalized_cik = str(cik).strip().lstrip("0") or "0"
        if not normalized_name or not ticker:
            continue
        identity = identities.setdefault(
            normalized_name, {"tickers": set(), "ciks": set()}
        )
        identity["tickers"].add(ticker)
        identity["ciks"].add(normalized_cik)
        original_names.setdefault(normalized_name, name.strip())

    safe: dict[str, dict[str, str]] = {}
    for normalized_name, identity in identities.items():
        if len(identity["tickers"]) != 1 or len(identity["ciks"]) != 1:
            continue
        safe[normalized_name] = {
            "ticker": next(iter(identity["tickers"])),
            "cik": next(iter(identity["ciks"])),
            "name": original_names[normalized_name],
        }
    return safe


def supplement_from_sec(
    securities: Sequence[Security], cache: dict[str, Any], dataset: Any
) -> int:
    safe_sec = build_safe_sec_name_index(dataset)
    local_names: dict[str, set[str]] = collections.defaultdict(set)
    for security in securities:
        normalized = normalize_issuer(security.issuer)
        if normalized:
            local_names[normalized].add(security.cusip)

    records = cache["records"]
    # Re-evaluate prior SEC-name mappings against the current official file.
    for record in records.values():
        if isinstance(record, dict) and record.get("source") == "sec_name":
            record.pop("ticker", None)
            record.pop("source", None)
            record.pop("sec_cik", None)
            record.pop("sec_name", None)

    added = 0
    for security in securities:
        normalized = normalize_issuer(security.issuer)
        if not normalized or len(local_names[normalized]) != 1:
            continue
        sec_match = safe_sec.get(normalized)
        if not sec_match:
            continue
        record = records.setdefault(
            security.cusip,
            {
                "issuer": security.issuer,
                "class": security.security_class,
                "figi": security.figi,
                "openfigi_status": "unqueried",
            },
        )
        status = str(record.get("openfigi_status") or "unqueried")
        if status == "resolved" or status in HARD_AMBIGUITY_STATUSES:
            continue
        existing = normalize_ticker(record.get("ticker"))
        if existing and record.get("source") != "sec_name":
            continue
        record.update(
            {
                "ticker": sec_match["ticker"],
                "source": "sec_name",
                "sec_cik": sec_match["cik"],
                "sec_name": sec_match["name"],
                "sec_matched_at": utc_now(),
                "issuer": security.issuer,
                "class": security.security_class,
                "figi": security.figi,
            }
        )
        added += 1
    return added


def database_update_counts(
    con: sqlite3.Connection, mappings: dict[str, str]
) -> tuple[list[tuple[str, str]], int, int]:
    rows = {
        normalize_cusip(row["cusip"]): normalize_ticker(row["ticker"])
        for row in con.execute("SELECT cusip, ticker FROM securities")
    }
    updates: list[tuple[str, str]] = []
    already = 0
    conflicts = 0
    for cusip, raw_ticker in mappings.items():
        ticker = normalize_ticker(raw_ticker)
        if cusip not in rows or not ticker:
            continue
        current = rows[cusip]
        if not current:
            updates.append((ticker, cusip))
        elif current == ticker:
            already += 1
        else:
            conflicts += 1
            eprint(
                f"warning: refusing to overwrite {cusip}: DB={current}, cache={ticker}"
            )
    return updates, already, conflicts


def update_database(path: Path, mappings: dict[str, str], *, dry_run: bool) -> None:
    con = connect_database(path, writable=not dry_run)
    try:
        if "ticker" not in table_columns(con, "securities"):
            message = (
                "securities.ticker does not exist; rebuild/migrate the application "
                "schema before using --update-db"
            )
            if dry_run:
                print(f"Database preview: {message}")
                return
            raise EnrichmentError(message)
        updates, already, conflicts = database_update_counts(con, mappings)
        if dry_run:
            print(
                f"Database preview: {len(updates):,} blank tickers would be filled; "
                f"{already:,} already match; {conflicts:,} conflicts would be preserved"
            )
            return
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            """
            UPDATE securities SET ticker=?
            WHERE cusip=? AND (ticker IS NULL OR TRIM(ticker)='')
            """,
            updates,
        )
        con.commit()
        print(
            f"Database updated: {len(updates):,} tickers filled; "
            f"{already:,} already matched; {conflicts:,} conflicts preserved"
        )
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def print_dry_run(
    args: argparse.Namespace,
    securities: Sequence[Security],
    cache: dict[str, Any],
    selected: Sequence[Security],
    has_ticker: bool,
) -> None:
    sync_cache(cache)
    batches = (len(selected) + args.batch_size - 1) // args.batch_size if selected else 0
    print("DRY RUN — no network requests or files/database writes will occur")
    print(f"Database: {args.db}")
    print(f"Securities: {len(securities):,}; ticker column: {'yes' if has_ticker else 'no'}")
    print(
        f"Cache: {args.cache} ({len(cache['tickers']):,} mappings, "
        f"{len(cache['records']):,} progress records)"
    )
    print(
        f"OpenFIGI: would query {len(selected):,} prioritized exact CUSIPs "
        f"in {batches:,} batch{'es' if batches != 1 else ''}"
    )
    for security in selected[:10]:
        print(
            f"  {security.cusip}  ${security.latest_value:,}  "
            f"{security.latest_funds:,} funds  {security.issuer}"
        )
    if len(selected) > 10:
        print(f"  … and {len(selected) - 10:,} more")
    if args.skip_sec:
        print("SEC supplement: skipped")
    elif args.sec_cache.is_file():
        print(f"SEC supplement: would evaluate cached file {args.sec_cache}")
    else:
        print(f"SEC supplement: would download {SEC_TICKERS_URL}")
    if args.update_db:
        update_database(args.db, cache["tickers"], dry_run=True)


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build/resume a conservative CUSIP→ticker cache using exact "
            "OpenFIGI mappings and unique official SEC issuer-name fallbacks."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="13F SQLite database")
    parser.add_argument(
        "--cache", type=Path, default=DEFAULT_CACHE, help="persistent enrichment JSON"
    )
    parser.add_argument(
        "--sec-cache",
        type=Path,
        default=DEFAULT_SEC_CACHE,
        help="cached official SEC company ticker JSON",
    )
    parser.add_argument(
        "--limit",
        type=nonnegative_int,
        default=250,
        help="maximum prioritized OpenFIGI CUSIPs attempted this run; 0 disables exact lookups",
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=None,
        help="jobs per OpenFIGI request (automatically capped at 5 unauthenticated or 100 authenticated)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="OpenFIGI API key; preferably set OPENFIGI_API_KEY",
    )
    parser.add_argument(
        "--exchange-code",
        default="US",
        help=(
            "OpenFIGI exchange filter; US avoids ambiguous foreign listings for "
            "13F CUSIPs (pass an empty value to disable)"
        ),
    )
    parser.add_argument(
        "--sec-user-agent",
        default=os.environ.get(
            "SEC_USER_AGENT", "13F Atlas local research ticker-enrichment@localhost"
        ),
        help="descriptive organization/contact User-Agent for SEC requests",
    )
    parser.add_argument(
        "--sec-max-age-days",
        type=float,
        default=7.0,
        help="reuse the downloaded SEC file for this many days",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--retries", type=nonnegative_int, default=3, help="retries for transient OpenFIGI errors"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="recheck terminal OpenFIGI results, still bounded by --limit",
    )
    parser.add_argument(
        "--skip-sec", action="store_true", help="do not download/apply the SEC fallback"
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="fill blank securities.ticker values from the final cache",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview candidates and cached DB updates without network or writes",
    )
    parser.add_argument("--verbose", action="store_true", help="show cache and rate-limit details")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise EnrichmentError("--timeout must be greater than zero")
    if args.sec_max_age_days < 0:
        raise EnrichmentError("--sec-max-age-days must be zero or greater")

    con = connect_database(args.db)
    try:
        securities, has_ticker = load_securities(con)
    finally:
        con.close()
    cache = load_cache(args.cache)
    seeded = seed_existing_tickers(cache, securities)

    api_key = args.api_key if args.api_key is not None else os.environ.get("OPENFIGI_API_KEY", "")
    exchange_code = args.exchange_code.strip().upper()
    client = OpenFigiClient(
        api_key=api_key,
        exchange_code=exchange_code,
        timeout=args.timeout,
        retries=args.retries,
        verbose=args.verbose,
    )
    requested_batch_size = args.batch_size or client.max_jobs
    args.batch_size = min(requested_batch_size, client.max_jobs)
    if requested_batch_size > client.max_jobs:
        print(
            f"Capping --batch-size at {client.max_jobs} for "
            f"{'authenticated' if api_key else 'unauthenticated'} OpenFIGI"
        )
    selected = select_openfigi_candidates(
        securities,
        cache,
        args.limit,
        refresh=args.refresh,
        exchange_code=exchange_code,
    )

    if args.dry_run:
        print_dry_run(args, securities, cache, selected, has_ticker)
        return 0

    if args.update_db and not has_ticker:
        raise EnrichmentError(
            "securities.ticker does not exist; rebuild/migrate the application "
            "schema before using --update-db"
        )
    if seeded and args.verbose:
        print(f"Seeded {seeded:,} cache mappings from existing database tickers")

    processed = 0
    network_failed = False
    if selected:
        processed, network_failed = process_openfigi(
            selected, cache, args.cache, client, args.batch_size
        )
    else:
        print("No OpenFIGI candidates need checking in this run")

    sec_added = 0
    if not args.skip_sec:
        dataset = load_sec_dataset(
            args.sec_cache,
            max_age_days=args.sec_max_age_days,
            user_agent=args.sec_user_agent,
            timeout=args.timeout,
            verbose=args.verbose,
        )
        if dataset is not None:
            sec_added = supplement_from_sec(securities, cache, dataset)
            print(f"SEC unique-name supplement retained {sec_added:,} safe mappings")

    cache.setdefault("last_run", {})
    cache["last_run"] = {
        "at": utc_now(),
        "database": str(args.db.resolve()),
        "securities": len(securities),
        "openfigi_selected": len(selected),
        "openfigi_processed": processed,
        "openfigi_authenticated": bool(api_key),
        "openfigi_exchange": exchange_code,
        "openfigi_network_failed": network_failed,
        "sec_safe_matches": sec_added,
    }
    sync_cache(cache)
    atomic_write_json(args.cache, cache)
    stats = cache["stats"]
    print(
        f"Ticker cache: {stats['mapped']:,} mappings, {stats['records']:,} progress "
        f"records → {args.cache}"
    )

    if args.update_db:
        database_lock = args.db.with_name(f".{args.db.name}.build.lock")
        with exclusive_file_lock(database_lock, "database build/write"):
            update_database(args.db, cache["tickers"], dry_run=False)
    else:
        print("Database unchanged (pass --update-db to fill blank securities.ticker values)")
    return 1 if network_failed else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.dry_run:
            return run(args)
        cache_lock = args.cache.with_name(f".{args.cache.name}.lock")
        with exclusive_file_lock(cache_lock, "ticker cache"):
            return run(args)
    except (EnrichmentError, sqlite3.Error) as exc:
        eprint(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
