from __future__ import annotations

from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import refresh_fund_signals as signals


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
