#!/usr/bin/env python3
"""Zero-dependency local web server for the multi-quarter 13F dashboard."""

from __future__ import annotations

import argparse
from contextlib import closing
import glob
import hashlib
import json
import mimetypes
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "13f.sqlite"
MARKET_CAPS = ROOT / "data" / "market_caps.json"
STARRED_FUNDS = ROOT / "data" / "starred_funds.json"
TICKER_MAP = ROOT / "data" / "cusip_tickers.json"
SECTORS = ROOT / "data" / "sectors.json"
PRICE_CACHE = ROOT / "data" / "prices.sqlite"
SCHEMA_VERSION = "9"
# Public URL prefix ("" at the root, "/13f" behind a path-routing proxy).  Set once at
# startup from --base-path / $BASE_PATH; request_path() and render_document() read it.
BASE_PATH = ""
BASE_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*")
# Where the *_form13f.zip archives live for freshness hashing; a container that ships
# only data/ points this at a mount (or runs with --trust-database and never hashes).
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR") or ROOT)
STATIC_PATHS = {"/dashboard.html", "/dashboard.js", "/dashboard.css"}
HTML_DOCUMENTS = frozenset({"dashboard.html"})
# href="/..." and src="/..." in the served document; "//host" and data: URLs never match.
ROOT_RELATIVE_ATTRIBUTE = re.compile(r'\b(href|src)="(/[^"]*)"')
# The dashboard is the whole site: "/" (Top Holdings), "/initiations", "/movers", and
# "/about".  The pre-landing-page "/dashboard…" URLs stay as aliases so old links keep
# working (dashboard.js rewrites them to the canonical path).
DASHBOARD_ROUTES = {"/", "/initiations", "/movers", "/about"}
DASHBOARD_ALIASES = {"/dashboard", "/dashboard/initiations", "/dashboard/movers"}
DASHBOARD_VIEWS = frozenset({"holdings", "initiations", "movers"})
DASHBOARD_SIDES = frozenset({"gainers", "losers"})
DASHBOARD_MAX_HORIZON = 4
DASHBOARD_SORTS = frozenset({"metric", "ticker", "name", "price", "day", "ytd", "sector"})
DASHBOARD_PRICE_SORTS = frozenset({"price", "day", "ytd"})
DASHBOARD_UNMAPPED = frozenset({"exclude", "include"})
SORT_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
# ORDER BY fragments for /api/dashboard, keyed by the validated ``sort`` value; user
# text is never interpolated.  Text and price keys put NULLs (blank tickers, no
# screener row, unpriced symbols) last in both directions; the metric key keeps
# the view's own tie-breakers, which follow every other key as the default order.
DASHBOARD_SORT_SQL = {
    "ticker": "nullif(ticker,'') COLLATE NOCASE {direction} NULLS LAST",
    "name": "nullif(name,'') COLLATE NOCASE {direction} NULLS LAST",
    "sector": "nullif(sector,'') COLLATE NOCASE {direction} NULLS LAST",
    "price": "sort_price {direction} NULLS LAST",
    "day": "sort_day {direction} NULLS LAST",
    "ytd": "sort_ytd {direction} NULLS LAST",
}
DASHBOARD_TIE_BREAKERS = {"holdings": "holders DESC,cusip ASC", "initiations": "avg_weight DESC,cusip ASC",
                          "movers": "cusip ASC"}
MAX_QUERY_LENGTH = 8_192
MAX_PARAMETER_LENGTH = 256
MAX_PAGE = 100_000
MAX_CONNECTIONS = 64
CONNECTION_TIMEOUT_SECONDS = 15
API_SLOTS = threading.BoundedSemaphore(4)
API_PARAMETERS = {
    "/api/meta": frozenset(),
    "/api/dashboard": frozenset({"view", "horizon", "side", "sort", "direction", "page", "size", "unmapped"}),
}
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


TRUST_DATABASE = env_flag("TRUST_DATABASE")


def normalize_base_path(value: str) -> str:
    """Canonical public prefix: "" for the root, otherwise "/name[/name...]" without a trailing slash."""
    value = str(value).strip()
    if value in ("", "/"):
        return ""
    if value.endswith("/"):
        value = value[:-1]
    if not BASE_PATH_PATTERN.fullmatch(value):
        raise ValueError(
            "Base path must be empty or look like /prefix or /a/b "
            "(letters, digits, '.', '_', '~', '-'; no trailing slash)")
    return value


def request_path(path: str) -> str:
    """Strip the public prefix from a request path.

    ``/13f`` and ``/13f/`` are the root; ``/13f/x`` becomes ``/x``.  Anything else
    is returned unchanged, so a proxy that strips the prefix itself keeps working
    and look-alikes such as ``/13f-other`` fall through to the allowlist and 404.
    """
    if not BASE_PATH:
        return path
    if path == BASE_PATH or path == BASE_PATH + "/":
        return "/"
    if path.startswith(BASE_PATH + "/"):
        return path[len(BASE_PATH):]
    return path


FINGERPRINTED_ASSETS = frozenset({"/dashboard.js", "/dashboard.css"})
_asset_versions: dict[str, tuple[tuple[int, int], str]] = {}


def asset_version(name: str) -> str:
    """Short content hash of a static asset, cached by size and mtime; '' when the file is absent.

    Appended as ``?v=`` to the asset URLs in the served HTML so a new deployment
    never shows a stylesheet or script that a browser or CDN cached earlier.
    """
    path = ROOT / name
    try:
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
        cached = _asset_versions.get(name)
        if cached is not None and cached[0] == key:
            return cached[1]
        version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""
    _asset_versions[name] = (key, version)
    return version


def render_document(name: str) -> bytes:
    """Read dashboard.html, fingerprinting its assets and prefixing root-absolute values under BASE_PATH.

    ``/dashboard.js`` and ``/dashboard.css`` gain ``?v=<content hash>``; every other
    value that starts with exactly one slash gains the prefix when BASE_PATH is set.
    Protocol-relative ``//host``, ``data:`` URLs, relative queries, and values already
    carrying the prefix are left alone.  The static allowlist ignores query strings.
    """
    if name not in HTML_DOCUMENTS:
        raise ValueError(f"Not a served document: {name}")
    raw = (ROOT / name).read_bytes()

    def rewrite(match: re.Match) -> str:
        attribute, value = match.group(1), match.group(2)
        if value.startswith("//") or value == BASE_PATH or (BASE_PATH and value.startswith(BASE_PATH + "/")):
            return match.group(0)
        if value in FINGERPRINTED_ASSETS:
            version = asset_version(value[1:])
            return f'{attribute}="{BASE_PATH}{value}{"?v=" + version if version else ""}"'
        return f'{attribute}="{BASE_PATH}{value}"'

    return ROOT_RELATIVE_ATTRIBUTE.sub(rewrite, raw.decode("utf-8")).encode("utf-8")


EMPTY_PRICE_SCHEMA = """
CREATE TABLE prices.bars (
  symbol TEXT NOT NULL COLLATE NOCASE, price_date TEXT NOT NULL, close REAL NOT NULL,
  PRIMARY KEY(symbol,price_date)
) WITHOUT ROWID;
CREATE TABLE prices.metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
INSERT INTO prices.metadata VALUES ('available','0');
"""
PRICE_BAR_COLUMNS = frozenset({"symbol", "price_date", "close"})


def price_cache_problem(path: Path | None = None) -> str:
    """'' when the price cache can be attached; otherwise a one-line reason for the operator."""
    path = path or PRICE_CACHE
    if not path.is_file():
        return f"{path} is missing (run `make signals`, or copy data/prices.sqlite next to the database)"
    if not os.access(path, os.R_OK):
        return f"{path} is not readable by uid {os.getuid()} (check ownership/permissions of data/)"
    try:
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as cache:
            columns = {row[1] for row in cache.execute("PRAGMA table_info(bars)")}
            tables = {row[0] for row in cache.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('bars','metadata')")}
    except (OSError, sqlite3.Error) as exc:
        return f"{path} could not be opened: {exc}"
    if not (PRICE_BAR_COLUMNS.issubset(columns) and tables == {"bars", "metadata"}):
        return f"{path} does not have the expected bars/metadata schema"
    return ""


def price_cache_is_usable(path: Path | None = None) -> bool:
    return not price_cache_problem(path)


def attach_price_cache(con: sqlite3.Connection) -> bool:
    """ATTACH the offline price cache read-only as ``prices`` (True) or an empty stand-in (False).

    Must run before the request's BEGIN: the stand-in schema is created with
    ``query_only`` lifted for the in-memory database only, and executescript
    ends any open transaction.  Only the dashboard handler attaches prices;
    ``/api/meta`` never sees the extra schema.
    """
    if price_cache_is_usable():
        try:
            con.execute("ATTACH DATABASE ? AS prices", (f"file:{PRICE_CACHE.resolve()}?mode=ro",))
            return True
        except sqlite3.Error:
            pass
    con.execute("PRAGMA query_only=OFF")
    try:
        con.execute("ATTACH DATABASE ':memory:' AS prices")
        con.executescript(EMPTY_PRICE_SCHEMA)
    finally:
        con.execute("PRAGMA query_only=ON")
    return False


def price_mark(con: sqlite3.Connection) -> tuple[str, str]:
    """Return (mark_date, source) of the attached price cache; ('', '') when unusable."""
    metadata = dict(con.execute("SELECT key,value FROM prices.metadata"))
    if metadata.get("available") == "0":
        return "", ""
    mark_date = str(metadata.get("mark_date", ""))
    if not ISO_DATE.fullmatch(mark_date):
        mark_date = str(con.execute("SELECT coalesce(max(price_date),'') FROM prices.bars").fetchone()[0])
    if not ISO_DATE.fullmatch(mark_date):
        return "", ""
    return mark_date, str(metadata.get("source", ""))


def percent_change(current, base) -> float | None:
    if current is None or base is None or base <= 0:
        return None
    return round(100.0 * (current / base - 1.0), 4)


def price_fields(con: sqlite3.Connection, ticker: str, mark_date: str) -> dict:
    """Close on the mark date plus day and year-to-date changes; None for anything unpriced."""
    fields = {"price": None, "price_date": "", "day_change": None, "ytd_change": None}
    if not ticker or not mark_date:
        return fields
    previous_year = int(mark_date[:4]) - 1
    row = con.execute("""SELECT
      (SELECT close FROM prices.bars WHERE symbol=:symbol AND price_date=:mark) mark_close,
      (SELECT close FROM prices.bars WHERE symbol=:symbol AND price_date<:mark
         ORDER BY price_date DESC LIMIT 1) previous_close,
      (SELECT close FROM prices.bars WHERE symbol=:symbol AND price_date BETWEEN :base_start AND :base_end
         ORDER BY price_date DESC LIMIT 1) base_close""",
      {"symbol": ticker, "mark": mark_date,
       "base_start": f"{previous_year}-12-01", "base_end": f"{previous_year}-12-31"}).fetchone()
    if row[0] is None:
        return fields
    fields.update({"price": row[0], "price_date": mark_date,
                   "day_change": percent_change(row[0], row[1]),
                   "ytd_change": percent_change(row[0], row[2])})
    return fields


def db() -> sqlite3.Connection:
    # Use normal SQLite read locking rather than immutable=1: ticker enrichment
    # can intentionally update the live database, and immutable readers are not
    # allowed to overlap a writer safely.
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA cache_size=-32768")
    con.execute("PRAGMA mmap_size=268435456")
    return con


def param(params, name, default=""):
    value = str(params.get(name, [default])[0]).strip()
    if len(value) > MAX_PARAMETER_LENGTH or any(ord(char) < 32 for char in value):
        raise ValueError(f"Invalid {name.replace('_', ' ')}")
    return value


def reject_unknown_params(params, allowed) -> None:
    unknown = sorted(set(params).difference(allowed))
    if unknown:
        label = "parameter" if len(unknown) == 1 else "parameters"
        raise ValueError(f"Unknown query {label}: {', '.join(unknown)}")


def bounded_int(params, name, default, *, minimum, maximum):
    raw = param(params, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {name.replace('_', ' ')}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name.replace('_', ' ')}")
    return value


def enum_param(params, name, choices, default=""):
    value = param(params, name, default)
    if value not in choices:
        raise ValueError(f"Invalid {name.replace('_', ' ')}")
    return value


def page_params(params, *, minimum_size=10, default_size=50):
    page = bounded_int(params, "page", 1, minimum=1, maximum=MAX_PAGE)
    size = bounded_int(params, "size", default_size, minimum=minimum_size, maximum=200)
    return page, size


def dashboard_params(params) -> dict:
    """Validate every dashboard parameter; horizon/side are checked for all views but only steer movers.

    ``direction`` defaults to ``desc`` except for movers losers, whose default
    list runs most-negative first, so every view's default order is unchanged
    by the optional sort parameters.
    """
    view = enum_param(params, "view", DASHBOARD_VIEWS, "holdings")
    horizon = bounded_int(params, "horizon", 1, minimum=1, maximum=DASHBOARD_MAX_HORIZON)
    side = enum_param(params, "side", DASHBOARD_SIDES, "gainers")
    sort = enum_param(params, "sort", DASHBOARD_SORTS, "metric")
    default_direction = "asc" if view == "movers" and side == "losers" else "desc"
    direction = enum_param(params, "direction", SORT_DIRECTIONS, default_direction)
    unmapped = enum_param(params, "unmapped", DASHBOARD_UNMAPPED, "exclude")
    page, size = page_params(params, default_size=100)
    return {"view": view, "horizon": horizon, "side": side, "sort": sort, "direction": direction,
            "unmapped": unmapped, "page": page, "size": size}


def dashboard_order(options: dict, *, priced: bool) -> str:
    """ORDER BY for the validated dashboard options, composed only from the fixed fragments above.

    The default (``sort=metric`` at the default direction) is exactly the view's
    historical order.  A price sort without a usable price cache has nothing to
    sort by (every key is NULL) and falls back to that default order.
    """
    view, sort, direction = options["view"], options["sort"], options["direction"]
    losers = view == "movers" and options["side"] == "losers"
    ties = DASHBOARD_TIE_BREAKERS[view]
    default_order = f"metric {'ASC' if losers else 'DESC'},{ties}"
    if sort == "metric":
        return f"metric {SORT_DIRECTIONS[direction]},{ties}"
    if sort in DASHBOARD_PRICE_SORTS and not priced:
        return default_order
    return DASHBOARD_SORT_SQL[sort].format(direction=SORT_DIRECTIONS[direction]) + "," + default_order


def static_path_for(path: str) -> str | None:
    """Map a request path onto the exact static allowlist; anything else is 404.

    The dashboard answers at the root, its three sub-paths, and the legacy
    ``/dashboard…`` aliases.  Trailing slashes and case variants are not routes.
    """
    if path in DASHBOARD_ROUTES or path in DASHBOARD_ALIASES:
        return "/dashboard.html"
    return path if path in STATIC_PATHS else None


class Handler(SimpleHTTPRequestHandler):
    server_version = "13FDashboard/1.0"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_response(self, code, message=None):
        self._cache_control_sent = False
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._cache_control_sent = True
        super().send_header(keyword, value)

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        # Every non-API response revalidates: JS/CSS/HTML go stale across deploys
        # otherwise.  API responses set no-store themselves before reaching here.
        if not getattr(self, "_cache_control_sent", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def json_response(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def send_document(self, name: str) -> None:
        body = render_document(name)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def serve_static(self, path: str):
        """Serve a prefix-free path: HTML through render_document, JS/CSS through the stdlib handler."""
        static = static_path_for(path)
        if static is None:
            self.send_error(404, "Not found")
            return
        if static[1:] in HTML_DOCUMENTS:
            self.send_document(static[1:])
            return
        self.path = static
        if self.command == "HEAD":
            return super().do_HEAD()
        return super().do_GET()

    def do_HEAD(self):
        path = request_path(urlparse(self.path).path)
        if path.startswith("/api/"):
            self.json_response({"error": "Method not allowed"}, 405)
            return
        self.serve_static(path)

    def do_GET(self):
        if len(self.path) > MAX_QUERY_LENGTH:
            self.json_response({"error": "Request target is too long"}, 414)
            return
        parsed = urlparse(self.path)
        path = request_path(parsed.path)
        if path.startswith("/api/"):
            try:
                params = parse_qs(parsed.query, max_num_fields=64)
                with API_SLOTS:
                    self.handle_api(path, params)
            except ValueError as exc:
                self.json_response({"error": str(exc)}, 400)
            except sqlite3.Error as exc:
                self.log_error("Database error: %s", exc)
                self.json_response({"error": "The database request could not be completed"}, 500)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception as exc:
                self.log_error("Unexpected API error: %s", exc)
                self.json_response({"error": "The request could not be completed"}, 500)
            return
        self.serve_static(path)

    def handle_api(self, path, params):
        allowed = API_PARAMETERS.get(path)
        if allowed is None:
            self.json_response({"error": "Not found"}, 404)
            return
        reject_unknown_params(params, allowed)
        con = db()
        try:
            price_available = attach_price_cache(con) if path == "/api/dashboard" else False
            # Keep multi-query responses (rows, counts, summaries) on one
            # coherent snapshot if ticker enrichment overlaps a request.
            con.execute("BEGIN")
            if path == "/api/meta":
                self.meta(con)
            elif path == "/api/dashboard":
                self.dashboard(con, params, price_available)
        finally:
            con.close()

    def meta(self, con):
        """Flat build summary: the metadata rows plus the period list and latest-quarter totals.

        The Docker healthcheck and the dashboard's About tab read this; the
        latest-quarter counts come from the ``period_stats`` rollup.
        """
        meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM metadata")}
        periods = [dict(r) for r in con.execute("SELECT label,period_date FROM periods ORDER BY period_date DESC")]
        if not periods:
            raise ValueError("The database does not contain any reporting periods")
        snapshot = con.execute("""SELECT ps.position_count,ps.total_value,
          ps.position_manager_count,ps.security_count FROM period_stats ps
          JOIN periods q ON q.id=ps.period_id ORDER BY q.period_date DESC LIMIT 1""").fetchone()
        if snapshot is None:
            raise sqlite3.DatabaseError("Missing period statistics")
        meta.update({"periods": periods, "latest_period": periods[0]["label"], "period_count": len(periods),
                     "holding_count": snapshot[0], "total_value": snapshot[1],
                     "distinct_managers": snapshot[2], "distinct_issuers": snapshot[3]})
        self.json_response(meta)

    def dashboard(self, con, params, price_available: bool):
        options = dashboard_params(params)
        view, horizon, side = options["view"], options["horizon"], options["side"]
        page, size = options["page"], options["size"]
        mark = price_mark(con) if price_available else ("", "")
        current = con.execute(
            "SELECT id,label,period_date FROM periods ORDER BY period_date DESC LIMIT 1").fetchone()
        if current is None:
            raise ValueError("The database does not contain any reporting periods")
        comparison = con.execute(
            "SELECT id,label,period_date FROM periods ORDER BY period_date DESC LIMIT 1 OFFSET ?",
            (horizon if view == "movers" else 1,)).fetchone()
        comparison_id = comparison["id"] if comparison is not None else None
        # Every view resolves display names and sectors the same way: the
        # cleaned screener name when the ticker is known, else the SEC issuer.
        naming = """JOIN securities s ON s.id=r.security_id
          LEFT JOIN sectors sec ON s.ticker!='' AND sec.ticker=s.ticker"""
        columns = """s.cusip,s.ticker,s.issuer,coalesce(nullif(sec.name,''),s.issuer) name,
          coalesce(sec.sector,'') sector"""
        values = {"cur": current["id"], "prev": comparison_id}
        # Securities without a ticker are hidden unless unmapped=include; the clause
        # sits inside every ranked CTE so count, sort, and paging all see the filter.
        mapped = "" if options["unmapped"] == "include" else " AND s.ticker!=''"
        if view == "holdings":
            cte = f"""WITH ranked AS (SELECT {columns},
              CASE WHEN :prev IS NULL THEN 'flat'
                WHEN r.avg_weight>coalesce(prev.avg_weight,0) THEN 'up'
                WHEN r.avg_weight<coalesce(prev.avg_weight,0) THEN 'down' ELSE 'flat' END direction,
              r.avg_weight metric,r.holder_count holders
              FROM security_weight_stats r {naming}
              LEFT JOIN security_weight_stats prev ON prev.period_id=:prev AND prev.security_id=r.security_id
              WHERE r.period_id=:cur{mapped})"""
        elif view == "initiations":
            # Direction compares this quarter's first-time holders with the security's
            # count in the prior quarter (no prior row means zero new holders then).
            cte = f"""WITH ranked AS (SELECT {columns},
              CASE WHEN :prev IS NULL THEN 'flat'
                WHEN r.new_holder_count>coalesce(prev.new_holder_count,0) THEN 'up'
                WHEN r.new_holder_count<coalesce(prev.new_holder_count,0) THEN 'down' ELSE 'flat' END direction,
              r.new_holder_count metric,r.holder_count holders,r.avg_weight
              FROM security_weight_stats r {naming}
              LEFT JOIN security_weight_stats prev ON prev.period_id=:prev AND prev.security_id=r.security_id
              WHERE r.period_id=:cur AND r.new_holder_count>0{mapped})"""
        else:
            if comparison is None:
                self.dashboard_response(con, options, current, None, [], 0, mark)
                return
            # Union of securities with a stats row in either period; a missing
            # side is a zero weight.  Zero changes belong to neither side.
            cte = f"""WITH changes AS (
              SELECT cur.security_id,cur.avg_weight-coalesce(prev.avg_weight,0) change,cur.holder_count holders
              FROM security_weight_stats cur
              LEFT JOIN security_weight_stats prev ON prev.period_id=:prev AND prev.security_id=cur.security_id
              WHERE cur.period_id=:cur
              UNION ALL
              SELECT prev.security_id,-prev.avg_weight,0 FROM security_weight_stats prev
              WHERE prev.period_id=:prev AND NOT EXISTS (SELECT 1 FROM security_weight_stats cur
                WHERE cur.period_id=:cur AND cur.security_id=prev.security_id)),
            ranked AS (SELECT {columns},:direction direction,r.change metric,r.holders
              FROM changes r {naming} WHERE r.change{'>' if side == 'gainers' else '<'}0{mapped})"""
            values["direction"] = "up" if side == "gainers" else "down"
        source = "ranked"
        priced = options["sort"] in DASHBOARD_PRICE_SORTS and bool(mark[0])
        if priced:
            # Sort keys with price_fields() semantics for the whole result set: the
            # close on the mark date, and its ratio to the previous close and to the
            # prior-December base.  Blank tickers and unpriced symbols stay NULL.
            # Only a price sort pays for this join; the response rows still get
            # their price fields from price_fields() one page at a time.
            previous_year = int(mark[0][:4]) - 1
            values.update({"mark": mark[0], "base_start": f"{previous_year}-12-01",
                           "base_end": f"{previous_year}-12-31"})
            cte += """,
            priced AS (SELECT *,
              CASE WHEN previous_close>0 THEN sort_price/previous_close END sort_day,
              CASE WHEN base_close>0 THEN sort_price/base_close END sort_ytd
              FROM (SELECT ranked.*,
                CASE WHEN ticker!='' THEN (SELECT b.close FROM prices.bars b
                  WHERE b.symbol=ranked.ticker AND b.price_date=:mark) END sort_price,
                CASE WHEN ticker!='' THEN (SELECT b.close FROM prices.bars b
                  WHERE b.symbol=ranked.ticker AND b.price_date<:mark
                  ORDER BY b.price_date DESC LIMIT 1) END previous_close,
                CASE WHEN ticker!='' THEN (SELECT b.close FROM prices.bars b
                  WHERE b.symbol=ranked.ticker AND b.price_date BETWEEN :base_start AND :base_end
                  ORDER BY b.price_date DESC LIMIT 1) END base_close
                FROM ranked))"""
            source = "priced"
        order = dashboard_order(options, priced=priced)
        sql = cte + f" SELECT *,count(*) OVER() total_count FROM {source} ORDER BY {order} LIMIT :size OFFSET :offset"
        rows = [dict(row) for row in con.execute(sql, {**values, "size": size, "offset": (page - 1) * size})]
        count = rows[0]["total_count"] if rows else con.execute(
            cte + " SELECT count(*) FROM ranked", values).fetchone()[0]
        self.dashboard_response(con, options, current, comparison, rows, count, mark)

    def dashboard_response(self, con, options, current, comparison, rows, count, mark):
        mark_date, price_source = mark
        for row in rows:
            for helper in ("total_count", "avg_weight", "sort_price", "sort_day", "sort_ytd",
                           "previous_close", "base_close"):
                row.pop(helper, None)
            if options["view"] != "initiations":
                row["metric"] = round(row["metric"], 4)
            row.update(price_fields(con, row["ticker"], mark_date))
        self.json_response({
            "view": options["view"], "period": current["label"], "period_date": current["period_date"],
            "comparison_period": comparison["label"] if comparison is not None else None,
            "comparison_period_date": comparison["period_date"] if comparison is not None else None,
            "horizon": options["horizon"], "side": options["side"],
            "sort": options["sort"], "direction": options["direction"], "unmapped": options["unmapped"],
            "price_date": mark_date, "price_source": price_source,
            "price_available": bool(mark_date),
            "rows": rows, "count": count, "page": options["page"], "size": options["size"],
        })


def database_is_current(trust: bool = False) -> bool:
    """Strict (default): metadata digests must match every archive in ARCHIVE_DIR and every JSON input.

    ``trust=True`` (--trust-database) checks only that the database exists and
    carries SCHEMA_VERSION: a container that ships data/ but not the archives
    cannot re-hash them.  verify.py always uses the strict check.
    """
    if not DB.exists():
        return False
    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as con:
            metadata = dict(con.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema_version") != SCHEMA_VERSION:
            return False
        if trust:
            return True
        archive_paths = sorted((Path(path) for path in glob.glob(str(ARCHIVE_DIR / "*_form13f.zip"))),
                               key=lambda path: path.name)
        archive_names = [path.name for path in archive_paths]
        archive_hashes = [{"name": path.name, "sha256": sha256_file(path)} for path in archive_paths]
        market_cap_hash = sha256_file(MARKET_CAPS) if MARKET_CAPS.exists() else ""
        watchlist_hash = sha256_file(STARRED_FUNDS) if STARRED_FUNDS.exists() else ""
        ticker_map_hash = sha256_file(TICKER_MAP) if TICKER_MAP.exists() else ""
        sector_hash = sha256_file(SECTORS) if SECTORS.exists() else ""
        return (json.loads(metadata.get("source_archives", "[]")) == archive_names
                and json.loads(metadata.get("source_archive_hashes", "[]")) == archive_hashes
                and metadata.get("ticker_map_sha256", "") == ticker_map_hash
                and metadata.get("market_cap_sha256", "") == market_cap_hash
                and metadata.get("fund_watchlist_sha256", "") == watchlist_hash
                and metadata.get("sector_sha256", "") == sector_hash)
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
        return False


class ExplorerHTTPServer(ThreadingHTTPServer):
    """Bounded threading server (the class name predates the dashboard; tests/support.py uses it)."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(CONNECTION_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class IPv6ExplorerHTTPServer(ExplorerHTTPServer):
    address_family = socket.AF_INET6


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description="Serve the local 13F Dashboard")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8013)
    parser.add_argument("--no-build",action="store_true",help="Never rebuild; require a current database")
    parser.add_argument("--base-path",default=os.environ.get("BASE_PATH",""),
                        help="Public URL prefix such as /13f (default: $BASE_PATH, else the root)")
    parser.add_argument("--trust-database",action="store_true",default=TRUST_DATABASE,
                        help="Skip archive/JSON hashing; require only an existing database with the "
                             "current schema version (default: $TRUST_DATABASE=1). Implies --no-build.")
    args=parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        BASE_PATH = normalize_base_path(args.base_path)
    except ValueError as exc:
        parser.error(f"--base-path: {exc}")
    if args.trust_database:
        print("NOTICE: trusting data/13f.sqlite without re-hashing source archives (--trust-database)",
              file=sys.stderr)
        if not database_is_current(trust=True):
            raise SystemExit(f"Database is missing or not schema version {SCHEMA_VERSION}: {DB}")
    else:
        if not database_is_current() and not args.no_build:
            subprocess.run([sys.executable,str(ROOT/"build_database.py"),"--force"],check=True)
        if not database_is_current():
            raise SystemExit("Database is missing or stale. Run: python3 build_database.py --force")
    mimetypes.add_type("text/javascript",".js")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: the dashboard is being exposed beyond this computer. Use only on a trusted network.",
              file=sys.stderr)
    server_class = IPv6ExplorerHTTPServer if ":" in args.host else ExplorerHTTPServer
    server=server_class((args.host,args.port),Handler)
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    problem = price_cache_problem()
    if problem:
        print(f"NOTICE: price columns will show '—': {problem}", file=sys.stderr, flush=True)
    print(f"13F Dashboard is running at http://{display_host}:{args.port}{BASE_PATH}/", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
