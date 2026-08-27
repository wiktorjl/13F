from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

import build_database
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


if __name__ == "__main__":
    unittest.main()
