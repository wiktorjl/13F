from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import server
from tests.support import create_fixture_database


class ParameterHelperTests(unittest.TestCase):
    def test_param_uses_first_value_and_strips(self) -> None:
        self.assertEqual(server.param({"q": ["  apple  ", "ignored"]}, "q"), "apple")
        self.assertEqual(server.param({}, "q", "fallback"), "fallback")

    def test_param_rejects_control_characters_and_excessive_length(self) -> None:
        for value in ("line\nbreak", "x" * (server.MAX_PARAMETER_LENGTH + 1)):
            with self.subTest(value=value[:20]):
                with self.assertRaisesRegex(ValueError, "Invalid search term"):
                    server.param({"search_term": [value]}, "search_term")

    def test_integer_enum_and_page_bounds(self) -> None:
        self.assertEqual(server.bounded_int({"n": ["4"]}, "n", 1, minimum=1, maximum=5), 4)
        for raw in ("0", "6", "1.5", "not-a-number"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                server.bounded_int({"n": [raw]}, "n", 1, minimum=1, maximum=5)
        self.assertEqual(server.enum_param({"kind": ["a"]}, "kind", {"a", "b"}), "a")
        with self.assertRaises(ValueError):
            server.enum_param({"kind": ["c"]}, "kind", {"a", "b"})
        self.assertEqual(server.page_params({}), (1, 50))
        self.assertEqual(server.page_params({"page": ["2"], "size": ["10"]}), (2, 10))

    def test_direction_and_literal_like_escaping(self) -> None:
        self.assertEqual(server.direction_sql({"direction": ["asc"]}), "ASC")
        self.assertEqual(server.direction_sql({}), "DESC")
        with self.assertRaises(ValueError):
            server.direction_sql({"direction": ["sideways"]})
        self.assertEqual(server.like_value(r"50%_off\today"), r"%50\%\_off\\today%")
        self.assertEqual(server.like_value("ABC", prefix=True), "ABC%")

    def test_nonnegative_bounds_and_range_validation(self) -> None:
        self.assertIsNone(server.nonnegative_bound({}, "amount"))
        self.assertEqual(server.nonnegative_bound({"amount": ["1.25"]}, "amount"), 1.25)
        self.assertEqual(server.nonnegative_bound({"amount": ["12"]}, "amount", integer=True), 12)
        for raw in ("-1", "nan", "inf", str(server.MAX_SQLITE_INTEGER + 1)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                server.nonnegative_bound({"amount": [raw]}, "amount")
        with self.assertRaises(ValueError):
            server.nonnegative_bound({"amount": ["1.5"]}, "amount", integer=True)
        server.validate_range(None, 1, "amount")
        server.validate_range(1, 1, "amount")
        with self.assertRaisesRegex(ValueError, "Minimum amount"):
            server.validate_range(2, 1, "amount")

    def test_period_helpers_use_date_order_and_default_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(path)) as con:
                con.row_factory = sqlite3.Row
                self.assertEqual(server.latest_period(con), "31-DEC-2025")
                current, previous = server.period_pair(con, "2025-12-31")
                self.assertEqual(current["label"], "31-DEC-2025")
                self.assertEqual(previous["label"], "30-SEP-2025")
                params = server.default_period_params(con, {"q": ["AAPL"]})
                self.assertEqual(params["period"], ["31-DEC-2025"])
                self.assertEqual(params["q"], ["AAPL"])
                with self.assertRaisesRegex(ValueError, "Unknown reporting period"):
                    server.period_pair(con, "1900-01-01")

    def test_comparison_status_sql_is_aliasable(self) -> None:
        expression = server.comparison_status_sql("now", "before")
        for token in ("NOT_COMPARABLE", "EXITED", "NEW", "INCREASED", "REDUCED", "UNCHANGED"):
            self.assertIn(token, expression)
        self.assertIn("now.manager_id", expression)
        self.assertIn("before.manager_id", expression)


class FreshnessTests(unittest.TestCase):
    def test_database_freshness_checks_every_input_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "snapshot.sqlite"
            archive = root / "fixture_form13f.zip"
            ticker_map = root / "tickers.json"
            market_caps = root / "caps.json"
            watchlist = root / "funds.json"
            archive.write_bytes(b"archive")
            ticker_map.write_bytes(b"tickers")
            market_caps.write_bytes(b"caps")
            watchlist.write_bytes(b"funds")

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            metadata = {
                "schema_version": server.SCHEMA_VERSION,
                "source_archives": json.dumps([archive.name]),
                "source_archive_hashes": json.dumps([{"name": archive.name, "sha256": digest(archive)}]),
                "ticker_map_sha256": digest(ticker_map),
                "market_cap_sha256": digest(market_caps),
                "fund_watchlist_sha256": digest(watchlist),
            }
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                con.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                con.commit()

            with (
                mock.patch.object(server, "ROOT", root),
                mock.patch.object(server, "DB", database),
                mock.patch.object(server, "TICKER_MAP", ticker_map),
                mock.patch.object(server, "MARKET_CAPS", market_caps),
                mock.patch.object(server, "STARRED_FUNDS", watchlist),
            ):
                self.assertTrue(server.database_is_current())
                market_caps.write_bytes(b"changed")
                self.assertFalse(server.database_is_current())


if __name__ == "__main__":
    unittest.main()
