from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
import zipfile
from contextlib import closing
from pathlib import Path

import build_database
import refresh_market_caps
from tests.support import create_fixture_database


def event(
    accession: str,
    part_type: str,
    *,
    submission_type: str = "13F-HR",
    date: str = "2025-08-14",
    amendment: int | None = None,
    report_type: str = "13F HOLDINGS REPORT",
    confidential: str = "N",
) -> dict:
    return {
        "accession": accession,
        "part_type": part_type,
        "submission_type": submission_type,
        "filing_date_iso": date,
        "amendment_no": amendment,
        "cover": {"REPORTTYPE": report_type},
        "summary": {"ISCONFIDENTIALOMITTED": confidential},
    }


class ScalarBuildHelperTests(unittest.TestCase):
    def test_scalar_parsers(self) -> None:
        self.assertEqual(build_database.date_iso("14-Aug-2025"), "2025-08-14")
        self.assertEqual(build_database.integer("10"), 10)
        self.assertEqual(build_database.integer("10.0"), 10)
        self.assertIsNone(build_database.integer(""))
        self.assertEqual(build_database.number("1.25"), 1.25)
        with self.assertRaisesRegex(ValueError, "Invalid numeric"):
            build_database.number("not numeric")
        self.assertEqual(build_database.normalize_cik(" 123 "), "0000000123")

    def test_event_order_places_full_reset_before_same_day_additions(self) -> None:
        base = event("b", "RESTATEMENT", amendment=2)
        addition = event("a", "NEW HOLDINGS", amendment=2)
        self.assertLess(build_database.event_order(base), build_database.event_order(addition))

    def test_effective_chain_models_reset_addition_partial_and_notice(self) -> None:
        base = event("base", "BASE")
        addition = event("add", "NEW HOLDINGS", date="2025-08-15", amendment=1)
        parts, latest, coverage = build_database.effective_chain([addition, base])
        self.assertEqual([part["accession"] for part in parts], ["base", "add"])
        self.assertEqual(latest["accession"], "add")
        self.assertEqual(coverage, "COMPLETE")

        restatement = event("reset", "RESTATEMENT", date="2025-08-16", amendment=2)
        parts, _, coverage = build_database.effective_chain([base, addition, restatement])
        self.assertEqual([part["accession"] for part in parts], ["reset"])
        self.assertEqual(coverage, "COMPLETE")

        _, _, coverage = build_database.effective_chain([event("only-add", "NEW HOLDINGS")])
        self.assertEqual(coverage, "INFERRED")
        _, _, coverage = build_database.effective_chain([event("partial", "BASE", confidential="Y")])
        self.assertEqual(coverage, "PARTIAL")
        parts, latest, coverage = build_database.effective_chain(
            [event("notice", "NOTICE", submission_type="13F-NT")]
        )
        self.assertEqual(parts, [])
        self.assertEqual(latest["accession"], "notice")
        self.assertEqual(coverage, "NOTICE")

    def test_canonical_groups_classifies_amendments(self) -> None:
        archive = {
            "path": Path("fixture.zip"),
            "submissions": [
                {"PERIODOFREPORT": "30-JUN-2025", "SUBMISSIONTYPE": "13F-HR", "ACCESSION_NUMBER": "base", "CIK": "1", "FILING_DATE": "14-Aug-2025"},
                {"PERIODOFREPORT": "30-JUN-2025", "SUBMISSIONTYPE": "13F-HR/A", "ACCESSION_NUMBER": "add", "CIK": "1", "FILING_DATE": "15-Aug-2025"},
            ],
            "covers": {
                "base": {"ISAMENDMENT": "N"},
                "add": {"ISAMENDMENT": "Y", "AMENDMENTTYPE": "NEW HOLDINGS", "AMENDMENTNO": "1"},
            },
            "summaries": {"base": {}, "add": {}},
        }
        groups = build_database.canonical_groups([archive], {"30-JUN-2025"})
        rows = groups[("0000000001", "30-JUN-2025")]
        self.assertEqual([row["part_type"] for row in rows], ["BASE", "NEW HOLDINGS"])


class SnapshotLoaderTests(unittest.TestCase):
    def test_ticker_and_market_cap_snapshots_are_normalized_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ticker_file = root / "tickers.json"
            ticker_file.write_text(json.dumps({"tickers": {" abc ": " xyz ", "empty": ""}}))
            tickers, digest = build_database.load_ticker_map_snapshot(ticker_file)
            self.assertEqual(tickers, {"ABC": "XYZ"})
            self.assertEqual(digest, hashlib.sha256(ticker_file.read_bytes()).hexdigest())

            cap_file = root / "caps.json"
            cap_file.write_text(json.dumps({
                "source": "Fixture", "retrieved_at": "2026-01-01T00:00:00Z",
                "currency": "USD", "market_caps": {" aapl ": 100, "bad": 0},
            }))
            snapshot = build_database.load_market_cap_snapshot(cap_file)
            self.assertEqual(snapshot["market_caps"], {"AAPL": 100})
            self.assertEqual(snapshot["source"], "Fixture")
            self.assertEqual(snapshot["sha256"], hashlib.sha256(cap_file.read_bytes()).hexdigest())

    def test_invalid_snapshots_raise_contextual_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("[]")
            with self.assertRaisesRegex(ValueError, "ticker map"):
                build_database.load_ticker_map_snapshot(path)
            path.write_text(json.dumps({"market_caps": []}))
            with self.assertRaisesRegex(ValueError, "market-cap snapshot"):
                build_database.load_market_cap_snapshot(path)

    def test_sector_snapshot_is_normalized_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            absent = build_database.load_sector_snapshot(root / "missing.json")
            self.assertEqual(absent, {"source": "", "source_page": "", "retrieved_at": "",
                                      "sectors": {}, "sha256": ""})

            path = root / "sectors.json"
            path.write_text(json.dumps({
                "source": "Fixture", "source_page": "https://example.invalid",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "sectors": {
                    " aapl ": {"sector": " Technology ", "name": "Apple  Inc."},
                    "spy": {"sector": "ETF", "name": "SPDR S&P 500 ETF Trust"},
                    "brk/b": {"sector": "", "name": "Berkshire Hathaway Inc."},
                    "   ": {"sector": "Finance", "name": "blank symbol"},
                    "NONAME": {},
                },
            }))
            snapshot = build_database.load_sector_snapshot(path)
            self.assertEqual(snapshot["sectors"], {
                "AAPL": ("Technology", "Apple Inc."),
                "SPY": ("ETF", "SPDR S&P 500 ETF Trust"),
                "BRK/B": ("", "Berkshire Hathaway Inc."),
                "NONAME": ("", ""),
            })
            self.assertEqual(snapshot["source"], "Fixture")
            self.assertEqual(snapshot["source_page"], "https://example.invalid")
            self.assertEqual(snapshot["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

            for bad in ("not json", json.dumps({"sectors": []}), json.dumps({"sectors": {"AAPL": "Tech"}}),
                        json.dumps({"sectors": {"A\u0000B": {"sector": "Technology"}}}),
                        json.dumps({"sectors": {"AAA": {"sector": ["Tech"], "name": "x"}}}),
                        json.dumps({"sectors": {"AAA": {"sector": "Tech", "name": {"x": 1}}}}),
                        json.dumps({"sectors": {"BBB": {"sector": 12, "name": "x"}}}),
                        json.dumps({"sectors": {"CCC": {"sector": "Tech\u0001x", "name": "x"}}}),
                        json.dumps({"sectors": {"CCC": {"sector": "Tech", "name": "N\u0000ame"}}})):
                path.write_text(bad)
                with self.assertRaisesRegex(ValueError, "sector snapshot"):
                    build_database.load_sector_snapshot(path)

    def test_starred_watchlist_requires_twenty_unique_ciks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "funds.json"
            payload = {
                "name": "Fixture",
                "sources": [{"url": "https://example.invalid"}],
                "funds": [{"cik": str(index), "name": f"Fund {index}"} for index in range(1, 21)],
            }
            path.write_text(json.dumps(payload))
            result = build_database.load_starred_funds(path)
            self.assertEqual(len(result["ciks"]), 20)
            payload["funds"][19]["cik"] = "1"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "Invalid starred-fund watchlist"):
                build_database.load_starred_funds(path)

    def test_zip_member_lookup_and_tab_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nested/SAMPLE.TSV", "A\tB\n1\t2\n")
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(build_database.member(archive, "sample.tsv"), "nested/SAMPLE.TSV")
                self.assertEqual(list(build_database.rows_from_zip(archive, "sample.tsv")), [{"A": "1", "B": "2"}])
                with self.assertRaisesRegex(ValueError, "MISSING"):
                    build_database.member(archive, "MISSING.tsv")


class DisplayNameCleanupTests(unittest.TestCase):
    def test_conservative_suffix_and_parenthetical_removal(self) -> None:
        clean = refresh_market_caps.clean_display_name
        cases = {
            "Apple Inc. Common Stock": "Apple Inc.",
            "Alphabet Inc. Class A Common Stock": "Alphabet Inc.",
            "Berkshire Hathaway Inc. Class B Common Stock": "Berkshire Hathaway Inc.",
            "Ambev S.A. American Depositary Shares (Each representing 1 Common Share)": "Ambev S.A.",
            "Taiwan Semiconductor Manufacturing Company Ltd. American Depository Shares (each representing five ordinary shares)":
                "Taiwan Semiconductor Manufacturing Company Ltd.",
            # Nasdaq writes share ratios with a nested parenthetical; one level is allowed.
            "Grupo Aeromexico S.A.B. de C.V. American Depositary Shares (each representing ten (10) Common Shares)":
                "Grupo Aeromexico S.A.B. de C.V.",
            "Shell PLC American Depositary Shares (each representing two (2) Ordinary Shares)": "Shell PLC",
            # Two nested levels stay conservative: nothing is stripped.
            "Nested Corp. American Depositary Shares (each representing one (1 (one)) Share)":
                "Nested Corp. American Depositary Shares (each representing one (1 (one)) Share)",
            "Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred Stock":
                "Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred Stock",
            "Sample Acquisition Corp. Units": "Sample Acquisition Corp.",
            "Sample Acquisition Corp. Warrant": "Sample Acquisition Corp.",
            "Sample Acquisition Corp. Rights": "Sample Acquisition Corp.",
            "Example Holdings Ordinary Shares": "Example Holdings",
            "Example Holdings ORDINARY SHARE": "Example Holdings",
            # Only one suffix comes off; the rule is deliberately conservative.
            "Example Holdings Units Common Stock": "Example Holdings Units",
            "  Spaced   Name  Common Stock  ": "Spaced Name",
            # No leading whitespace means no suffix; an all-suffix name stays intact.
            "CommonStock": "CommonStock",
            "Common Stock": "Common Stock",
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean(raw), expected)

    def test_screener_row_shapes_and_symbol_hygiene(self) -> None:
        rows = [{"symbol": "x"}]
        self.assertEqual(refresh_market_caps.screener_rows({"data": {"rows": rows}}), rows)
        self.assertEqual(refresh_market_caps.screener_rows({"data": {"data": {"rows": rows}}}), rows)
        self.assertEqual(refresh_market_caps.screener_rows({"data": {"records": {"data": {"rows": rows}}}}), rows)
        self.assertEqual(refresh_market_caps.screener_rows({"rows": rows}), rows)
        self.assertEqual(refresh_market_caps.screener_rows({"data": None}), [])
        self.assertEqual(refresh_market_caps.clean_symbol(" brk/b "), "BRK/B")
        self.assertEqual(refresh_market_caps.clean_symbol("A\x01B"), "")
        self.assertEqual(refresh_market_caps.clean_symbol("X" * 33), "")
        self.assertEqual(refresh_market_caps.clean_symbol(None), "")

    def test_sector_snapshot_merges_etfs_with_stock_precedence(self) -> None:
        stock_rows = [{"symbol": "AAPL", "sector": "Technology", "name": "Apple Inc. Common Stock"},
                      {"symbol": "dual", "sector": "", "name": "Dual Listing Inc. Common Stock"},
                      {"symbol": "", "sector": "Finance", "name": "no symbol"}]
        etf_rows = [{"symbol": "SPY", "companyName": "SPDR S&P 500 ETF Trust Unit"},
                    {"symbol": "DUAL", "companyName": "Dual ETF"}]
        with unittest.mock.patch.object(refresh_market_caps, "MIN_STOCK_SECTOR_ROWS", 1):
            snapshot = refresh_market_caps.sector_snapshot(stock_rows, etf_rows, "2026-01-01T00:00:00+00:00")
        self.assertEqual(snapshot["sectors"], {
            "AAPL": {"sector": "Technology", "name": "Apple Inc."},
            "DUAL": {"sector": "", "name": "Dual Listing Inc."},
            "SPY": {"sector": "ETF", "name": "SPDR S&P 500 ETF Trust Unit"},
        })
        self.assertEqual(snapshot["sector_values"], ["ETF", "Technology"])
        self.assertEqual(snapshot["source"], refresh_market_caps.SOURCE_NAME)
        self.assertEqual(snapshot["etf_source_page"], refresh_market_caps.ETF_SOURCE_PAGE)
        with self.assertRaisesRegex(ValueError, "stock screener symbols"):
            refresh_market_caps.sector_snapshot(stock_rows, etf_rows, "2026-01-01T00:00:00+00:00")


# Ground truth for the fixture's dashboard rollups, derived from tests.support
# POSITIONS and the metric definitions: the weight universe is COMPLETE managers
# with a positive reported total (manager 3 is PARTIAL in period 2), manager
# weight is type-0 value over the manager's full total (put rows included),
# avg_weight is equal-weighted across the whole universe in percent, and new
# holders need COMPLETE filings on both sides of the adjacent pair.
DASHBOARD_PERIOD_EXPECTATIONS = {1: (None, 3, 0), 2: (1, 2, 2), 3: (2, 3, 2)}
# period -> security -> (holder_count, weight_sum, new_holder_count)
WEIGHT_EXPECTATIONS = {
    1: {1: (2, 1000 / 1500 + 250 / 550, 0), 2: (1, 500 / 1500, 0),
        3: (1, 150 / 150, 0), 4: (1, 300 / 550, 0)},
    2: {1: (2, 1500 / 2000 + 120 / 450, 0), 3: (1, 400 / 2000, 1), 4: (1, 330 / 450, 0)},
    3: {1: (1, 1600 / 2650, 0), 2: (1, 300 / 1300, 1),
        3: (2, 900 / 2650 + 800 / 800, 0), 4: (1, 1000 / 1300, 0)},
}
# Independent cross-check against the contract's rounded percentages.
AVG_WEIGHT_PERCENT = {
    (1, 1): 37.3737, (1, 2): 11.1111, (1, 3): 33.3333, (1, 4): 18.1818,
    (2, 1): 50.8333, (2, 3): 10.0, (2, 4): 36.6667,
    (3, 1): 20.1258, (3, 2): 7.6923, (3, 3): 44.6541, (3, 4): 25.6410,
}


class DashboardStatsTests(unittest.TestCase):
    def read_stats(self, con: sqlite3.Connection) -> tuple[dict, dict]:
        periods = {
            row[0]: (row[1], row[2], row[3])
            for row in con.execute("""SELECT period_id,previous_period_id,weight_manager_count,
              comparable_manager_count FROM dashboard_period_stats""")
        }
        weights = {
            (row[0], row[1]): row[2:]
            for row in con.execute("""SELECT period_id,security_id,holder_count,weight_sum,avg_weight,
              new_holder_count FROM security_weight_stats""")
        }
        return periods, weights

    def assert_ground_truth(self, con: sqlite3.Connection) -> None:
        periods, weights = self.read_stats(con)
        self.assertEqual(periods, DASHBOARD_PERIOD_EXPECTATIONS)
        expected_keys = {(period, security) for period, rows in WEIGHT_EXPECTATIONS.items() for security in rows}
        self.assertEqual(set(weights), expected_keys)
        for period, rows in WEIGHT_EXPECTATIONS.items():
            universe = DASHBOARD_PERIOD_EXPECTATIONS[period][1]
            for security, (holders, weight_sum, new_holders) in rows.items():
                with self.subTest(period=period, security=security):
                    stored_holders, stored_sum, stored_avg, stored_new = weights[(period, security)]
                    self.assertEqual(stored_holders, holders)
                    self.assertAlmostEqual(stored_sum, weight_sum, delta=1e-6)
                    self.assertAlmostEqual(stored_avg, 100.0 * weight_sum / universe, delta=1e-6)
                    self.assertAlmostEqual(stored_avg, AVG_WEIGHT_PERCENT[(period, security)], places=4)
                    self.assertEqual(stored_new, new_holders)

    def test_fixture_rollups_match_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                self.assert_ground_truth(con)
                indexes = {row[1] for row in con.execute("PRAGMA index_list(security_weight_stats)")}
                self.assertTrue({"security_weight_stats_rank", "security_weight_stats_new"} <= indexes)
                ranked = [row[0] for row in con.execute(
                    """SELECT security_id FROM security_weight_stats WHERE period_id=3
                    ORDER BY avg_weight DESC,holder_count DESC,security_id""")]
                self.assertEqual(ranked, [3, 4, 1, 2])
                initiations = con.execute(
                    "SELECT security_id,new_holder_count FROM security_weight_stats WHERE period_id=3 AND new_holder_count>0"
                ).fetchall()
                self.assertEqual(initiations, [(2, 1)])

    def test_materializer_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                before = self.read_stats(con)
                con.execute("UPDATE security_weight_stats SET avg_weight=avg_weight+1, new_holder_count=9")
                con.execute("UPDATE dashboard_period_stats SET weight_manager_count=0")
                build_database.materialize_dashboard_stats(con)
                self.assertEqual(self.read_stats(con), before)
                self.assert_ground_truth(con)
                build_database.validate_database(con, expected_period_count=3)

    def test_new_holder_without_weight_row_is_dropped_not_crashed(self) -> None:
        # A manager that is COMPLETE in both quarters but reports a zero total is
        # comparable (counts for new holders) yet outside the weight universe, so
        # a security only it holds gets no stats row at all.
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                con.execute("INSERT INTO managers VALUES (4,'0000000004','Delta Zero','TX',0)")
                con.execute("INSERT INTO securities VALUES (5,'000000000','ZERO VALUE CO','COM','','')")
                for period_id in (2, 3):
                    con.execute("""INSERT INTO manager_periods VALUES
                      (4,?,?,'14-FEB-2026','2026-02-14','fixture.zip','13F HOLDINGS REPORT','COMPLETE',0,1)""",
                                (period_id, f"0000000004-26-{period_id:06d}"))
                con.execute("INSERT INTO manager_period_stats VALUES (4,2,0,0,0)")
                con.execute("INSERT INTO manager_period_stats VALUES (4,3,1,1,0)")
                con.execute("INSERT INTO positions VALUES (4,3,5,0,0,0,10.0)")
                build_database.materialize_dashboard_stats(con)
                periods, weights = self.read_stats(con)
                self.assertEqual(periods[3], (2, 3, 3))
                self.assertNotIn((3, 5), weights)
                self.assertEqual(weights[(3, 2)][3], 1)
                self.assertEqual(weights[(3, 3)], WEIGHT_EXPECTATIONS[3][3][:1] + weights[(3, 3)][1:3] + (0,))


class DatabaseValidatorTests(unittest.TestCase):
    def test_fixture_passes_builder_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                build_database.validate_database(con, expected_period_count=3)

    def test_validator_rejects_corrupt_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                con.execute("UPDATE manager_period_stats SET total_value=total_value+1 WHERE manager_id=1 AND period_id=1")
                with self.assertRaisesRegex(ValueError, "stats value total|stats mismatches"):
                    build_database.validate_database(con, expected_period_count=3)

    def test_validator_is_read_only(self) -> None:
        # verify.py re-runs the validator under PRAGMA query_only, so none of
        # the checks may create temp tables or write.
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
                con.execute("PRAGMA query_only=ON")
                build_database.validate_database(con, expected_period_count=3)

    def test_validator_rejects_dashboard_drift(self) -> None:
        tampers = {
            "latest-period security weight mismatches":
                "UPDATE security_weight_stats SET avg_weight=avg_weight+1e-6 WHERE period_id=3 AND security_id=3",
            "latest-period new-holder mismatches":
                "UPDATE security_weight_stats SET new_holder_count=2 WHERE period_id=3 AND security_id=1",
            "dashboard weight-universe counts":
                "UPDATE dashboard_period_stats SET weight_manager_count=2 WHERE period_id=1",
            "dashboard comparable-manager counts":
                "UPDATE dashboard_period_stats SET comparable_manager_count=3 WHERE period_id=3",
            "dashboard previous-period links":
                "UPDATE dashboard_period_stats SET previous_period_id=1 WHERE period_id=3",
            "security weight stats row count":
                "DELETE FROM security_weight_stats WHERE period_id=1 AND security_id=2",
            "dashboard period stats row count":
                "DELETE FROM dashboard_period_stats WHERE period_id=1",
            "orphan security weight stats":
                "INSERT INTO security_weight_stats VALUES (3,99,1,0.5,50.0,0)",
            "empty sector tickers": "INSERT INTO sectors VALUES ('','Technology','Nameless')",
        }
        for label, sql in tampers.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = create_fixture_database(Path(directory) / "fixture.sqlite")
                with closing(sqlite3.connect(path)) as con:
                    con.execute(sql)
                    with self.assertRaisesRegex(ValueError, label):
                        build_database.validate_database(con, expected_period_count=3)


if __name__ == "__main__":
    unittest.main()
