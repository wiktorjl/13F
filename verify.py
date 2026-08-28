#!/usr/bin/env python3
"""Deterministic verification workflow for the local 13F dashboard."""

from __future__ import annotations

import argparse
import contextlib
import json
import py_compile
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import build_database
import server
from tests.chromium_walkthrough import run_walkthrough
from tests.support import create_fixture_database, http_request, running_server

ROOT = Path(__file__).resolve().parent
JAVASCRIPT_FILES = ("dashboard.js",)
# The dashboard DOM contract: tests/chromium_walkthrough.py and dashboard.js both key on these.
DASHBOARD_IDS = frozenset({
    "dashLogo", "dashNav", "dashMain", "dashControls", "dashSide", "dashHorizon",
    "dashTable", "dashHead", "dashRows", "dashStatus", "dashPager", "dashPrev", "dashNext",
    "dashAbout", "aboutQuarters", "aboutSpan", "aboutManagers",
})


class VerificationFailure(RuntimeError):
    pass


class DocumentAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.preloads: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "script":
            if values.get("src"):
                self.scripts.append(str(values["src"]))
            else:
                self.inline_scripts += 1
        # <style> blocks, style="..." and on*="..." attributes all need
        # 'unsafe-inline', which the CSP deliberately does not grant.
        if tag == "style" or any(name == "style" or name.startswith("on") for name, _ in attrs):
            self.inline_styles += 1
        rel = set(str(values.get("rel", "")).split())
        if tag == "link" and "stylesheet" in rel:
            if values.get("href"):
                self.stylesheets.append(str(values["href"]))
        if tag == "link" and rel & {"preload", "modulepreload"}:
            self.preloads.append(str(values.get("href") or ""))


def audit_document(name: str, *, scripts: list[str], stylesheets: list[str],
                   required_ids: frozenset[str] = frozenset()) -> DocumentAudit:
    """Reject inline scripts/styles/handlers, duplicate IDs, and any asset outside the static allowlist."""
    audit = DocumentAudit()
    audit.feed((ROOT / name).read_text(encoding="utf-8"))
    duplicates = sorted(identifier for identifier in set(audit.ids) if audit.ids.count(identifier) > 1)
    if duplicates:
        raise VerificationFailure(f"{name} contains duplicate IDs: {duplicates}")
    if audit.inline_scripts:
        raise VerificationFailure(f"{name} contains inline script that violates the static CSP")
    if audit.inline_styles:
        raise VerificationFailure(f"{name} contains inline style or event-handler attributes that violate the static CSP")
    if audit.preloads:
        raise VerificationFailure(f"{name} contains preload links outside the static allowlist: {audit.preloads}")
    # Root-absolute references ("/dashboard.js") are how the routed document
    # (/, /initiations, /movers, /about) reaches the same allowlisted assets. Tolerate exactly
    # one leading slash: "//dashboard.js" is a protocol-relative external host.
    if ([src.removeprefix("/") for src in audit.scripts] != scripts
            or [href.removeprefix("/") for href in audit.stylesheets] != stylesheets):
        raise VerificationFailure(
            f"Unexpected local assets in {name}: scripts={audit.scripts}, styles={audit.stylesheets}"
        )
    missing = sorted(required_ids.difference(audit.ids))
    if missing:
        raise VerificationFailure(f"{name} is missing required IDs: {missing}")
    return audit


def syntax_checks() -> dict[str, Any]:
    python_files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
    for path in python_files:
        py_compile.compile(str(path), doraise=True)

    node = shutil.which("node")
    if not node:
        raise VerificationFailure("Node.js is required for the deterministic JavaScript syntax check")
    for name in JAVASCRIPT_FILES:
        result = subprocess.run(
            [node, "--check", str(ROOT / name)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode:
            raise VerificationFailure(f"{name} syntax check failed:\n{result.stdout}")

    dashboard = audit_document("dashboard.html", scripts=["dashboard.js"], stylesheets=["dashboard.css"],
                               required_ids=DASHBOARD_IDS)
    return {"python_files": len(python_files), "javascript": "node --check " + " ".join(JAVASCRIPT_FILES),
            "dashboard_ids": len(dashboard.ids)}


def run_unittests() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v", "-s", "tests"], cwd=ROOT
    )
    if result.returncode:
        raise VerificationFailure("The unittest suite failed")


def _scalar(con: sqlite3.Connection, sql: str, values: tuple[Any, ...] = ()) -> Any:
    row = con.execute(sql, values).fetchone()
    return row[0] if row is not None else None


def check_database(path: Path, *, require_fresh: bool) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise VerificationFailure(f"Database is missing: {path}")
    if require_fresh:
        if path != server.DB.resolve():
            raise VerificationFailure("Freshness hashing is only defined for the configured production database")
        if not server.database_is_current():
            raise VerificationFailure(
                "Production database is stale or incomplete; run `python3 build_database.py --force`"
            )
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=30)) as con:
        quick_check = [row[0] for row in con.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise VerificationFailure("PRAGMA quick_check failed: " + "; ".join(quick_check))
        con.execute("PRAGMA query_only=ON")
        period_count = int(_scalar(con, "SELECT count(*) FROM periods"))
        build_database.validate_database(con, expected_period_count=period_count)
        zero_checks = {
            "invalid manager flags": "SELECT count(*) FROM managers WHERE starred NOT IN (0,1)",
            "invalid positions": """SELECT count(*) FROM positions
              WHERE value<0 OR shares<0 OR position_type NOT IN (0,1,2) OR shares_type NOT IN (0,1,2)""",
            "orphan changes": """SELECT count(*) FROM stock_changes c
              LEFT JOIN periods current ON current.id=c.current_period_id
              LEFT JOIN periods previous ON previous.id=c.previous_period_id
              LEFT JOIN securities s ON s.id=c.security_id
              WHERE current.id IS NULL OR previous.id IS NULL OR s.id IS NULL
                OR current.period_date<=previous.period_date""",
            "orphan change totals": """SELECT count(*) FROM period_change_totals t
              LEFT JOIN periods current ON current.id=t.current_period_id
              LEFT JOIN periods previous ON previous.id=t.previous_period_id
              WHERE current.id IS NULL OR previous.id IS NULL OR current.period_date<=previous.period_date""",
            "invalid market caps": "SELECT count(*) FROM market_caps WHERE market_cap<=0",
        }
        failures = []
        for label, sql in zero_checks.items():
            count = int(_scalar(con, sql))
            if count:
                failures.append(f"{label}: {count}")

        metadata = dict(con.execute("SELECT key,value FROM metadata"))
        count_tables = {
            "period_count": "periods",
            "manager_count": "managers",
            "security_count": "securities",
            "position_count": "positions",
        }
        for key, table in count_tables.items():
            actual = int(_scalar(con, f"SELECT count(*) FROM {table}"))
            if metadata.get(key) != str(actual):
                failures.append(f"metadata {key}: expected {actual}, found {metadata.get(key)!r}")
        latest = _scalar(con, "SELECT label FROM periods ORDER BY period_date DESC LIMIT 1")
        if metadata.get("latest_period") != latest:
            failures.append(
                f"metadata latest_period: expected {latest!r}, found {metadata.get('latest_period')!r}"
            )
        if metadata.get("schema_version") != server.SCHEMA_VERSION:
            failures.append(
                f"schema version: expected {server.SCHEMA_VERSION}, found {metadata.get('schema_version')!r}"
            )
        if failures:
            raise VerificationFailure("Database invariant failures: " + " | ".join(failures))
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "schema_version": metadata["schema_version"],
            "periods": period_count,
            "managers": int(metadata["manager_count"]),
            "securities": int(metadata["security_count"]),
            "positions": int(metadata["position_count"]),
            "quick_check": "ok",
            "fresh": require_fresh,
        }


def api_smoke_and_performance(
    database: Path, *, repetitions: int = 2, budget_seconds: float = 5.0
) -> dict[str, dict[str, float]]:
    endpoints: dict[str, tuple[str, set[str]]] = {
        "meta": ("/api/meta", {"periods", "latest_period", "period_count", "holding_count", "distinct_managers"}),
        "dashboard": ("/api/dashboard?" + urlencode({"view": "movers", "horizon": 1, "side": "gainers", "page": 1, "size": 10}), {"rows", "count", "view"}),
        "dashboard-holdings": ("/api/dashboard?" + urlencode({"view": "holdings", "page": 1, "size": 10}), {"rows", "count", "view"}),
        "dashboard-initiations": ("/api/dashboard?" + urlencode({"view": "initiations", "page": 1, "size": 10}), {"rows", "count", "view"}),
        # A price sort joins prices.bars for the whole holdings universe; keep it inside the budget.
        "dashboard-price-sort": ("/api/dashboard?" + urlencode({"view": "holdings", "sort": "price", "direction": "asc", "page": 1, "size": 10}), {"rows", "count", "sort", "direction"}),
    }
    measurements: dict[str, dict[str, float]] = {}
    with running_server(database) as address:
        for name, (path, required_keys) in endpoints.items():
            durations = []
            payload: Any = None
            for index in range(repetitions + 1):
                started = time.perf_counter()
                status, headers, body = http_request(address, path)
                elapsed = time.perf_counter() - started
                if status != 200:
                    raise VerificationFailure(
                        f"API smoke {name} returned HTTP {status}: {body.decode(errors='replace')}"
                    )
                if headers.get("content-type") != "application/json; charset=utf-8":
                    raise VerificationFailure(f"API smoke {name} returned the wrong content type")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise VerificationFailure(f"API smoke {name} returned invalid JSON") from exc
                if index:
                    durations.append(elapsed)
            if not isinstance(payload, dict) or not required_keys.issubset(payload):
                raise VerificationFailure(f"API smoke {name} response is missing {required_keys}")
            maximum = max(durations)
            if maximum > budget_seconds:
                raise VerificationFailure(
                    f"API performance budget exceeded for {name}: {maximum:.3f}s > {budget_seconds:.3f}s"
                )
            measurements[name] = {
                "median_seconds": round(statistics.median(durations), 6),
                "max_seconds": round(maximum, 6),
            }
    return measurements


def run_step(name: str, function: Callable[[], Any]) -> Any:
    print(f"\n==> {name}", flush=True)
    started = time.monotonic()
    result = function()
    print(f"PASS {name} ({time.monotonic() - started:.2f}s)", flush=True)
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fast", action="store_true", help="Use only the tiny fixture; skip production DB and Chromium")
    mode.add_argument("--ci", action="store_true", help="CI alias for --fast; never reads or rebuilds production data")
    parser.add_argument("--database", type=Path, default=server.DB, help="Production database for full verification")
    parser.add_argument("--skip-browser", action="store_true", help="Skip Chromium in full mode")
    parser.add_argument("--chromium", help="Chromium executable (or set CHROMIUM)")
    parser.add_argument("--api-budget", type=float, default=5.0, help="Maximum warmed API response time")
    parser.add_argument(
        "--browser-report",
        type=Path,
        default=ROOT / "artifacts" / "chromium-report.json",
        help="JSON report; desktop/mobile PNGs are written beside it",
    )
    args = parser.parse_args()
    if args.api_budget <= 0:
        parser.error("--api-budget must be positive")
    return args


def main() -> int:
    args = parse_args()
    fast = args.fast or args.ci
    try:
        run_step("Python, JavaScript, and HTML syntax", syntax_checks)
        run_step("stdlib unittest suite", run_unittests)
        if fast:
            with tempfile.TemporaryDirectory(prefix="13f-verify-") as directory:
                fixture = create_fixture_database(Path(directory) / "fixture.sqlite")
                run_step("fixture quick_check and invariants", lambda: check_database(fixture, require_fresh=False))
                # The fast path never reads production caches: point the price
                # cache at an absent file so the dashboard degrades to "unpriced".
                original_price_cache = server.PRICE_CACHE
                server.PRICE_CACHE = Path(directory) / "prices.sqlite"
                try:
                    run_step(
                        "fixture API smoke and performance",
                        lambda: api_smoke_and_performance(
                            fixture, repetitions=2, budget_seconds=min(args.api_budget, 5.0)
                        ),
                    )
                finally:
                    server.PRICE_CACHE = original_price_cache
            print("\nFAST VERIFICATION PASSED (production database and Chromium intentionally not used)")
            return 0

        database = args.database.resolve()
        if database != server.DB.resolve():
            server.DB = database
        run_step("production freshness, quick_check, and invariants", lambda: check_database(database, require_fresh=True))
        run_step(
            "production API smoke and performance",
            lambda: api_smoke_and_performance(
                database, repetitions=2, budget_seconds=args.api_budget
            ),
        )
        if not args.skip_browser:
            report = run_step(
                "full-data Chromium walkthrough",
                lambda: run_walkthrough(
                    database, chromium=args.chromium, report_path=args.browser_report
                ),
            )
            print("Screenshots:")
            for name, path in report.get("screenshots", {}).items():
                print(f"  {name}: {path}")
        print("\nFULL VERIFICATION PASSED")
        return 0
    except (VerificationFailure, OSError, ValueError, sqlite3.Error) as exc:
        print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
