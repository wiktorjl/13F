#!/usr/bin/env python3
"""Zero-dependency local web server for the multi-quarter 13F explorer."""

from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import glob
import hashlib
import json
import mimetypes
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
FUND_SIGNALS = ROOT / "data" / "fund_signals.sqlite"
MARKET_CAPS = ROOT / "data" / "market_caps.json"
STARRED_FUNDS = ROOT / "data" / "starred_funds.json"
TICKER_MAP = ROOT / "data" / "cusip_tickers.json"
SCHEMA_VERSION = "8"
STATIC_PATHS = {"/index.html", "/app.js", "/styles.css"}
MAX_QUERY_LENGTH = 8_192
MAX_PARAMETER_LENGTH = 256
MAX_PAGE = 100_000
MAX_SQLITE_INTEGER = 9_000_000_000_000_000_000
MAX_CONNECTIONS = 64
CONNECTION_TIMEOUT_SECONDS = 15
API_SLOTS = threading.BoundedSemaphore(4)
FILTER_PARAMETERS = frozenset({
    "q", "manager", "issuer", "cusip", "period", "form", "put_call", "state",
    "amendments", "min_value", "max_value",
})
API_PARAMETERS = {
    "/api/meta": frozenset(),
    "/api/holdings": FILTER_PARAMETERS | {"page", "size", "sort"},
    "/api/aggregate": FILTER_PARAMETERS | {"group", "page", "size", "limit", "sort", "direction"},
    "/api/funds": FILTER_PARAMETERS | {
        "fund_q", "starred", "scope", "page", "size", "sort", "direction",
    },
    "/api/suggest": frozenset({"kind", "q"}),
    "/api/stock-detail": frozenset({
        "cusip", "period", "page", "size", "sort", "direction", "fund_q", "change", "position",
    }),
    "/api/fund-detail": frozenset({
        "cik", "period", "page", "size", "sort", "direction", "security_q", "change", "position",
    }),
    "/api/net-adds": frozenset({
        "position", "metric", "min_activity", "min_adding_funds", "max_adding_funds",
        "min_cutting_funds", "max_cutting_funds", "min_market_cap", "max_market_cap",
        "stock_q", "page", "size", "sort", "direction",
    }),
}


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


EMPTY_SIGNAL_SCHEMA = """
CREATE TABLE signals.fund_scores (
  cik TEXT NOT NULL, period TEXT NOT NULL, previous_period TEXT,
  filing_date TEXT, reference_date TEXT, mark_date TEXT,
  signal_return REAL, raw_signal_return REAL,
  signal_pnl REAL, raw_signal_pnl REAL, gross_notional REAL,
  candidate_events INTEGER NOT NULL, eligible_events INTEGER NOT NULL,
  priced_events INTEGER NOT NULL, buy_events INTEGER NOT NULL,
  sell_events INTEGER NOT NULL, hit_events INTEGER NOT NULL,
  signal_coverage REAL, effective_bets REAL,
  rankable INTEGER NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(cik,period)
) WITHOUT ROWID;
CREATE TABLE signals.scope_values (
  cik TEXT NOT NULL, period TEXT NOT NULL,
  raw_reported_value INTEGER NOT NULL, value_scale INTEGER NOT NULL,
  scope_value INTEGER NOT NULL, evidence_count INTEGER NOT NULL,
  median_value_ratio REAL, scale_inferred INTEGER NOT NULL,
  PRIMARY KEY(cik,period)
) WITHOUT ROWID;
CREATE TABLE signals.metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
INSERT INTO signals.metadata VALUES ('available','0');
"""


def signal_snapshot_is_current(con: sqlite3.Connection, path: Path | None = None) -> bool:
    path = path or FUND_SIGNALS
    if not path.exists():
        return False
    try:
        main = dict(con.execute(
            "SELECT key,value FROM metadata WHERE key IN "
            "('schema_version','source_archive_hashes','ticker_map_sha256')"
        ))
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as sidecar:
            signal = dict(sidecar.execute("SELECT key,value FROM metadata"))
        return (
            signal.get("available") == "1"
            and signal.get("main_schema_version") == main.get("schema_version", "")
            and signal.get("main_source_archive_hashes") == main.get("source_archive_hashes", "[]")
            and signal.get("main_ticker_map_sha256") == main.get("ticker_map_sha256", "")
        )
    except (OSError, sqlite3.Error):
        return False


def attach_signal_snapshot(con: sqlite3.Connection) -> None:
    if signal_snapshot_is_current(con):
        try:
            uri = f"file:{FUND_SIGNALS.resolve()}?mode=ro"
            con.execute("ATTACH DATABASE ? AS signals", (uri,))
            return
        except sqlite3.Error:
            pass
    con.execute("ATTACH DATABASE ':memory:' AS signals")
    con.executescript(EMPTY_SIGNAL_SCHEMA)


def signal_metadata(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key,value FROM signals.metadata"))


def db() -> sqlite3.Connection:
    # Use normal SQLite read locking rather than immutable=1: ticker enrichment
    # can intentionally update the live database, and immutable readers are not
    # allowed to overlap a writer safely.
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    attach_signal_snapshot(con)
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


def direction_sql(params) -> str:
    return "ASC" if enum_param(params, "direction", {"asc", "desc"}, "desc") == "asc" else "DESC"


def like_value(value: str, *, prefix=False) -> str:
    """Treat search input literally rather than as user-controlled SQL wildcards."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%" if prefix else f"%{escaped}%"


def nonnegative_bound(params, name, *, integer=False):
    raw = param(params, name)
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {name.replace('_', ' ')}") from exc
    if (not value.is_finite() or value < 0 or value > MAX_SQLITE_INTEGER
            or (integer and value != value.to_integral_value())):
        raise ValueError(f"Invalid {name.replace('_', ' ')}")
    return int(value) if integer else float(value)


def validate_range(minimum, maximum, label):
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"Minimum {label} cannot exceed maximum {label}")


def latest_period(con) -> str:
    row = con.execute("SELECT label FROM periods ORDER BY period_date DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("The database does not contain any reporting periods")
    return row[0]


def default_period_params(con, params):
    result = {key: list(value) for key, value in params.items()}
    if not param(result, "period"):
        result["period"] = [latest_period(con)]
    return result


def filters(params, *, include_search=True):
    clauses, values = [], []
    if include_search and (search := param(params, "q")):
        clauses.append("(s.ticker LIKE ? ESCAPE '\\' OR s.issuer LIKE ? ESCAPE '\\' "
                       "OR s.cusip LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\' "
                       "OR m.cik LIKE ? ESCAPE '\\')")
        needle = like_value(search)
        values.extend([needle] * 5)
    if manager := param(params, "manager"):
        clauses.append("m.name LIKE ? ESCAPE '\\'")
        values.append(like_value(manager))
    if issuer := param(params, "issuer"):
        clauses.append("(s.ticker LIKE ? ESCAPE '\\' OR s.issuer LIKE ? ESCAPE '\\')")
        values.extend([like_value(issuer)] * 2)
    if cusip := param(params, "cusip"):
        clauses.append("s.cusip LIKE ? ESCAPE '\\'")
        values.append(like_value(cusip.upper(), prefix=True))
    if period := param(params, "period"):
        if period == "ALL":
            raise ValueError("All-quarter totals are not supported; choose one reporting period")
        clauses.append("q.label = ?")
        values.append(period)
    if form := param(params, "form"):
        if form != "13F-HR":
            raise ValueError("Invalid form")
    if position := param(params, "put_call"):
        code = {"SHARES": 0, "PUT": 1, "CALL": 2}.get(position.upper())
        if code is None:
            raise ValueError("Invalid position type")
        clauses.append("p.position_type = ?")
        values.append(code)
    if state := param(params, "state"):
        clauses.append("m.state_country = ?")
        values.append(state)
    amendments = enum_param(params, "amendments", {"", "only"})
    if amendments == "only":
        clauses.append("fp.part_type!='BASE'")
    minimum = nonnegative_bound(params, "min_value", integer=True)
    maximum = nonnegative_bound(params, "max_value", integer=True)
    validate_range(minimum, maximum, "position value")
    if minimum is not None:
        clauses.append("p.value >= ?")
        values.append(minimum)
    if maximum is not None:
        clauses.append("p.value <= ?")
        values.append(maximum)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), values


BASE = """ FROM positions p
  JOIN managers m ON m.id=p.manager_id
  JOIN securities s ON s.id=p.security_id
  JOIN periods q ON q.id=p.period_id
  JOIN manager_periods mp ON mp.manager_id=p.manager_id AND mp.period_id=p.period_id
  JOIN effective_filing_parts fp ON fp.accession=mp.accession """


def period_pair(con, requested: str) -> tuple[sqlite3.Row, sqlite3.Row | None]:
    current = con.execute(
        "SELECT * FROM periods WHERE label=? OR period_date=?", (requested, requested)
    ).fetchone()
    if current is None:
        raise ValueError("Unknown reporting period")
    previous = con.execute(
        "SELECT * FROM periods WHERE period_date < ? ORDER BY period_date DESC LIMIT 1",
        (current["period_date"],),
    ).fetchone()
    return current, previous


def comparison_status_sql(current="c", previous="v") -> str:
    return f"""CASE
      WHEN cm.coverage_status!='COMPLETE' OR vm.coverage_status!='COMPLETE'
        OR cm.manager_id IS NULL OR vm.manager_id IS NULL THEN 'NOT_COMPARABLE'
      WHEN {current}.manager_id IS NULL THEN 'EXITED'
      WHEN {previous}.manager_id IS NULL THEN 'NEW'
      WHEN coalesce({current}.shares,0)>coalesce({previous}.shares,0) THEN 'INCREASED'
      WHEN coalesce({current}.shares,0)<coalesce({previous}.shares,0) THEN 'REDUCED'
      ELSE 'UNCHANGED' END"""


class Handler(SimpleHTTPRequestHandler):
    server_version = "13FExplorer/1.0"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
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

    def _static_path(self) -> str | None:
        path = urlparse(self.path).path
        if path == "/":
            return "/index.html"
        return path if path in STATIC_PATHS else None

    def do_HEAD(self):
        if urlparse(self.path).path.startswith("/api/"):
            self.json_response({"error": "Method not allowed"}, 405)
            return
        path = self._static_path()
        if path is None:
            self.send_error(404, "Not found")
            return
        self.path = path
        return super().do_HEAD()

    def do_GET(self):
        if len(self.path) > MAX_QUERY_LENGTH:
            self.json_response({"error": "Request target is too long"}, 414)
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            try:
                params = parse_qs(parsed.query, max_num_fields=64)
                with API_SLOTS:
                    self.handle_api(parsed.path, params)
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
        path = self._static_path()
        if path is None:
            self.send_error(404, "Not found")
            return
        self.path = path
        return super().do_GET()

    def handle_api(self, path, params):
        allowed = API_PARAMETERS.get(path)
        if allowed is None:
            self.json_response({"error": "Not found"}, 404)
            return
        reject_unknown_params(params, allowed)
        con = db()
        try:
            # Keep multi-query responses (rows, counts, summaries) on one
            # coherent snapshot if ticker enrichment overlaps a request.
            con.execute("BEGIN")
            if path == "/api/meta":
                self.meta(con)
            elif path == "/api/holdings":
                self.holdings(con, default_period_params(con, params))
            elif path == "/api/aggregate":
                self.aggregate(con, default_period_params(con, params))
            elif path == "/api/funds":
                self.funds(con, default_period_params(con, params))
            elif path == "/api/suggest":
                self.suggest(con, params)
            elif path == "/api/stock-detail":
                self.stock_detail(con, params)
            elif path == "/api/fund-detail":
                self.fund_detail(con, params)
            elif path == "/api/net-adds":
                self.net_adds(con, params)
        finally:
            con.close()

    def meta(self, con):
        meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM metadata")}
        signal = signal_metadata(con)
        periods = [dict(r) for r in con.execute("SELECT label,period_date FROM periods ORDER BY period_date DESC")]
        if not periods:
            raise ValueError("The database does not contain any reporting periods")
        snapshot = con.execute("""SELECT ps.position_count,ps.total_value,
          ps.position_manager_count,ps.security_count FROM period_stats ps
          JOIN periods q ON q.id=ps.period_id ORDER BY q.period_date DESC LIMIT 1""").fetchone()
        if snapshot is None:
            raise sqlite3.DatabaseError("Missing period statistics")
        meta.update({"periods": periods, "latest_period": periods[0]["label"], "forms": ["13F-HR"],
                     "states": [r[0] for r in con.execute("SELECT DISTINCT state_country FROM managers WHERE state_country!='' ORDER BY 1")],
                     "holding_count": snapshot[0], "total_value": snapshot[1],
                     "distinct_managers": snapshot[2], "distinct_issuers": snapshot[3]})
        meta.update({
            "signal_available": signal.get("available", "0"),
            "signal_price_source": signal.get("price_source", ""),
            "signal_price_date": signal.get("mark_date", ""),
            "signal_metric_version": signal.get("metric_version", ""),
            "signal_reference_policy": signal.get("reference_policy", ""),
            "signal_ranking_policy": signal.get("ranking_policy", ""),
            "research_fund_cutoff": signal.get("research_cutoff", "1000000000"),
        })
        self.json_response(meta)

    def holdings(self, con, params):
        where, values = filters(params)
        page, size = page_params(params)
        sorts = {
            "value_desc": "p.value DESC", "value_asc": "p.value ASC",
            "issuer_asc": "coalesce(nullif(s.ticker,''),s.issuer) COLLATE NOCASE ASC,p.value DESC", "issuer_desc": "coalesce(nullif(s.ticker,''),s.issuer) COLLATE NOCASE DESC,p.value DESC",
            "class_asc": "s.class COLLATE NOCASE ASC,p.value DESC", "class_desc": "s.class COLLATE NOCASE DESC,p.value DESC",
            "cusip_asc": "s.cusip ASC,p.value DESC", "cusip_desc": "s.cusip DESC,p.value DESC",
            "manager_asc": "m.name COLLATE NOCASE ASC,p.value DESC", "manager_desc": "m.name COLLATE NOCASE DESC,p.value DESC",
            "cik_asc": "m.cik ASC,p.value DESC", "cik_desc": "m.cik DESC,p.value DESC",
            "period_asc": "q.period_date ASC,p.value DESC", "period_desc": "q.period_date DESC,p.value DESC",
            "position_asc": "p.position_type ASC,p.value DESC", "position_desc": "p.position_type DESC,p.value DESC",
            "shares_asc": "p.shares ASC", "shares_desc": "p.shares DESC",
            "filing_date_asc": "mp.filing_date_iso ASC,p.value DESC", "filing_date_desc": "mp.filing_date_iso DESC,p.value DESC",
            "form_asc": "mp.coverage_status ASC,p.value DESC", "form_desc": "mp.coverage_status DESC,p.value DESC",
        }
        order = sorts.get(param(params, "sort", "value_desc"), "p.value DESC")
        sql = f"""SELECT printf('%d-%d-%d-%d-%d',p.manager_id,p.period_id,p.security_id,p.position_type,p.shares_type) id,
          s.ticker,s.issuer,s.class,s.cusip,p.value,p.shares,
          CASE p.shares_type WHEN 0 THEN 'SH' WHEN 1 THEN 'PRN' ELSE 'OTHER' END shares_type,
          CASE p.position_type WHEN 1 THEN 'Put' WHEN 2 THEN 'Call' ELSE '' END put_call,
          '' discretion,m.name manager_name,m.cik,m.starred,q.label period,mp.filing_date,
          CASE WHEN fp.part_type='BASE' THEN '13F-HR' ELSE '13F-HR/A' END submission_type,
          mp.accession,(fp.part_type!='BASE') is_amendment,mp.coverage_status
          {BASE}{where} ORDER BY {order},p.manager_id,p.period_id,p.security_id,
          p.position_type,p.shares_type LIMIT ? OFFSET ?"""
        rows = [dict(r) for r in con.execute(sql, [*values, size, (page-1)*size])]
        simple_period = set(params).issubset({"period", "page", "size", "sort"})
        if simple_period:
            stats = con.execute("""SELECT ps.position_count,ps.total_value,
              ps.position_manager_count,ps.security_count FROM period_stats ps
              JOIN periods q ON q.id=ps.period_id WHERE q.label=?""", (param(params, "period"),)).fetchone()
            if stats is None:
                raise ValueError("Unknown reporting period")
            count, totals = stats["position_count"], (
                stats["total_value"], stats["position_manager_count"], stats["security_count"])
        else:
            count = con.execute("SELECT count(*)" + BASE + where, values).fetchone()[0]
            totals = con.execute("SELECT coalesce(sum(p.value),0),count(DISTINCT m.id),count(DISTINCT s.id)" + BASE + where, values).fetchone()
        self.json_response({"rows": rows, "count": count, "page": page, "size": size,
                            "value": totals[0], "managers": totals[1], "issuers": totals[2]})

    def aggregate(self, con, params):
        groups = {
            "issuer": ("s.issuer", "s.cusip", "Issuer"), "manager": ("m.name", "m.cik", "Manager"),
            "period": ("q.label", "q.period_date", "Period"),
            "state": ("coalesce(nullif(m.state_country,''),'Unknown')", "m.state_country", "State / country"),
            "put_call": ("CASE p.position_type WHEN 1 THEN 'Long PUT' WHEN 2 THEN 'Long CALL' ELSE 'Long shares / other' END", "p.position_type", "Position type"),
        }
        group = param(params, "group", "issuer")
        if group not in groups:
            raise ValueError("Invalid aggregation")
        if group == "issuer" and set(params).issubset(
                {"period", "group", "page", "size", "limit", "sort", "direction"}):
            self.aggregate_period_securities(con, params)
            return
        expression, key, label = groups[group]
        ticker = "max(s.ticker)" if group == "issuer" else "''"
        starred = "max(m.starred)" if group == "manager" else "0"
        where, values = filters(params)
        page = bounded_int(params, "page", 1, minimum=1, maximum=MAX_PAGE)
        size = bounded_int(params, "size", param(params, "limit", "50"), minimum=5, maximum=200)
        direction = direction_sql(params)
        sortable = {
            "name": "coalesce(nullif(ticker,''),name) COLLATE NOCASE", "key": "key COLLATE NOCASE",
            "positions": "positions", "managers": "managers", "issuers": "issuers", "value": "value",
        }
        order = sortable.get(param(params, "sort", "value"), "value")
        cte = f"""WITH grouped AS (SELECT {expression} name,{key} key,{ticker} ticker,{starred} starred,
          sum(p.value) value,count(*) positions,count(DISTINCT m.id) managers,
          count(DISTINCT s.id) issuers {BASE}{where} GROUP BY {expression},{key})"""
        sql = cte + f""" SELECT *,count(*) OVER() total_count FROM grouped
          ORDER BY {order} {direction},name COLLATE NOCASE ASC,key ASC LIMIT ? OFFSET ?"""
        rows = [dict(row) for row in con.execute(sql, [*values, size, (page-1)*size])]
        count = rows[0].pop("total_count") if rows else con.execute(
            cte + " SELECT count(*) FROM grouped", values).fetchone()[0]
        for row in rows[1:]:
            row.pop("total_count", None)
        self.json_response({"group": group, "label": label, "rows": rows, "count": count,
                            "page": page, "size": size})

    def aggregate_period_securities(self, con, params):
        page = bounded_int(params, "page", 1, minimum=1, maximum=MAX_PAGE)
        size = bounded_int(params, "size", param(params, "limit", "50"), minimum=5, maximum=200)
        direction = direction_sql(params)
        sortable = {
            "name": "coalesce(nullif(ticker,''),name) COLLATE NOCASE", "key": "key COLLATE NOCASE",
            "positions": "positions", "managers": "managers", "issuers": "issuers", "value": "value",
        }
        order = sortable.get(param(params, "sort", "value"), "value")
        cte = """WITH grouped AS (SELECT s.issuer name,s.cusip key,s.ticker,0 starred,
          stats.total_value value,stats.position_count positions,stats.manager_count managers,1 issuers
          FROM security_period_stats stats JOIN securities s ON s.id=stats.security_id
          JOIN periods q ON q.id=stats.period_id WHERE q.label=?)"""
        values = [param(params, "period")]
        sql = cte + f""" SELECT *,count(*) OVER() total_count FROM grouped
          ORDER BY {order} {direction},name COLLATE NOCASE ASC,key ASC LIMIT ? OFFSET ?"""
        rows = [dict(row) for row in con.execute(sql, [*values, size, (page - 1) * size])]
        count = rows[0].pop("total_count") if rows else con.execute(
            cte + " SELECT count(*) FROM grouped", values).fetchone()[0]
        for row in rows[1:]:
            row.pop("total_count", None)
        self.json_response({"group": "issuer", "label": "Issuer", "rows": rows, "count": count,
                            "page": page, "size": size})

    def funds(self, con, params):
        if set(params).issubset({"period", "page", "size", "sort", "direction", "fund_q",
                                 "starred", "scope", "manager", "state", "amendments", "form"}):
            self.funds_from_stats(con, params)
            return
        where, values = filters(params)
        query = param(params, "fund_q")
        if query:
            where += (" AND " if where else " WHERE ") + "(m.name LIKE ? ESCAPE '\\' OR m.cik LIKE ? ESCAPE '\\')"
            values.extend([like_value(query), like_value(query)])
        star_filter = param(params, "starred")
        if star_filter in ("0", "1"):
            where += (" AND " if where else " WHERE ") + "m.starred=?"
            values.append(int(star_filter))
        elif star_filter not in ("", "all"):
            raise ValueError("Invalid starred fund filter")
        scope = enum_param(params, "scope", {"", "all", "research"}, "all")
        if scope == "research" and not query:
            where += (" AND " if where else " WHERE ") + \
                "(coalesce(sv.scope_value,stats_all.total_value)>=1000000000 OR m.starred=1)"
        page, size = page_params(params)
        direction = direction_sql(params)
        sortable = {"name":"manager_name COLLATE NOCASE","cik":"cik","state":"state_country",
                    "starred":"starred",
                    "filings":"filings","positions":"positions","securities":"securities",
                    "value":"value","signal_return":"signal_return","signal_pnl":"signal_pnl",
                    "signal_coverage":"signal_coverage",
                    "latest":"latest_filing","coverage":"coverage_status"}
        order = sortable.get(param(params, "sort", "value"), "value")
        base = """ FROM managers m JOIN manager_periods mp ON mp.manager_id=m.id
          JOIN periods q ON q.id=mp.period_id LEFT JOIN positions p ON p.manager_id=m.id AND p.period_id=q.id
          LEFT JOIN securities s ON s.id=p.security_id
          LEFT JOIN effective_filing_parts fp ON fp.accession=mp.accession
          JOIN manager_period_stats stats_all ON stats_all.manager_id=m.id AND stats_all.period_id=q.id
          LEFT JOIN signals.scope_values sv ON sv.cik=m.cik AND sv.period=q.label
          LEFT JOIN signals.fund_scores fs ON fs.cik=m.cik AND fs.period=q.label """
        cte = f"""WITH grouped AS (SELECT m.name manager_name,m.cik,max(m.starred) starred,
          coalesce(nullif(max(m.state_country),''),'—') state_country,
          (SELECT count(*) FROM manager_periods history WHERE history.manager_id=m.id) filings,
          count(p.security_id) positions,count(DISTINCT p.security_id) securities,coalesce(sum(p.value),0) value,
          max(mp.filing_date_iso) latest_filing,
          CASE WHEN min(mp.coverage_status)=max(mp.coverage_status) THEN min(mp.coverage_status) ELSE 'MIXED' END coverage_status,
          max(fs.signal_return) signal_return,max(fs.signal_pnl) signal_pnl,
          max(CASE WHEN fs.eligible_events>0 THEN fs.signal_coverage END) signal_coverage,
          max(fs.priced_events) priced_signals,
          max(fs.eligible_events) eligible_signals,max(fs.rankable) signal_rankable,
          max(fs.reason) signal_reason,max(fs.mark_date) signal_price_date,
          max(coalesce(sv.scope_value,stats_all.total_value)) scope_value,
          max(coalesce(sv.scale_inferred,0)) scope_scale_inferred
          {base}{where} GROUP BY m.id,m.name,m.cik)"""
        sql = cte + f""" SELECT *,count(*) OVER() total_count,
          coalesce(sum(starred) OVER(),0) starred_count FROM grouped
          ORDER BY ({order}) IS NULL ASC,{order} {direction},manager_name COLLATE NOCASE ASC,cik ASC LIMIT ? OFFSET ?"""
        rows = [dict(r) for r in con.execute(sql, [*values, size, (page-1)*size])]
        if rows:
            count, starred_count = rows[0]["total_count"], rows[0]["starred_count"]
        else:
            count, starred_count = con.execute(
                cte + " SELECT count(*),coalesce(sum(starred),0) FROM grouped", values
            ).fetchone()
        for row in rows:
            row.pop("total_count", None)
            row.pop("starred_count", None)
        signal = signal_metadata(con)
        self.json_response({"rows": rows, "count": count, "starred_count": starred_count,
                            "page": page, "size": size, "scope": scope or "all",
                            "signal_price_source": signal.get("price_source", ""),
                            "signal_price_date": signal.get("mark_date", "")})

    def funds_from_stats(self, con, params):
        page, size = page_params(params)
        direction = direction_sql(params)
        clauses, values = ["q.label=?"], [param(params, "period")]
        if manager := param(params, "manager"):
            clauses.append("m.name LIKE ? ESCAPE '\\'")
            values.append(like_value(manager))
        if state := param(params, "state"):
            clauses.append("m.state_country=?")
            values.append(state)
        query = param(params, "fund_q")
        if query:
            clauses.append("(m.name LIKE ? ESCAPE '\\' OR m.cik LIKE ? ESCAPE '\\')")
            values.extend([like_value(query), like_value(query)])
        star_filter = enum_param(params, "starred", {"", "all", "0", "1"}, "")
        if star_filter in {"0", "1"}:
            clauses.append("m.starred=?")
            values.append(int(star_filter))
        if enum_param(params, "amendments", {"", "only"}) == "only":
            clauses.append("fp.part_type!='BASE'")
        if form := param(params, "form"):
            if form != "13F-HR":
                raise ValueError("Invalid form")
        scope = enum_param(params, "scope", {"", "all", "research"}, "all")
        if scope == "research" and not query:
            clauses.append("(coalesce(sv.scope_value,stats.total_value)>=1000000000 OR m.starred=1)")
        sortable = {"name":"manager_name COLLATE NOCASE","cik":"cik","state":"state_country",
                    "starred":"starred", "filings":"filings","positions":"positions",
                    "securities":"securities","value":"value","latest":"latest_filing",
                    "signal_return":"signal_return","signal_pnl":"signal_pnl",
                    "signal_coverage":"signal_coverage","coverage":"coverage_status"}
        order = sortable.get(param(params, "sort", "value"), "value")
        where = " WHERE " + " AND ".join(clauses)
        cte = f"""WITH grouped AS (SELECT m.name manager_name,m.cik,m.starred,
          coalesce(nullif(m.state_country,''),'—') state_country,
          (SELECT count(*) FROM manager_periods history WHERE history.manager_id=m.id) filings,
          stats.position_count positions,stats.security_count securities,stats.total_value value,
          mp.filing_date_iso latest_filing,mp.coverage_status,
          fs.signal_return,fs.signal_pnl,
          CASE WHEN fs.eligible_events>0 THEN fs.signal_coverage END signal_coverage,
          fs.priced_events priced_signals,fs.eligible_events eligible_signals,
          fs.rankable signal_rankable,fs.reason signal_reason,fs.mark_date signal_price_date,
          coalesce(sv.scope_value,stats.total_value) scope_value,
          coalesce(sv.scale_inferred,0) scope_scale_inferred
          FROM managers m JOIN manager_periods mp ON mp.manager_id=m.id
          JOIN periods q ON q.id=mp.period_id
          JOIN manager_period_stats stats ON stats.manager_id=m.id AND stats.period_id=q.id
          LEFT JOIN effective_filing_parts fp ON fp.accession=mp.accession
          LEFT JOIN signals.scope_values sv ON sv.cik=m.cik AND sv.period=q.label
          LEFT JOIN signals.fund_scores fs ON fs.cik=m.cik AND fs.period=q.label{where})"""
        sql = cte + f""" SELECT *,count(*) OVER() total_count,coalesce(sum(starred) OVER(),0) starred_count
          FROM grouped ORDER BY ({order}) IS NULL ASC,{order} {direction},
          manager_name COLLATE NOCASE ASC,cik ASC LIMIT ? OFFSET ?"""
        rows = [dict(row) for row in con.execute(sql, [*values, size, (page - 1) * size])]
        if rows:
            count, starred_count = rows[0]["total_count"], rows[0]["starred_count"]
        else:
            count, starred_count = con.execute(
                cte + " SELECT count(*),coalesce(sum(starred),0) FROM grouped", values).fetchone()
        for row in rows:
            row.pop("total_count", None)
            row.pop("starred_count", None)
        signal = signal_metadata(con)
        self.json_response({"rows": rows, "count": count, "starred_count": starred_count,
                            "page": page, "size": size, "scope": scope or "all",
                            "signal_price_source": signal.get("price_source", ""),
                            "signal_price_date": signal.get("mark_date", "")})

    def suggest(self, con, params):
        kind, query = param(params, "kind"), param(params, "q")
        if len(query) < 2:
            self.json_response([]); return
        if kind == "manager":
            sql = "SELECT name,cik key FROM managers WHERE name LIKE ? ESCAPE '\\' OR cik LIKE ? ESCAPE '\\' ORDER BY name,cik LIMIT 12"
        elif kind == "issuer":
            sql = """SELECT issuer name,CASE WHEN ticker!='' THEN ticker||' · '||cusip ELSE cusip END key
              FROM securities WHERE ticker LIKE ? ESCAPE '\\' OR issuer LIKE ? ESCAPE '\\'
              OR cusip LIKE ? ESCAPE '\\' ORDER BY issuer,cusip LIMIT 12"""
        else:
            raise ValueError("Invalid suggestion type")
        needle = like_value(query)
        values = (needle, needle) if kind == "manager" else (needle, needle, needle)
        self.json_response([dict(r) for r in con.execute(sql, values)])

    def stock_detail(self, con, params):
        cusip = param(params, "cusip").upper()
        if not re.fullmatch(r"[0-9A-Z*@#]{6,12}", cusip):
            raise ValueError("Invalid CUSIP")
        security = con.execute("SELECT * FROM securities WHERE cusip=?", (cusip,)).fetchone()
        if security is None:
            raise ValueError("Unknown CUSIP")
        current, previous = period_pair(con, param(params, "period", latest_period(con)))
        page, size = page_params(params)
        direction = direction_sql(params)
        query = param(params, "fund_q")
        action = param(params, "change")
        if action and action not in {"INCREASED", "REDUCED", "NEW", "EXITED", "UNCHANGED", "NOT_COMPARABLE"}:
            raise ValueError("Invalid change")
        position = param(params, "position")
        if position and position not in {"0", "1", "2"}:
            raise ValueError("Invalid position")
        history = [dict(r) for r in con.execute("""SELECT q.label period,q.period_date,
          coalesce(sum(p.value),0) value,count(DISTINCT p.manager_id) funds
          FROM periods q LEFT JOIN positions p ON p.period_id=q.id AND p.security_id=?
          GROUP BY q.id ORDER BY q.period_date""", (security["id"],))]
        if previous is None:
            self.json_response({"security": dict(security), "current_period": dict(current), "previous_period": None,
                                "history": history, "rows": [], "count": 0, "page": page, "size": size,
                                "summary": {"increased": 0, "reduced": 0, "new": 0, "exited": 0,
                                            "added_or_new": 0, "reduced_or_exited": 0,
                                            "not_comparable": 0}}); return
        sortable = {"manager":"manager_name COLLATE NOCASE","cik":"cik","position":"position_type",
                    "current_shares":"current_shares","previous_shares":"previous_shares","delta":"delta_shares",
                    "percent":"delta_percent","current_value":"current_value","previous_value":"previous_value",
                    "current_weight":"current_weight","previous_weight":"previous_weight",
                    "weight_change":"weight_change","status":"status"}
        order = sortable.get(param(params,"sort","delta"),"abs(delta_shares)")
        extra, values = [], [security["id"], current["id"], security["id"], previous["id"],
                             current["id"], previous["id"], current["id"], previous["id"]]
        if query:
            extra.append("(manager_name LIKE ? ESCAPE '\\' OR cik LIKE ? ESCAPE '\\')"); values.extend([like_value(query)]*2)
        if action:
            extra.append("status=?"); values.append(action)
        if position:
            extra.append("position_type=?"); values.append(position)
        where = " WHERE " + " AND ".join(extra) if extra else ""
        status = comparison_status_sql()
        ctes = f"""WITH c AS (SELECT manager_id,position_type,shares_type,sum(shares) shares,sum(value) value FROM positions
          WHERE security_id=? AND period_id=? GROUP BY manager_id,position_type,shares_type),
        v AS (SELECT manager_id,position_type,shares_type,sum(shares) shares,sum(value) value FROM positions
          WHERE security_id=? AND period_id=? GROUP BY manager_id,position_type,shares_type),
        keys AS (SELECT manager_id,position_type,shares_type FROM c UNION SELECT manager_id,position_type,shares_type FROM v),
        compared AS (SELECT m.id manager_id,m.name manager_name,m.cik,m.starred,k.position_type,k.shares_type,
          coalesce(c.shares,0) current_shares,coalesce(v.shares,0) previous_shares,
          coalesce(c.shares,0)-coalesce(v.shares,0) delta_shares,
          CASE WHEN coalesce(v.shares,0)!=0 THEN (coalesce(c.shares,0)-v.shares)*100.0/v.shares END delta_percent,
          coalesce(c.value,0) current_value,coalesce(v.value,0) previous_value,
          coalesce(c.value,0)-coalesce(v.value,0) delta_value,{status} status,
          CASE WHEN current_stats.total_value>0
            THEN 100.0*coalesce(c.value,0)/current_stats.total_value END current_weight,
          CASE WHEN previous_stats.total_value>0
            THEN 100.0*coalesce(v.value,0)/previous_stats.total_value END previous_weight,
          cm.coverage_status current_coverage,vm.coverage_status previous_coverage
          FROM keys k JOIN managers m ON m.id=k.manager_id
          LEFT JOIN c ON (c.manager_id,c.position_type,c.shares_type)=(k.manager_id,k.position_type,k.shares_type)
          LEFT JOIN v ON (v.manager_id,v.position_type,v.shares_type)=(k.manager_id,k.position_type,k.shares_type)
          LEFT JOIN manager_period_stats current_stats
            ON current_stats.manager_id=k.manager_id AND current_stats.period_id=?
          LEFT JOIN manager_period_stats previous_stats
            ON previous_stats.manager_id=k.manager_id AND previous_stats.period_id=?
          LEFT JOIN manager_periods cm ON cm.manager_id=k.manager_id AND cm.period_id=?
          LEFT JOIN manager_periods vm ON vm.manager_id=k.manager_id AND vm.period_id=?), filtered AS (
          SELECT *,current_weight-previous_weight weight_change FROM compared{where})"""
        summary_row = con.execute(ctes + """ SELECT count(*) total_count,
          count(DISTINCT CASE WHEN status='INCREASED' THEN manager_id END) increased_count,
          count(DISTINCT CASE WHEN status='REDUCED' THEN manager_id END) reduced_count,
          count(DISTINCT CASE WHEN status='NEW' THEN manager_id END) new_count,
          count(DISTINCT CASE WHEN status='EXITED' THEN manager_id END) exited_count,
          count(DISTINCT CASE WHEN status IN ('INCREASED','NEW') THEN manager_id END) added_or_new_count,
          count(DISTINCT CASE WHEN status IN ('REDUCED','EXITED') THEN manager_id END) reduced_or_exited_count,
          count(DISTINCT CASE WHEN status='NOT_COMPARABLE' THEN manager_id END) not_comparable_count
          FROM filtered""", values).fetchone()
        sql = ctes + f""" SELECT * FROM filtered
          ORDER BY {order} {direction},manager_name COLLATE NOCASE,manager_id,
          position_type,shares_type LIMIT ? OFFSET ?"""
        rows = [dict(r) for r in con.execute(sql, [*values,size,(page-1)*size])]
        count = summary_row["total_count"]
        summary = {key: summary_row[f"{key}_count"] for key in ("increased", "reduced", "new", "exited")}
        summary.update({key: summary_row[f"{key}_count"] for key in
                        ("added_or_new", "reduced_or_exited", "not_comparable")})
        self.json_response({"security":dict(security),"current_period":dict(current),"previous_period":dict(previous),
                            "history":history,"rows":rows,"count":count,"page":page,"size":size,"summary":summary})

    def fund_detail(self, con, params):
        raw_cik = param(params,"cik")
        if not raw_cik.isdigit() or len(raw_cik) > 10:
            raise ValueError("Invalid manager CIK")
        cik = raw_cik.zfill(10)
        manager = con.execute("SELECT * FROM managers WHERE cik=?",(cik,)).fetchone()
        if manager is None: raise ValueError("Unknown manager CIK")
        current, previous = period_pair(con,param(params,"period",latest_period(con)))
        page, size = page_params(params)
        direction = direction_sql(params)
        query = param(params, "security_q")
        action = param(params, "change")
        if action and action not in {"INCREASED", "REDUCED", "NEW", "EXITED", "UNCHANGED", "NOT_COMPARABLE"}:
            raise ValueError("Invalid change")
        position = param(params, "position")
        if position and position not in {"0", "1", "2"}:
            raise ValueError("Invalid position")
        history = [dict(r) for r in con.execute("""SELECT q.label period,q.period_date,mp.coverage_status,
          coalesce(sum(p.value),0) value,count(p.security_id) positions,count(DISTINCT p.security_id) securities
          FROM periods q LEFT JOIN manager_periods mp ON mp.period_id=q.id AND mp.manager_id=?
          LEFT JOIN positions p ON p.period_id=q.id AND p.manager_id=? GROUP BY q.id ORDER BY q.period_date""",(manager["id"],manager["id"]))]
        current_coverage_row = con.execute(
            "SELECT coverage_status FROM manager_periods WHERE manager_id=? AND period_id=?",
            (manager["id"], current["id"]),
        ).fetchone()
        previous_coverage_row = (con.execute(
            "SELECT coverage_status FROM manager_periods WHERE manager_id=? AND period_id=?",
            (manager["id"], previous["id"]),
        ).fetchone() if previous is not None else None)
        current_coverage = current_coverage_row[0] if current_coverage_row else "MISSING"
        previous_coverage = previous_coverage_row[0] if previous_coverage_row else "MISSING"
        if previous is None:
            self.json_response({"manager":dict(manager),"current_period":dict(current),"previous_period":None,
                                "current_coverage":current_coverage,"previous_coverage":previous_coverage,
                                "history":history,"rows":[],"count":0,"page":page,"size":size,
                                "summary":{"increased":0,"reduced":0,"new":0,"exited":0}}); return
        sortable={"issuer":"coalesce(nullif(ticker,''),issuer) COLLATE NOCASE","cusip":"cusip","position":"position_type",
                  "current_shares":"current_shares","previous_shares":"previous_shares","delta":"delta_shares",
                  "percent":"delta_percent","current_value":"current_value","previous_value":"previous_value",
                  "current_weight":"current_weight","previous_weight":"previous_weight",
                  "weight_change":"weight_change","status":"status"}
        order=sortable.get(param(params,"sort","current_value"),"current_value")
        extra,values=[],[manager["id"],current["id"],manager["id"],previous["id"],
                         manager["id"],current["id"],manager["id"],previous["id"],
                         manager["id"],current["id"],manager["id"],previous["id"]]
        if query:
            extra.append("(ticker LIKE ? ESCAPE '\\' OR issuer LIKE ? ESCAPE '\\' OR cusip LIKE ? ESCAPE '\\')"); values.extend([like_value(query)]*3)
        if action:
            extra.append("status=?"); values.append(action)
        if position:
            extra.append("position_type=?"); values.append(position)
        where=" WHERE "+" AND ".join(extra) if extra else ""
        status=comparison_status_sql()
        ctes=f"""WITH c AS (SELECT security_id,position_type,shares_type,shares,value,manager_id FROM positions WHERE manager_id=? AND period_id=?),
        v AS (SELECT security_id,position_type,shares_type,shares,value,manager_id FROM positions WHERE manager_id=? AND period_id=?),
        totals AS (SELECT
          (SELECT sum(value) FROM positions WHERE manager_id=? AND period_id=?) current_total,
          (SELECT sum(value) FROM positions WHERE manager_id=? AND period_id=?) previous_total),
        keys AS (SELECT security_id,position_type,shares_type FROM c UNION SELECT security_id,position_type,shares_type FROM v),
        compared AS (SELECT s.ticker,s.issuer,s.class,s.cusip,k.position_type,k.shares_type,
          coalesce(c.shares,0) current_shares,coalesce(v.shares,0) previous_shares,
          coalesce(c.shares,0)-coalesce(v.shares,0) delta_shares,
          CASE WHEN coalesce(v.shares,0)!=0 THEN (coalesce(c.shares,0)-v.shares)*100.0/v.shares END delta_percent,
          coalesce(c.value,0) current_value,coalesce(v.value,0) previous_value,
          coalesce(c.value,0)-coalesce(v.value,0) delta_value,{status} status,
          CASE WHEN totals.current_total>0 THEN 100.0*coalesce(c.value,0)/totals.current_total END current_weight,
          CASE WHEN totals.previous_total>0 THEN 100.0*coalesce(v.value,0)/totals.previous_total END previous_weight,
          cm.coverage_status current_coverage,vm.coverage_status previous_coverage
          FROM keys k CROSS JOIN totals JOIN securities s ON s.id=k.security_id
          LEFT JOIN c ON (c.security_id,c.position_type,c.shares_type)=(k.security_id,k.position_type,k.shares_type)
          LEFT JOIN v ON (v.security_id,v.position_type,v.shares_type)=(k.security_id,k.position_type,k.shares_type)
          LEFT JOIN manager_periods cm ON cm.manager_id=? AND cm.period_id=?
          LEFT JOIN manager_periods vm ON vm.manager_id=? AND vm.period_id=?), filtered AS (
          SELECT *,current_weight-previous_weight weight_change FROM compared{where})"""
        sql=ctes+f""" SELECT *,count(*) OVER() total_count,sum(status='INCREASED') OVER() increased_count,
          sum(status='REDUCED') OVER() reduced_count,sum(status='NEW') OVER() new_count,sum(status='EXITED') OVER() exited_count
          FROM filtered ORDER BY {order} {direction},issuer COLLATE NOCASE,cusip,
          position_type,shares_type LIMIT ? OFFSET ?"""
        rows=[dict(r) for r in con.execute(sql,[*values,size,(page-1)*size])]
        summary={"increased":0,"reduced":0,"new":0,"exited":0}; count=0
        if rows:
            count=rows[0]["total_count"]; summary={k:rows[0][k+"_count"] for k in summary}
            for row in rows:
                for key in ("total_count","increased_count","reduced_count","new_count","exited_count"): row.pop(key,None)
        elif page > 1:
            summary_row=con.execute(ctes+""" SELECT count(*) total_count,
              coalesce(sum(status='INCREASED'),0) increased_count,
              coalesce(sum(status='REDUCED'),0) reduced_count,
              coalesce(sum(status='NEW'),0) new_count,
              coalesce(sum(status='EXITED'),0) exited_count FROM filtered""",values).fetchone()
            count=summary_row["total_count"]
            summary={key:summary_row[key+"_count"] for key in summary}
        self.json_response({"manager":dict(manager),"current_period":dict(current),"previous_period":dict(previous),
          "current_coverage":current_coverage,"previous_coverage":previous_coverage,
          "history":history,"rows":rows,"count":count,"page":page,"size":size,"summary":summary})

    def net_adds(self, con, params):
        page, size = page_params(params)
        direction = direction_sql(params)
        position = param(params, "position", "SHARES").upper()
        positions = {"SHARES": 0, "PUT": 1, "CALL": 2, "0": 0, "1": 1, "2": 2}
        if position not in positions:
            raise ValueError("Invalid position")
        position_type = positions[position]
        metric = param(params, "metric", "value")
        if metric not in ("value", "portfolio", "position"):
            raise ValueError("Invalid quarterly-change metric")
        minimum_activity = nonnegative_bound(params, "min_activity", integer=True)
        minimum_adding = nonnegative_bound(params, "min_adding_funds", integer=True)
        maximum_adding = nonnegative_bound(params, "max_adding_funds", integer=True)
        minimum_cutting = nonnegative_bound(params, "min_cutting_funds", integer=True)
        maximum_cutting = nonnegative_bound(params, "max_cutting_funds", integer=True)
        minimum_market_cap = nonnegative_bound(params, "min_market_cap")
        maximum_market_cap = nonnegative_bound(params, "max_market_cap")
        validate_range(minimum_adding, maximum_adding, "adding funds")
        validate_range(minimum_cutting, maximum_cutting, "cutting funds")
        validate_range(minimum_market_cap, maximum_market_cap, "market cap")

        periods = [dict(row) for row in con.execute("""SELECT q.id,q.label,q.period_date,
          previous.id previous_id,previous.label previous_label,
          (SELECT count(*) FROM manager_periods current_managers
            JOIN manager_periods previous_managers ON previous_managers.manager_id=current_managers.manager_id
            WHERE current_managers.period_id=q.id AND previous_managers.period_id=previous.id
              AND current_managers.coverage_status='COMPLETE'
              AND previous_managers.coverage_status='COMPLETE') comparable_managers
          FROM periods q
          JOIN (SELECT DISTINCT current_period_id FROM stock_changes) available ON available.current_period_id=q.id
          JOIN periods previous ON previous.id=(SELECT previous_period_id FROM stock_changes
            WHERE current_period_id=q.id LIMIT 1) ORDER BY q.period_date""")]
        if not periods:
            self.json_response({"periods": [], "rows": [], "count": 0, "page": page, "size": size}); return
        latest_id = periods[-1]["id"]
        if metric == "value":
            metric_expression = "sc.value_change"
        elif metric == "portfolio":
            metric_expression = ("CASE WHEN totals.current_value!=0 AND totals.previous_value!=0 THEN "
                "100.0*sc.current_value/totals.current_value-"
                "100.0*(sc.current_value-sc.value_change)/totals.previous_value ELSE 0 END")
        else:
            metric_expression = ("CASE WHEN sc.current_value-sc.value_change>0 THEN "
                "100.0*sc.value_change/(sc.current_value-sc.value_change) END")
        release_columns = ",".join(
            (f"max(CASE WHEN current_period_id={period['id']} THEN metric_value END) release_{period['id']}"
             if metric == "position" else
             f"coalesce(max(CASE WHEN current_period_id={period['id']} THEN metric_value END),0) release_{period['id']}")
            for period in periods)
        status_columns = ",".join(
            f"coalesce(max(CASE WHEN current_period_id={period['id']} THEN metric_status END),'NOT_HELD') status_{period['id']}"
            for period in periods)
        if metric == "position":
            valid_previous = "sum(CASE WHEN previous_value>0 THEN previous_value ELSE 0 END)"
            valid_change = "sum(CASE WHEN previous_value>0 THEN value_change ELSE 0 END)"
            overall_expression = (f"CASE WHEN {valid_previous}>0 THEN "
                f"100.0*{valid_change}/{valid_previous} END")
        else:
            overall_expression = "sum(metric_value)"
        latest_columns = f"""coalesce(max(CASE WHEN current_period_id={latest_id} THEN adding_funds END),0) adding_funds,
          coalesce(max(CASE WHEN current_period_id={latest_id} THEN cutting_funds END),0) cutting_funds,
          coalesce(max(CASE WHEN current_period_id={latest_id} THEN current_funds END),0) current_funds,
          coalesce(max(CASE WHEN current_period_id={latest_id} THEN current_value END),0) current_value"""
        if metric == "position":
            release_names = [f"release_{period['id']}" for period in periods]
            trend_expression = (f"CASE WHEN defined_releases>=2 THEN "
                f"coalesce({','.join(reversed(release_names))})-"
                f"coalesce({','.join(release_names)}) END" if len(release_names) > 1 else "NULL")
        else:
            trend_expression = f"release_{periods[-1]['id']}-release_{periods[0]['id']}"
        sortable = {
            "rank": "net_rank", "issuer": "coalesce(nullif(ticker,''),issuer) COLLATE NOCASE",
            "cusip": "cusip", "overall": "overall", "trend": "trend", "adding": "adding_funds",
            "cutting": "cutting_funds", "current_funds": "current_funds", "current_value": "current_value",
            "market_cap": "market_cap", "latest": f"release_{latest_id}",
        }
        for period in periods:
            sortable[f"release_{period['id']}"] = f"release_{period['id']}"
        order = sortable.get(param(params, "sort", "overall"), "overall")
        clauses, values = [], []
        if query := param(params, "stock_q"):
            needle = like_value(query)
            clauses.append("(ticker LIKE ? ESCAPE '\\' OR issuer LIKE ? ESCAPE '\\' OR cusip LIKE ? ESCAPE '\\')")
            values.extend([needle, needle, like_value(query.upper())])
        if minimum_activity is not None:
            clauses.append("total_activity>=?")
            values.append(minimum_activity)
        for value, clause in (
            (minimum_adding, "adding_funds>=?"),
            (maximum_adding, "adding_funds<=?"),
            (minimum_cutting, "cutting_funds>=?"),
            (maximum_cutting, "cutting_funds<=?"),
            (minimum_market_cap, "market_cap>=?"),
            (maximum_market_cap, "market_cap<=?"),
        ):
            if value is not None:
                clauses.append(clause)
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        ctes = f"""WITH metrics AS (SELECT sc.security_id,sc.current_period_id,
          sc.adding_funds,sc.cutting_funds,sc.current_funds,sc.current_value,
          sc.value_change,sc.current_value-sc.value_change previous_value,
          CASE WHEN sc.current_value-sc.value_change>0 AND sc.current_value=0 THEN 'EXITED'
            WHEN sc.current_value-sc.value_change>0 THEN 'DEFINED'
            WHEN sc.current_value>0 THEN 'NEW_OR_ZERO_BASE'
            ELSE 'NO_REPORTED_VALUE' END metric_status,
          {metric_expression} metric_value
          FROM stock_changes sc JOIN period_change_totals totals
            ON totals.current_period_id=sc.current_period_id
          WHERE sc.position_type=?),
        pivoted AS (SELECT s.ticker,s.issuer,s.cusip,max(mc.market_cap) market_cap,{overall_expression} overall,
          sum(metric_value IS NOT NULL) defined_releases,
          sum(adding_funds+cutting_funds) total_activity,{release_columns},{status_columns},{latest_columns}
          FROM metrics JOIN securities s ON s.id=metrics.security_id
          LEFT JOIN market_caps mc ON mc.ticker=s.ticker GROUP BY s.id),
        ranked AS (SELECT *,{trend_expression} trend,
          CASE WHEN overall IS NOT NULL THEN row_number() OVER (ORDER BY overall IS NULL ASC,overall DESC,adding_funds DESC,
            current_value DESC,cusip ASC) END net_rank FROM pivoted)"""
        sql = ctes + f""", filtered AS (SELECT *,count(*) OVER() total_count FROM ranked{where})
        SELECT * FROM filtered ORDER BY ({order}) IS NULL ASC,{order} {direction},adding_funds DESC,
          current_value DESC,cusip ASC LIMIT ? OFFSET ?"""
        rows = [dict(row) for row in con.execute(sql, [position_type, *values, size, (page-1)*size])]
        count = rows[0].pop("total_count") if rows else con.execute(
            ctes + f" SELECT count(*) FROM ranked{where}", [position_type, *values]).fetchone()[0]
        for row in rows:
            row.pop("total_count", None)
            row.pop("total_activity", None)
            row["history"] = [row.pop(f"release_{period['id']}") for period in periods]
            statuses = [row.pop(f"status_{period['id']}") for period in periods]
            if metric == "position":
                row["history_status"] = statuses
        self.json_response({"periods": periods, "metric": metric, "position_type": position_type,
                            "rows": rows, "count": count, "page": page, "size": size})


def database_is_current() -> bool:
    if not DB.exists():
        return False
    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as con:
            metadata = dict(con.execute("SELECT key,value FROM metadata"))
        archive_paths = sorted((Path(path) for path in glob.glob(str(ROOT / "*_form13f.zip"))),
                               key=lambda path: path.name)
        archive_names = [path.name for path in archive_paths]
        archive_hashes = [{"name": path.name, "sha256": sha256_file(path)} for path in archive_paths]
        market_cap_hash = sha256_file(MARKET_CAPS) if MARKET_CAPS.exists() else ""
        watchlist_hash = sha256_file(STARRED_FUNDS) if STARRED_FUNDS.exists() else ""
        ticker_map_hash = sha256_file(TICKER_MAP) if TICKER_MAP.exists() else ""
        return (metadata.get("schema_version") == SCHEMA_VERSION
                and json.loads(metadata.get("source_archives", "[]")) == archive_names
                and json.loads(metadata.get("source_archive_hashes", "[]")) == archive_hashes
                and metadata.get("ticker_map_sha256", "") == ticker_map_hash
                and metadata.get("market_cap_sha256", "") == market_cap_hash
                and metadata.get("fund_watchlist_sha256", "") == watchlist_hash)
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
        return False


class ExplorerHTTPServer(ThreadingHTTPServer):
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
    parser=argparse.ArgumentParser(description="Serve the local 13F Explorer")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8013)
    parser.add_argument("--no-build",action="store_true")
    args=parser.parse_args()
    if not database_is_current() and not args.no_build:
        subprocess.run([sys.executable,str(ROOT/"build_database.py"),"--force"],check=True)
    if not database_is_current():
        raise SystemExit("Database is missing or stale. Run: python3 build_database.py --force")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    mimetypes.add_type("text/javascript",".js")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: the explorer is being exposed beyond this computer. Use only on a trusted network.",
              file=sys.stderr)
    server_class = IPv6ExplorerHTTPServer if ":" in args.host else ExplorerHTTPServer
    server=server_class((args.host,args.port),Handler)
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"13F Explorer is running at http://{display_host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
