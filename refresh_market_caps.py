#!/usr/bin/env python3
"""Refresh the local, timestamped market-cap snapshot used by Quarterly Changes."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "data" / "market_caps.json"
SOURCE_NAME = "Nasdaq Stock Screener"
SOURCE_PAGE = "https://www.nasdaq.com/market-activity/stocks/screener"
SOURCE_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&offset=0&download=true"
)
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


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


def fetch_snapshot() -> dict:
    request = Request(
        SOURCE_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (compatible; 13F-Explorer/1.0)",
        },
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(read_limited(response).decode("utf-8"))
    rows = payload.get("data", {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nasdaq response did not contain stock screener rows")

    values: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        value = market_cap(row.get("marketCap"))
        if symbol and len(symbol) <= 32 and not any(ord(char) < 32 for char in symbol) and value is not None:
            values[symbol] = value
    if len(values) < 1_000:
        raise ValueError(f"Nasdaq response contained only {len(values):,} positive market caps")

    return {
        "source": SOURCE_NAME,
        "source_page": SOURCE_PAGE,
        "source_url": SOURCE_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "currency": "USD",
        "market_caps": dict(sorted(values.items())),
    }


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    snapshot = fetch_snapshot()
    write_atomic(args.output, snapshot)
    print(
        f"Saved {len(snapshot['market_caps']):,} positive USD market caps to "
        f"{args.output} at {snapshot['retrieved_at']}"
    )
