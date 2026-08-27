from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from zoneinfo import ZoneInfo

import refresh_fund_signals as signals
from tests.support import temporary_fixture


class FakeResponse:
    """Minimal urlopen() stand-in: JSON body, content-type header, context manager."""

    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    class headers:  # noqa: N801 - mimics email.message.Message
        @staticmethod
        def get(_name: str) -> None:
            return None

        @staticmethod
        def get_content_type() -> str:
            return "application/json"

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc) -> None:
        return None


class SignalHelperTests(unittest.TestCase):
    def test_close_and_split_factor_validation(self) -> None:
        self.assertEqual(signals.parse_close("$1,234.50"), 1234.5)
        self.assertIsNone(signals.parse_close("N/A"))
        self.assertEqual(signals.closest_split_factor(9.98), 10.0)
        self.assertEqual(signals.closest_split_factor(0.101), 0.1)
        self.assertEqual(signals.closest_split_factor(1.02), 1.0)
        self.assertIsNone(signals.closest_split_factor(0.001))
        self.assertIsNone(signals.closest_split_factor(1.28))

    def test_completed_market_date_is_post_close_and_weekend_aware(self) -> None:
        eastern = ZoneInfo("America/New_York")
        before_close = datetime(2026, 8, 17, 16, 30, tzinfo=eastern)
        after_close = datetime(2026, 8, 17, 18, 0, tzinfo=eastern)
        self.assertEqual(signals.completed_market_date(before_close).isoformat(), "2026-08-14")
        self.assertEqual(signals.completed_market_date(after_close).isoformat(), "2026-08-17")

    def test_history_urls_try_stocks_then_etf_with_dot_class_symbols(self) -> None:
        urls = signals.history_urls("BRK/B", "2026-08-17", "2026-08-26")
        self.assertEqual(urls, [
            "https://api.nasdaq.com/api/quote/BRK.B/historical"
            "?assetclass=stocks&fromdate=2026-08-17&todate=2026-08-26&limit=5000",
            "https://api.nasdaq.com/api/quote/BRK.B/historical"
            "?assetclass=etf&fromdate=2026-08-17&todate=2026-08-26&limit=5000",
        ])
        self.assertEqual([url.count("assetclass=stocks") for url in urls], [1, 0])
        self.assertEqual(signals.nasdaq_symbol("BRK/B"), "BRK.B")
        for bad in ("", "brk/b", "A B", "X" * 33, "A/B/C%"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Unsupported Nasdaq symbol"):
                signals.history_urls(bad, "2026-08-17", "2026-08-26")

    def test_fetch_history_falls_back_to_etf_on_non_success_status(self) -> None:
        requested: list[str] = []

        def fake_urlopen(request, timeout=None):
            requested.append(request.full_url)
            if "assetclass=stocks" in request.full_url:
                return FakeResponse({"data": None, "status": {"rCode": 400, "bCodeMessage": [{"errorMessage": "Symbol not exists."}]}})
            return FakeResponse({"data": {"tradesTable": {"rows": [
                {"date": "08/26/2026", "close": "$766.08"},
                {"date": "08/25/2026", "close": "$770.10"},
                {"date": "08/10/2026", "close": "$1.00"},
                {"date": "bad", "close": "$1.00"},
                {"date": "08/24/2026", "close": "N/A"},
            ]}}, "status": {"rCode": 200}})

        with mock.patch.object(signals, "urlopen", fake_urlopen), mock.patch.object(signals.time, "sleep"):
            rows = signals.fetch_history("SPY", "2026-08-17", "2026-08-26", retries=1)
        self.assertEqual(rows, [("2026-08-25", 770.1), ("2026-08-26", 766.08)])
        self.assertEqual([url.split("assetclass=")[1].split("&")[0] for url in requested], ["stocks", "etf"])

    def test_fetch_history_does_not_try_etf_after_network_error(self) -> None:
        requested: list[str] = []

        def failing_urlopen(request, timeout=None):
            requested.append(request.full_url)
            raise OSError("connection reset")

        with mock.patch.object(signals, "urlopen", failing_urlopen), mock.patch.object(signals.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "AAPL: connection reset"):
                signals.fetch_history("AAPL", "2026-08-17", "2026-08-26", retries=2)
        self.assertEqual([url.count("assetclass=stocks") for url in requested], [1, 1])
        self.assertEqual(sleep.call_count, 1)

    def test_price_targets_add_display_tier_tickers(self) -> None:
        with temporary_fixture() as database:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as con:
                symbols, earliest, display_count = signals.price_targets(con, {"037833100": "AAPL"})
            self.assertEqual(symbols, {"AAPL", "MSFT", "NVDA", "TSLA"})
            self.assertEqual(earliest, "2025-06-30")
            self.assertEqual(display_count, 4)
            with closing(sqlite3.connect(database)) as con:
                con.execute("UPDATE securities SET ticker='' WHERE id=4")
                con.execute("UPDATE securities SET ticker=' nvda ' WHERE id=3")
                con.commit()
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as con:
                # Trusted symbols still come from the exact map even when the
                # displayed ticker differs; display-tier symbols are normalized.
                symbols, _, display_count = signals.price_targets(con, {"594918104": "MSFTX"})
            self.assertEqual(symbols, {"AAPL", "MSFT", "MSFTX", "NVDA"})
            self.assertEqual(display_count, 3)

    def test_only_exact_or_manual_ticker_mappings_are_trusted(self) -> None:
        payload = {
            "records": {
                "AAA": {"source": "openfigi", "ticker": " exact "},
                "BBB": {"source": "sec_name", "ticker": "GUESS"},
            },
            "manual_overrides": {"CCC": {"ticker": "manual"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tickers.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                signals.trusted_ticker_map(path), {"AAA": "EXACT", "CCC": "MANUAL"}
            )


if __name__ == "__main__":
    unittest.main()
