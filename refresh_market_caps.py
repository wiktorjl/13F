#!/usr/bin/env python3
"""Refresh the local, timestamped market-cap snapshot used by Quarterly Changes.

The same Nasdaq stock-screener download also carries sector and display-name
columns, so this script additionally writes the static ticker → sector/name
mapping (``data/sectors.json``) used by the dashboard, merged with the Nasdaq
ETF screener (sector ``ETF``).  The market-cap output is unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "data" / "market_caps.json"
DEFAULT_SECTORS_OUTPUT = ROOT / "data" / "sectors.json"
SOURCE_NAME = "Nasdaq Stock Screener"
SOURCE_PAGE = "https://www.nasdaq.com/market-activity/stocks/screener"
SOURCE_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&offset=0&download=true"
)
ETF_SOURCE_PAGE = "https://www.nasdaq.com/market-activity/etf/screener"
ETF_SOURCE_URL = (
    "https://api.nasdaq.com/api/screener/etf"
    "?tableonly=true&limit=10000&offset=0&download=true"
)
ETF_SECTOR = "ETF"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MIN_STOCK_SECTOR_ROWS = 1_000

# Conservative display-name cleanup for stock rows only: drop a trailing
# "(Each …)" / "(Representing …)" parenthetical (which may itself contain one
# level of nested parentheses, e.g. "ten (10) Common Shares"), then exactly one
# trailing share-class suffix.  Preferred issues are left untouched and ETF
# names are kept verbatim.
_TRAILING_PARENTHETICAL = re.compile(
    r"\s*\((?:each|representing)\b(?:[^()]|\([^()]*\))*\)\s*$",
    re.IGNORECASE,
)
_TRAILING_SUFFIX = re.compile(
    r"\s+(?:Class\s+[A-Z]\s+)?(?:"
    r"Common Stock|Common Shares|Ordinary Shares|Ordinary Share|"
    r"American Depositary Shares|American Depository Shares|Depositary Shares|"
    r"Units|Unit|Warrants|Warrant|Rights"
    r")\s*$",
    re.IGNORECASE,
)


def read_limited(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > limit:
            raise ValueError("Nasdaq response is too large")
    content_type = response.headers.get_content_type()
    if content_type not in {"application/json", "text/json", "text/plain"}:
        raise ValueError(f"Unexpected Nasdaq content type: {content_type}")
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Nasdaq response is too large")
    return payload


def market_cap(value: object) -> int | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return round(number)


def clean_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 32 or any(ord(char) < 32 for char in symbol):
        return ""
    return symbol


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clean_display_name(name: str) -> str:
    original = str(name or "")
    if "preferred" in original.lower():
        return clean_text(original) or original
    cleaned = _TRAILING_PARENTHETICAL.sub("", original)
    cleaned = _TRAILING_SUFFIX.sub("", cleaned, count=1)
    cleaned = clean_text(cleaned)
    return cleaned or clean_text(original) or original


def fetch_json(url: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (compatible; 13F-Explorer/1.0)",
        },
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(read_limited(response).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Nasdaq response was not a JSON object")
    return payload


def screener_rows(payload: dict) -> list[dict]:
    """Return the row list from any of Nasdaq's screener payload shapes."""
    # Seen in the wild: stocks use data.rows; the ETF screener nests as
    # data.data.rows (sometimes data.records.data.rows); guard a bare rows too.
    data = payload.get("data")
    candidates = []
    if isinstance(data, dict):
        candidates.append(data.get("rows"))
        for container in (data.get("data"), data.get("records")):
            if isinstance(container, dict):
                candidates.append(container.get("rows"))
                inner = container.get("data")
                if isinstance(inner, dict):
                    candidates.append(inner.get("rows"))
    candidates.append(payload.get("rows"))
    for rows in candidates:
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    return []


def fetch_stock_rows() -> list[dict]:
    rows = screener_rows(fetch_json(SOURCE_URL))
    if not rows:
        raise ValueError("Nasdaq response did not contain stock screener rows")
    return rows


def fetch_etf_rows() -> list[dict]:
    rows = screener_rows(fetch_json(ETF_SOURCE_URL))
    if not rows:
        raise ValueError("Nasdaq response did not contain ETF screener rows")
    return rows


def market_cap_snapshot(rows: list[dict], retrieved_at: str) -> dict:
    values: dict[str, int] = {}
    for row in rows:
        symbol = clean_symbol(row.get("symbol"))
        value = market_cap(row.get("marketCap"))
        if symbol and value is not None:
            values[symbol] = value
    if len(values) < 1_000:
        raise ValueError(f"Nasdaq response contained only {len(values):,} positive market caps")

    return {
        "source": SOURCE_NAME,
        "source_page": SOURCE_PAGE,
        "source_url": SOURCE_URL,
        "retrieved_at": retrieved_at,
        "currency": "USD",
        "market_caps": dict(sorted(values.items())),
    }


def sector_snapshot(stock_rows: list[dict], etf_rows: list[dict], retrieved_at: str) -> dict:
    sectors: dict[str, dict[str, str]] = {}
    for row in etf_rows:
        symbol = clean_symbol(row.get("symbol"))
        if symbol:
            name = clean_text(row.get("companyName") or row.get("name"))
            sectors[symbol] = {"sector": ETF_SECTOR, "name": name}
    stock_count = 0
    for row in stock_rows:
        symbol = clean_symbol(row.get("symbol"))
        if not symbol:
            continue
        # Stock rows win on symbol collisions with the ETF screener.
        sectors[symbol] = {
            "sector": clean_text(row.get("sector")),
            "name": clean_display_name(str(row.get("name") or "")),
        }
        stock_count += 1
    if stock_count < MIN_STOCK_SECTOR_ROWS:
        raise ValueError(f"Nasdaq response contained only {stock_count:,} stock screener symbols")
    sector_values = sorted({entry["sector"] for entry in sectors.values() if entry["sector"]})
    return {
        "source": SOURCE_NAME,
        "source_page": SOURCE_PAGE,
        "source_url": SOURCE_URL,
        "etf_source_page": ETF_SOURCE_PAGE,
        "etf_source_url": ETF_SOURCE_URL,
        "retrieved_at": retrieved_at,
        "sector_values": sector_values,
        "sectors": dict(sorted(sectors.items())),
    }


def fetch_snapshot() -> dict:
    """Fetch just the market-cap snapshot (retained for callers of the old entry point)."""
    return market_cap_snapshot(fetch_stock_rows(), datetime.now(timezone.utc).isoformat())


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sectors-output", type=Path, default=DEFAULT_SECTORS_OUTPUT)
    parser.add_argument("--skip-sectors", action="store_true",
                        help="refresh market caps only; leave the sector mapping untouched")
    args = parser.parse_args()

    retrieved_at = datetime.now(timezone.utc).isoformat()
    stock_rows = fetch_stock_rows()
    snapshot = market_cap_snapshot(stock_rows, retrieved_at)
    write_atomic(args.output, snapshot)
    print(
        f"Saved {len(snapshot['market_caps']):,} positive USD market caps to "
        f"{args.output} at {snapshot['retrieved_at']}"
    )
    if args.skip_sectors:
        return

    etf_rows: list[dict] = []
    try:
        etf_rows = fetch_etf_rows()
    except Exception as exc:  # the ETF screener is best effort; stocks alone are still useful
        print(f"Warning: ETF screener unavailable ({exc}); writing stock sectors only", file=sys.stderr)
    sectors = sector_snapshot(stock_rows, etf_rows, retrieved_at)
    write_atomic(args.sectors_output, sectors)
    etf_count = sum(entry["sector"] == ETF_SECTOR for entry in sectors["sectors"].values())
    print(
        f"Saved {len(sectors['sectors']):,} ticker sectors ({etf_count:,} ETFs, "
        f"{len(sectors['sector_values'])} sector values) to {args.sectors_output}"
    )


if __name__ == "__main__":
    main()
