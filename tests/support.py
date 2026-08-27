"""Small, deterministic database and HTTP helpers used by verification tests."""

from __future__ import annotations

import contextlib
import http.client
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import build_database
import server


PERIODS = (
    (1, "30-JUN-2025", "2025-06-30", "fixture-q1.zip"),
    (2, "30-SEP-2025", "2025-09-30", "fixture-q2.zip"),
    (3, "31-DEC-2025", "2025-12-31", "fixture-q3.zip"),
)

MANAGERS = (
    (1, "0000000001", "Alpha Capital", "NY", 1),
    (2, "0000000002", "Beta Partners", "CA", 0),
    (3, "0000000003", "Gamma Advisors", "GB", 0),
)

SECURITIES = (
    (1, "037833100", "APPLE INC", "COM", "", "AAPL"),
    (2, "594918104", "MICROSOFT CORP", "COM", "", "MSFT"),
    (3, "67066G104", "NVIDIA CORP", "COM", "", "NVDA"),
    (4, "88160R101", "TESLA INC", "COM", "", "TSLA"),
)

SECTORS = (
    ("AAPL", "Technology", "Apple Inc."),
    ("MSFT", "Technology", "Microsoft Corporation"),
    ("NVDA", "Technology", "NVIDIA Corporation"),
    ("TSLA", "Consumer Discretionary", "Tesla, Inc."),
)

# manager, period, security, position type, unit type, reported value, units
POSITIONS = (
    (1, 1, 1, 0, 0, 1_000, 100.0),
    (1, 1, 2, 0, 0, 500, 50.0),
    (2, 1, 1, 0, 0, 250, 25.0),
    (2, 1, 4, 0, 0, 300, 30.0),
    (3, 1, 3, 0, 0, 150, 15.0),
    (1, 2, 1, 0, 0, 1_500, 120.0),
    (1, 2, 1, 1, 0, 100, 2.0),
    (1, 2, 3, 0, 0, 400, 20.0),
    (2, 2, 1, 0, 0, 120, 10.0),
    (2, 2, 4, 0, 0, 330, 30.0),
    (3, 2, 3, 0, 0, 260, 20.0),
    (1, 3, 1, 0, 0, 1_600, 80.0),
    (1, 3, 1, 1, 0, 150, 3.0),
    (1, 3, 3, 0, 0, 900, 30.0),
    (2, 3, 2, 0, 0, 300, 10.0),
    (2, 3, 4, 0, 0, 1_000, 60.0),
    (3, 3, 3, 0, 0, 800, 25.0),
)


def _insert_manager_periods(con: sqlite3.Connection) -> None:
    rows = []
    for manager_id, *_ in MANAGERS:
        for period_id, _, _, source in PERIODS:
            accession = f"{manager_id:010d}-25-{period_id:06d}"
            coverage = "PARTIAL" if (manager_id, period_id) == (3, 2) else "COMPLETE"
            rows.append(
                (
                    manager_id,
                    period_id,
                    accession,
                    f"14-{('AUG', 'NOV', 'FEB')[period_id - 1]}-2025",
                    f"2025-{(8, 11, 2)[period_id - 1]:02d}-14",
                    source,
                    "13F HOLDINGS REPORT",
                    coverage,
                    0,
                    1,
                )
            )
    con.executemany(
        """INSERT INTO manager_periods
        (manager_id,period_id,accession,filing_date,filing_date_iso,source_archive,
         report_type,coverage_status,confidential_omitted,part_count)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    con.execute(
        """INSERT INTO effective_filing_parts(manager_id,period_id,accession,part_type)
        SELECT manager_id,period_id,accession,'BASE' FROM manager_periods"""
    )
    # Exercise the single-part restatement case: it is an amendment even though
    # the canonical effective chain contains only one (resetting) filing part.
    con.execute(
        """UPDATE effective_filing_parts SET part_type='RESTATEMENT'
        WHERE manager_id=1 AND period_id=3"""
    )


def _insert_statistics(con: sqlite3.Connection) -> None:
    con.execute(
        """INSERT INTO filing_part_stats
        SELECT mp.accession,mp.manager_id,mp.period_id,count(p.security_id),coalesce(sum(p.value),0),
          count(p.security_id),coalesce(sum(p.value),0),1,1,0
        FROM manager_periods mp LEFT JOIN positions p
          ON p.manager_id=mp.manager_id AND p.period_id=mp.period_id
        GROUP BY mp.manager_id,mp.period_id"""
    )
    con.execute(
        """INSERT INTO manager_period_stats
        SELECT mp.manager_id,mp.period_id,count(p.security_id),count(DISTINCT p.security_id),
          coalesce(sum(p.value),0)
        FROM manager_periods mp LEFT JOIN positions p
          ON p.manager_id=mp.manager_id AND p.period_id=mp.period_id
        GROUP BY mp.manager_id,mp.period_id"""
    )
    con.execute(
        """INSERT INTO security_period_stats
        SELECT period_id,security_id,count(*),count(DISTINCT manager_id),sum(value)
        FROM positions GROUP BY period_id,security_id"""
    )
    con.execute(
        """INSERT INTO period_stats
        SELECT q.id,count(p.security_id),coalesce(sum(p.value),0),count(DISTINCT p.manager_id),
          count(DISTINCT p.security_id),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id AND mp.coverage_status='COMPLETE'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id AND mp.coverage_status='PARTIAL'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id AND mp.coverage_status='INFERRED'),
          (SELECT count(*) FROM manager_periods mp WHERE mp.period_id=q.id AND mp.coverage_status='NOTICE')
        FROM periods q LEFT JOIN positions p ON p.period_id=q.id GROUP BY q.id"""
    )


def _insert_changes(con: sqlite3.Connection) -> None:
    # Values describe the comparable Alpha/Beta cohort. Gamma's partial middle
    # quarter is deliberately excluded from comparison aggregates.
    rows = (
        (2, 1, 1, 0, 2, 1, 1, 5, 2, 2, 5.0, 1_620, 370),
        (2, 1, 2, 0, 2, 0, 1, -50, 0, 1, -50.0, 0, -500),
        (2, 1, 3, 0, 2, 1, 0, 20, 1, 0, 20.0, 400, 400),
        (2, 1, 4, 0, 2, 0, 0, 0, 1, 1, 0.0, 330, 30),
        (2, 1, 1, 1, 2, 1, 0, 2, 1, 0, 2.0, 100, 100),
        (3, 2, 1, 0, 2, 0, 2, -50, 1, 2, -50.0, 1_600, -20),
        (3, 2, 2, 0, 2, 1, 0, 10, 1, 0, 10.0, 300, 300),
        (3, 2, 3, 0, 2, 1, 0, 10, 1, 1, 10.0, 900, 500),
        (3, 2, 4, 0, 2, 1, 0, 30, 1, 1, 30.0, 1_000, 670),
        (3, 2, 1, 1, 2, 1, 0, 1, 1, 1, 1.0, 150, 50),
    )
    con.executemany(
        """INSERT INTO stock_changes
        (current_period_id,previous_period_id,security_id,position_type,comparable_funds,
         adding_funds,cutting_funds,net_add,current_funds,previous_funds,net_units,
         current_value,value_change) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    con.executemany(
        """INSERT INTO period_change_totals
        (current_period_id,previous_period_id,current_value,previous_value) VALUES (?,?,?,?)""",
        ((2, 1, 2_450, 2_050), (3, 2, 3_950, 2_450)),
    )


def create_fixture_database(path: Path) -> Path:
    """Create a tiny schema-current database with meaningful adjacent quarters."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.executescript(build_database.SCHEMA)
        con.executemany("INSERT INTO periods VALUES (?,?,?,?)", PERIODS)
        con.executemany("INSERT INTO managers VALUES (?,?,?,?,?)", MANAGERS)
        con.executemany("INSERT INTO securities VALUES (?,?,?,?,?,?)", SECURITIES)
        con.executemany(
            "INSERT INTO market_caps VALUES (?,?)",
            (("AAPL", 3_100_000_000_000), ("MSFT", 2_900_000_000_000),
             ("NVDA", 3_000_000_000_000), ("TSLA", 900_000_000_000)),
        )
        con.executemany("INSERT INTO sectors VALUES (?,?,?)", SECTORS)
        _insert_manager_periods(con)
        con.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?)", POSITIONS)
        _insert_statistics(con)
        # Dashboard rollups come from the builder's shared materializer, never by hand.
        build_database.materialize_dashboard_stats(con)
        _insert_changes(con)
        metadata = {
            "schema_version": build_database.SCHEMA_VERSION,
            "snapshot_policy": "Deterministic verification fixture",
            "period_count": str(len(PERIODS)),
            "manager_count": str(len(MANAGERS)),
            "security_count": str(len(SECURITIES)),
            "position_count": str(len(POSITIONS)),
            "latest_period": PERIODS[-1][1],
            "built_at": "2026-01-15T12:00:00+00:00",
            "source_archives": "[]",
            "source_archive_hashes": "[]",
            "ticker_map_sha256": "",
            "market_cap_sha256": "",
            "fund_watchlist_sha256": "",
            "market_cap_source": "Fixture market caps",
            "market_cap_retrieved_at": "2026-01-15T00:00:00+00:00",
            "sector_sha256": "",
            "sector_source": "Fixture sectors",
            "sector_retrieved_at": "2026-01-15T00:00:00+00:00",
            "sector_count": str(len(SECTORS)),
        }
        con.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", metadata.items())
        con.commit()
    return path


class QuietHandler(server.Handler):
    def log_message(self, _format: str, *_args) -> None:
        pass


@contextlib.contextmanager
def running_server(database: Path) -> Iterator[tuple[str, int]]:
    """Run the real request handler on an ephemeral loopback port."""
    original_db = server.DB
    httpd = None
    thread = None
    try:
        server.DB = Path(database)
        httpd = server.ExplorerHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=httpd.serve_forever, name="fixture-http", daemon=True)
        thread.start()
        host, port = httpd.server_address[:2]
        yield str(host), int(port)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        server.DB = original_db


def http_request(
    address: tuple[str, int], path: str, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*address, timeout=10)
    try:
        connection.request(method, path, headers={"Accept": "application/json, text/html"})
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        connection.close()


@contextlib.contextmanager
def temporary_fixture() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="13f-fixture-") as directory:
        yield create_fixture_database(Path(directory) / "fixture.sqlite")
