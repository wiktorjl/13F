from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import server
from tests.support import create_fixture_database, http_request, running_server

DASHBOARD_KEYS = {
    "view", "period", "period_date", "comparison_period", "comparison_period_date", "horizon",
    "side", "sort", "direction", "unmapped", "price_date", "price_source", "price_available", "rows",
    "count", "page", "size",
}
DASHBOARD_ROW_KEYS = {
    "cusip", "ticker", "issuer", "name", "sector", "direction", "metric", "holders",
    "price", "price_date", "day_change", "ytd_change",
}
# Section 7 ground truth for the latest fixture quarter (31-DEC-2025, N=3).
HOLDINGS = (("NVDA", "up", 44.6541, 2), ("TSLA", "down", 25.6410, 1),
            ("AAPL", "down", 20.1258, 1), ("MSFT", "up", 7.6923, 1))
MOVERS = {
    (1, "gainers"): (("NVDA", 34.6541), ("MSFT", 7.6923)),
    (1, "losers"): (("AAPL", -30.7075), ("TSLA", -11.0256)),
    (2, "gainers"): (("NVDA", 11.3208), ("TSLA", 7.4592)),
    (2, "losers"): (("AAPL", -17.2479), ("MSFT", -3.4188)),
    (3, "gainers"): (), (3, "losers"): (), (4, "gainers"): (), (4, "losers"): (),
}
COMPARISON_LABELS = {1: "30-SEP-2025", 2: "30-JUN-2025", 3: None, 4: None}


PRICE_BARS = (
    ("AAPL", "2025-12-31", 250.0), ("AAPL", "2026-01-14", 260.0), ("AAPL", "2026-01-15", 265.0),
    ("TSLA", "2026-01-14", 400.0),
    ("NVDA", "2026-01-15", 180.0),  # mark-date close only: priced, but no day or year-to-date change
)
# Price-sort fixtures: AAPL alone priced (TSLA stale), and a second fully priced symbol whose
# day change is negative while its year-to-date change trails AAPL's (+5.0% vs +6.0%).
AAPL_ONLY_BARS = PRICE_BARS[:4]
TWO_PRICED_BARS = PRICE_BARS + (
    ("MSFT", "2025-12-31", 400.0), ("MSFT", "2026-01-14", 430.0), ("MSFT", "2026-01-15", 420.0),
)
DEFAULT_ORDER = ["NVDA", "TSLA", "AAPL", "MSFT"]

DOCTYPE = b"<!doctype html>"
DASHBOARD_MARKER = b'id="dashRows"'
# The About tab's static markup (contract section 2): the section and its three metric spans.
ABOUT_MARKERS = (b'id="dashAbout"', b'id="aboutQuarters"', b'id="aboutSpan"', b'id="aboutManagers"',
                 b'data-view="about"')
# Explorer-era markup and assets that must never come back.
EXPLORER_MARKERS = (b'id="fundsBody"', b"app.js", b"styles.css", b"/explorer")


def _price_cache(path: Path, bars: tuple[tuple[str, str, float], ...] = PRICE_BARS) -> Path:
    with closing(sqlite3.connect(path)) as con:
        con.execute("""CREATE TABLE bars (symbol TEXT NOT NULL COLLATE NOCASE,price_date TEXT NOT NULL,
          close REAL NOT NULL CHECK(close>0),PRIMARY KEY(symbol,price_date)) WITHOUT ROWID""")
        con.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
        con.executemany("INSERT INTO bars VALUES (?,?,?)", bars)
        con.executemany("INSERT INTO metadata VALUES (?,?)",
                        (("mark_date", "2026-01-15"), ("source", "Fixture prices")))
        con.commit()
    return path


def _fixture_with_security(directory: Path, ticker: str, stats: tuple[tuple[int, float], ...]) -> Path:
    """Fixture copy plus a fifth security with hand-placed weight-stats rows (period, avg_weight %)."""
    database = create_fixture_database(directory / "fixture.sqlite")
    with closing(sqlite3.connect(database)) as con:
        con.execute("INSERT INTO securities VALUES (5,'12345X104',?,'COM','',?)", (f"{ticker} CORP", ticker))
        con.executemany("INSERT INTO security_weight_stats VALUES (?,5,1,?,?,0)",
                        ((period_id, avg_weight / 100, avg_weight) for period_id, avg_weight in stats))
        con.commit()
    return database


def _fixture_with_tickers(directory: Path, tickers: tuple[str, ...]) -> Path:
    """Fixture copy plus extra latest-quarter securities (each a new holding) so a page fills up."""
    database = create_fixture_database(directory / "fixture.sqlite")
    with closing(sqlite3.connect(database)) as con:
        for index, ticker in enumerate(tickers, start=5):
            con.execute("INSERT INTO securities VALUES (?,?,?,'COM','',?)",
                        (index, f"{10000 + index}X104", f"{ticker} CORP", ticker))
            con.execute("INSERT INTO security_weight_stats VALUES (3,?,1,?,?,1)",
                        (index, index / 1000, index / 10))
        con.commit()
    return database


class HTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="13f-http-tests-")
        cls.database = create_fixture_database(Path(cls._temporary.name) / "fixture.sqlite")
        # Never read a real data/prices.sqlite: the dashboard must degrade to "unpriced".
        cls._price_cache_patch = mock.patch.object(
            server, "PRICE_CACHE", Path(cls._temporary.name) / "missing-prices.sqlite")
        cls._price_cache_patch.start()
        cls._server_context = running_server(cls.database)
        cls.address = cls._server_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)
        cls._price_cache_patch.stop()
        cls._temporary.cleanup()

    def request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        return http_request(self.address, path, method)

    def request_json(self, path: str, expected_status: int = 200) -> tuple[object, dict[str, str]]:
        status, headers, body = self.request(path)
        self.assertEqual(status, expected_status, body.decode("utf-8", errors="replace"))
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertEqual(int(headers["content-length"]), len(body))
        return json.loads(body), headers

    def assert_keys(self, value: dict, keys: set[str]) -> None:
        self.assertTrue(keys.issubset(value), f"missing {sorted(keys - set(value))}; got {sorted(value)}")

    def test_exact_static_allowlist_get_and_head(self) -> None:
        # path -> (body prefix, served document or None for JS/CSS)
        allowed = {
            "/": (DOCTYPE, "dashboard.html"),
            "/initiations": (DOCTYPE, "dashboard.html"),
            "/movers": (DOCTYPE, "dashboard.html"),
            "/about": (DOCTYPE, "dashboard.html"),
            "/movers?horizon=2&side=losers&page=2": (DOCTYPE, "dashboard.html"),
            "/?sort=ticker&direction=asc": (DOCTYPE, "dashboard.html"),
            "/about?ignored=1": (DOCTYPE, "dashboard.html"),  # query state is ignored client-side
            # Legacy aliases from before the dashboard became the landing page.
            "/dashboard": (DOCTYPE, "dashboard.html"),
            "/dashboard/initiations": (DOCTYPE, "dashboard.html"),
            "/dashboard/movers": (DOCTYPE, "dashboard.html"),
            "/dashboard/movers?horizon=2&side=losers&page=2": (DOCTYPE, "dashboard.html"),
            "/dashboard.html": (DOCTYPE, "dashboard.html"),
            "/dashboard.js": (b"'use strict';", None),
            "/dashboard.css": (b":root", None),
            "/dashboard.js?v=fixture": (b"'use strict';", None),
        }
        for path, (prefix, document) in allowed.items():
            with self.subTest(method="GET", path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(prefix, body[:600], body[:80])
                self.assertEqual(int(headers["content-length"]), len(body))
                # Every static response revalidates; only the API is no-store.
                self.assertEqual(headers.get("cache-control"), "no-cache")
                if document is not None:
                    self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
                    # Without a base path the document is served byte-for-byte.
                    self.assertEqual(body, (server.ROOT / document).read_bytes())
                    self.assertIn(DASHBOARD_MARKER, body)
                    for marker in EXPLORER_MARKERS:
                        self.assertNotIn(marker, body)
            with self.subTest(method="HEAD", path=path):
                status, headers, body = self.request(path, "HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertGreater(int(headers["content-length"]), 0)
                self.assertEqual(headers.get("cache-control"), "no-cache")

        denied = (
            "/server.py",
            "/build_database.py",
            "/data/13f.sqlite",
            "/tests/support.py",
            "/favicon.ico",
            # The explorer and its assets were removed; nothing serves them any more.
            "/explorer",
            "/explorer?view=stocks",
            "/explorer/",
            "/explorer/x",
            "/Explorer",
            "/EXPLORER",
            "/index.html",
            "/app.js",
            "/app.js?v=fixture",
            "/styles.css",
            "/APP.JS",
            "/app%2ejs",
            "/%2e%2e/server.py",
            "/about/",
            "/about/x",
            "/About",
            "/ABOUT",
            "/about.html",
            "/dashboard/about",
            "/initiations/",
            "/initiations/x",
            "/movers/",
            "/movers/x",
            "/Movers",
            "/dashboard/",
            "/dashboard/x",
            "/dashboard/movers/",
            "/dashboard/initiations/extra",
            "/DASHBOARD",
            "/Dashboard",
            "/dashboard.html/extra",
            "/dashboard.js/extra",
            "/dashboard%2ejs",
        )
        for method in ("GET", "HEAD"):
            for path in denied:
                with self.subTest(method=method, path=path):
                    status, _, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    if method == "HEAD":
                        self.assertEqual(body, b"")

    def test_about_route_serves_the_dashboard_document_with_the_about_section(self) -> None:
        # Contract section 2: /about is a canonical dashboard route whose markup lives statically
        # in dashboard.html (a hidden section after the pager, plus a fourth nav link).
        status, headers, body = self.request("/about")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assertEqual(body, (server.ROOT / "dashboard.html").read_bytes())
        for marker in ABOUT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        self.assertIn(b'href="/about"', body)
        self.assertLess(body.index(b'id="dashPager"'), body.index(b'id="dashAbout"'))
        self.assertIn(b"Nothing here is investment advice.", body)
        # The same bytes answer at every dashboard route: the About tab is client-side state.
        for path in ("/", "/initiations", "/movers", "/dashboard"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[2], body)
        # /about/ is not a route (only the root tolerates a trailing slash, and only under a prefix).
        for path in ("/about/", "/about/index.html", "/About"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_security_headers_cover_success_and_error_responses(self) -> None:
        paths = (("/", 200), ("/api/meta", 200), ("/api/not-real", 404), ("/server.py", 404),
                 ("/about", 200), ("/movers", 200), ("/dashboard", 200), ("/dashboard.js", 200),
                 ("/api/dashboard", 200), ("/api/dashboard?view=nope", 400), ("/explorer", 404),
                 ("/app.js", 404), ("/about/", 404), ("/movers/", 404))
        expected = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
            "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "cross-origin-resource-policy": "same-origin",
        }
        for path, expected_status in paths:
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, expected_status)
                for name, value in expected.items():
                    self.assertEqual(headers.get(name), value)
                policy = headers.get("content-security-policy", "")
                for directive in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
                    self.assertIn(directive, policy)
                self.assertNotIn("unsafe-inline", policy)
                self.assertNotIn("Python", headers.get("server", ""))

    def test_head_is_rejected_for_api_without_a_body(self) -> None:
        status, headers, body = self.request("/api/meta", "HEAD")
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertGreater(int(headers["content-length"]), 0)

    def test_request_target_and_field_count_limits(self) -> None:
        status, _, body = self.request("/api/meta?" + "x" * server.MAX_QUERY_LENGTH)
        self.assertEqual(status, 414)
        self.assertIn("too long", json.loads(body)["error"].lower())
        query = "&".join(f"x{index}=1" for index in range(65))
        payload, _ = self.request_json("/api/meta?" + query, expected_status=400)
        self.assertIn("field", payload["error"].lower())

    def test_meta_response_schema(self) -> None:
        # A flat object: the build metadata rows plus the period list and latest-quarter totals.
        # The Docker healthcheck only needs the 200; the About tab reads the counts and periods.
        payload, headers = self.request_json("/api/meta")
        self.assertIsInstance(payload, dict)
        self.assert_keys(payload, {
            "schema_version", "periods", "latest_period", "period_count", "holding_count",
            "total_value", "distinct_managers", "distinct_issuers", "built_at",
        })
        self.assertTrue(all(not isinstance(value, dict) for value in payload.values()))
        self.assertEqual(payload["schema_version"], server.SCHEMA_VERSION)
        self.assertEqual(payload["latest_period"], "31-DEC-2025")
        self.assertEqual(payload["period_count"], 3)
        self.assertEqual(len(payload["periods"]), 3)
        self.assertEqual([period["label"] for period in payload["periods"]],
                         ["31-DEC-2025", "30-SEP-2025", "30-JUN-2025"])
        self.assertEqual(payload["periods"][0], {"label": "31-DEC-2025", "period_date": "2025-12-31"})
        # Latest-quarter totals from period_stats: six positions across all three managers.
        self.assertEqual((payload["holding_count"], payload["total_value"], payload["distinct_managers"],
                          payload["distinct_issuers"]), (6, 4_750, 3, 4))
        self.assertEqual(payload["built_at"], "2026-01-15T12:00:00+00:00")
        # Explorer-era fields are gone along with the fund-signals sidecar.
        for removed in ("forms", "states", "signal_available", "signal_price_source", "signal_price_date",
                        "research_fund_cutoff"):
            self.assertNotIn(removed, payload)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_unknown_query_parameters_are_rejected_before_dispatch(self) -> None:
        paths = (
            "/api/meta?x=1", "/api/meta?period=31-DEC-2025", "/api/dashboard?x=1",
            "/api/dashboard?view=holdings&period=31-DEC-2025", "/api/dashboard?view=holdings&q=AAPL",
        )
        for path in paths:
            with self.subTest(path=path):
                payload, _ = self.request_json(path, expected_status=400)
                self.assertIn("Unknown query parameter", payload["error"])

    def test_removed_explorer_endpoints_are_json_404(self) -> None:
        for path in ("/api/holdings", "/api/aggregate?group=issuer", "/api/funds", "/api/suggest?kind=manager&q=A",
                     "/api/stock-detail?cusip=037833100", "/api/fund-detail?cik=1", "/api/net-adds?metric=value",
                     "/api/does-not-exist", "/api/", "/api/meta/", "/api/Meta", "/api/dashboard/"):
            with self.subTest(path=path):
                payload, _ = self.request_json(path, expected_status=404)
                self.assertEqual(payload, {"error": "Not found"})

    def assert_unpriced(self, payload: dict) -> None:
        self.assertFalse(payload["price_available"])
        self.assertEqual((payload["price_date"], payload["price_source"]), ("", ""))
        for row in payload["rows"]:
            self.assertEqual(
                (row["price"], row["price_date"], row["day_change"], row["ytd_change"]), (None, "", None, None))

    def test_dashboard_holdings_view_matches_the_fixture_ground_truth(self) -> None:
        payload, headers = self.request_json("/api/dashboard")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(set(payload), DASHBOARD_KEYS)
        self.assertEqual(
            (payload["view"], payload["period"], payload["period_date"], payload["comparison_period"],
             payload["comparison_period_date"], payload["horizon"], payload["side"], payload["count"],
             payload["page"], payload["size"]),
            ("holdings", "31-DEC-2025", "2025-12-31", "30-SEP-2025", "2025-09-30", 1, "gainers", 4, 1, 100))
        self.assert_unpriced(payload)
        self.assertEqual(len(payload["rows"]), 4)
        for row, (ticker, direction, metric, holders) in zip(payload["rows"], HOLDINGS):
            with self.subTest(ticker=ticker):
                self.assertEqual(set(row), DASHBOARD_ROW_KEYS)
                self.assertEqual((row["ticker"], row["direction"], row["holders"]), (ticker, direction, holders))
                self.assertAlmostEqual(row["metric"], metric, places=3)
        first = payload["rows"][0]
        self.assertEqual((first["cusip"], first["issuer"], first["name"], first["sector"]),
                         ("67066G104", "NVIDIA CORP", "NVIDIA Corporation", "Technology"))
        self.assertEqual((payload["rows"][1]["name"], payload["rows"][1]["sector"]),
                         ("Tesla, Inc.", "Consumer Discretionary"))
        # horizon/side are accepted for every view but only steer movers.
        steered, _ = self.request_json("/api/dashboard?view=holdings&horizon=3&side=losers&page=1&size=100")
        self.assertEqual((steered["horizon"], steered["side"], steered["comparison_period"]),
                         (3, "losers", "30-SEP-2025"))
        self.assertEqual(steered["rows"], payload["rows"])
        beyond, _ = self.request_json("/api/dashboard?view=holdings&page=2&size=10")
        self.assertEqual((beyond["rows"], beyond["count"], beyond["page"], beyond["size"]), ([], 4, 2, 10))

    def test_dashboard_initiations_view_counts_first_time_holders(self) -> None:
        payload, _ = self.request_json("/api/dashboard?view=initiations&page=1&size=100")
        self.assertEqual(set(payload), DASHBOARD_KEYS)
        self.assertEqual((payload["view"], payload["comparison_period"], payload["count"]),
                         ("initiations", "30-SEP-2025", 1))
        self.assert_unpriced(payload)
        row = payload["rows"][0]
        self.assertEqual(set(row), DASHBOARD_ROW_KEYS)
        self.assertEqual((row["ticker"], row["metric"], row["direction"], row["holders"], row["name"]),
                         ("MSFT", 1, "up", 1, "Microsoft Corporation"))
        self.assertIsInstance(row["metric"], int)

    def test_dashboard_movers_view_for_both_sides_and_every_horizon(self) -> None:
        for (horizon, side), expected in MOVERS.items():
            with self.subTest(horizon=horizon, side=side):
                payload, _ = self.request_json(
                    "/api/dashboard?" + urlencode({"view": "movers", "horizon": horizon, "side": side,
                                                   "page": 1, "size": 100}))
                self.assertEqual(set(payload), DASHBOARD_KEYS)
                self.assertEqual((payload["view"], payload["horizon"], payload["side"], payload["period"]),
                                 ("movers", horizon, side, "31-DEC-2025"))
                self.assertEqual(payload["comparison_period"], COMPARISON_LABELS[horizon])
                self.assertEqual(payload["comparison_period_date"] is None, COMPARISON_LABELS[horizon] is None)
                self.assertEqual((payload["count"], len(payload["rows"])), (len(expected), len(expected)))
                self.assert_unpriced(payload)
                for row, (ticker, metric) in zip(payload["rows"], expected):
                    self.assertEqual(set(row), DASHBOARD_ROW_KEYS)
                    self.assertEqual(row["ticker"], ticker)
                    self.assertEqual(row["direction"], "up" if side == "gainers" else "down")
                    self.assertAlmostEqual(row["metric"], metric, places=3)
        # Defaults: movers without horizon/side is the 1Q gainers list.
        defaults, _ = self.request_json("/api/dashboard?view=movers")
        self.assertEqual([row["ticker"] for row in defaults["rows"]], ["NVDA", "MSFT"])

    def test_dashboard_prices_come_from_the_offline_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="13f-prices-") as directory:
            cache = _price_cache(Path(directory) / "prices.sqlite")
            with mock.patch.object(server, "PRICE_CACHE", cache):
                payload, _ = self.request_json("/api/dashboard?view=holdings&page=1&size=100")
                self.assertTrue(payload["price_available"])
                self.assertEqual((payload["price_date"], payload["price_source"]),
                                 ("2026-01-15", "Fixture prices"))
                rows = {row["ticker"]: row for row in payload["rows"]}
                apple = rows["AAPL"]
                self.assertEqual((apple["price"], apple["price_date"]), (265.0, "2026-01-15"))
                self.assertAlmostEqual(apple["day_change"], 100 * (265 / 260 - 1), places=4)
                self.assertAlmostEqual(apple["ytd_change"], 6.0, places=4)
                # TSLA has no close on the mark date (stale); MSFT has no bars at all.
                for ticker in ("TSLA", "MSFT"):
                    row = rows[ticker]
                    self.assertEqual((row["price"], row["price_date"], row["day_change"], row["ytd_change"]),
                                     (None, "", None, None))
                # NVDA closes on the mark date only: priced, but both changes lack a base.
                nvidia = rows["NVDA"]
                self.assertEqual((nvidia["price"], nvidia["price_date"], nvidia["day_change"], nvidia["ytd_change"]),
                                 (180.0, "2026-01-15", None, None))
                movers, _ = self.request_json("/api/dashboard?view=movers&horizon=1&side=losers&page=1&size=100")
                self.assertTrue(movers["price_available"])
                self.assertEqual(movers["rows"][0]["price"], 265.0)
                # /api/meta never attaches the price cache and stays a flat object either way.
                meta, _ = self.request_json("/api/meta")
                self.assertEqual(meta["latest_period"], "31-DEC-2025")
            broken = Path(directory) / "broken.sqlite"
            broken.write_bytes(b"not a database")
            with mock.patch.object(server, "PRICE_CACHE", broken):
                payload, _ = self.request_json("/api/dashboard?view=holdings&page=1&size=100")
                self.assert_unpriced(payload)
        # The class default (absent cache) is restored afterwards.
        payload, _ = self.request_json("/api/dashboard?view=holdings&page=1&size=100")
        self.assert_unpriced(payload)

    def tickers(self, query: str) -> list[str]:
        payload, _ = self.request_json("/api/dashboard?" + query)
        self.assertEqual(set(payload), DASHBOARD_KEYS)
        return [row["ticker"] for row in payload["rows"]]

    def test_dashboard_echoes_the_effective_sort_and_direction(self) -> None:
        for query, expected in (
            ("", ("metric", "desc")),
            ("view=initiations", ("metric", "desc")),
            ("view=movers&side=gainers", ("metric", "desc")),
            ("view=movers&side=losers", ("metric", "asc")),  # losers run most-negative first
            ("view=movers&side=losers&direction=desc", ("metric", "desc")),
            ("view=movers&side=losers&sort=ticker", ("ticker", "asc")),
            ("view=holdings&side=losers", ("metric", "desc")),  # only movers losers flip the default
            ("view=holdings&sort=price", ("price", "desc")),
            ("view=holdings&sort=name&direction=asc", ("name", "asc")),
        ):
            with self.subTest(query=query):
                payload, _ = self.request_json("/api/dashboard?" + query)
                self.assertEqual((payload["sort"], payload["direction"]), expected)

    def test_dashboard_sorts_text_columns_case_insensitively_without_changing_count(self) -> None:
        self.assertEqual(self.tickers("view=holdings"), DEFAULT_ORDER)
        self.assertEqual(self.tickers("view=holdings&sort=ticker&direction=asc"), ["AAPL", "MSFT", "NVDA", "TSLA"])
        self.assertEqual(self.tickers("view=holdings&sort=ticker&direction=desc"), ["TSLA", "NVDA", "MSFT", "AAPL"])
        names, _ = self.request_json("/api/dashboard?view=holdings&sort=name&direction=asc")
        self.assertEqual([row["name"] for row in names["rows"]],
                         ["Apple Inc.", "Microsoft Corporation", "NVIDIA Corporation", "Tesla, Inc."])
        self.assertEqual(self.tickers("view=holdings&sort=name&direction=desc"), ["TSLA", "NVDA", "MSFT", "AAPL"])
        # Equal sectors fall back to the view's default order (the three Technology rows).
        self.assertEqual(self.tickers("view=holdings&sort=sector&direction=asc"), ["TSLA", "NVDA", "AAPL", "MSFT"])
        self.assertEqual(self.tickers("view=holdings&sort=sector&direction=desc"), ["NVDA", "AAPL", "MSFT", "TSLA"])
        for query in ("view=holdings&sort=ticker&direction=asc", "view=holdings&sort=sector&direction=desc",
                      "view=holdings&sort=price&direction=asc"):
            payload, _ = self.request_json("/api/dashboard?" + query)
            self.assertEqual((payload["count"], len(payload["rows"])), (4, 4))
        beyond, _ = self.request_json("/api/dashboard?view=holdings&sort=ticker&direction=asc&page=2&size=10")
        self.assertEqual((beyond["rows"], beyond["count"], beyond["sort"], beyond["direction"]),
                         ([], 4, "ticker", "asc"))

    def test_dashboard_metric_sort_flips_each_view_and_losers_default_ascending(self) -> None:
        self.assertEqual(self.tickers("view=holdings&sort=metric&direction=asc"), ["MSFT", "AAPL", "TSLA", "NVDA"])
        self.assertEqual(self.tickers("view=holdings&sort=metric&direction=desc"), DEFAULT_ORDER)
        self.assertEqual(self.tickers("view=movers&side=gainers"), ["NVDA", "MSFT"])
        self.assertEqual(self.tickers("view=movers&side=gainers&direction=asc"), ["MSFT", "NVDA"])
        self.assertEqual(self.tickers("view=movers&side=losers"), ["AAPL", "TSLA"])
        self.assertEqual(self.tickers("view=movers&side=losers&sort=metric&direction=asc"), ["AAPL", "TSLA"])
        # Least negative first, and the same rows either way.
        self.assertEqual(self.tickers("view=movers&side=losers&sort=metric&direction=desc"), ["TSLA", "AAPL"])
        self.assertEqual(self.tickers("view=movers&side=losers&sort=ticker"), ["AAPL", "TSLA"])
        self.assertEqual(self.tickers("view=movers&side=losers&sort=ticker&direction=desc"), ["TSLA", "AAPL"])
        self.assertEqual(self.tickers("view=movers&horizon=2&side=losers&sort=name&direction=asc"), ["AAPL", "MSFT"])
        initiations, _ = self.request_json("/api/dashboard?view=initiations&sort=ticker&direction=desc")
        self.assertEqual(([row["ticker"] for row in initiations["rows"]], initiations["count"],
                          initiations["sort"], initiations["direction"]), (["MSFT"], 1, "ticker", "desc"))

    def test_dashboard_price_sorts_without_a_cache_keep_the_default_order(self) -> None:
        for sort in ("price", "day", "ytd"):
            for direction in ("asc", "desc"):
                with self.subTest(sort=sort, direction=direction):
                    payload, _ = self.request_json(f"/api/dashboard?view=holdings&sort={sort}&direction={direction}")
                    self.assert_unpriced(payload)
                    self.assertEqual([row["ticker"] for row in payload["rows"]], DEFAULT_ORDER)
                    self.assertEqual((payload["sort"], payload["direction"], payload["count"]), (sort, direction, 4))
                    self.assertTrue(all(set(row) == DASHBOARD_ROW_KEYS for row in payload["rows"]))
        losers, _ = self.request_json("/api/dashboard?view=movers&side=losers&sort=ytd")
        self.assertEqual(([row["ticker"] for row in losers["rows"]], losers["direction"]), (["AAPL", "TSLA"], "asc"))

    def test_invalid_numerics_enums_ranges_and_pages(self) -> None:
        cases = (
            "/api/dashboard?view=funds",
            "/api/dashboard?view=about",
            "/api/dashboard?view=HOLDINGS",
            "/api/dashboard?side=up",
            "/api/dashboard?view=movers&side=Gainers",
            "/api/dashboard?horizon=0",
            "/api/dashboard?horizon=5",
            "/api/dashboard?horizon=1.5",
            "/api/dashboard?horizon=abc",
            "/api/dashboard?view=holdings&horizon=9",
            "/api/dashboard?page=0",
            f"/api/dashboard?page={server.MAX_PAGE + 1}",
            "/api/dashboard?page=abc",
            "/api/dashboard?size=9",
            "/api/dashboard?size=201",
            "/api/dashboard?view=line%0Abreak",
            "/api/dashboard?view=" + "x" * (server.MAX_PARAMETER_LENGTH + 1),
            "/api/dashboard?sort=bogus",
            "/api/dashboard?sort=Metric",
            "/api/dashboard?sort=avg_weight",
            "/api/dashboard?direction=up",
            "/api/dashboard?direction=ASC",
            "/api/dashboard?view=movers&side=losers&direction=descending",
            "/api/dashboard?sort=price&direction=sideways",
            "/api/dashboard?unmapped=all",
            "/api/dashboard?unmapped=Include",
            "/api/dashboard?view=movers&unmapped=1",
        )
        for path in cases:
            with self.subTest(path=path):
                payload, _ = self.request_json(path, expected_status=400)
                self.assertIsInstance(payload.get("error"), str)
                self.assertTrue(payload["error"])


class DashboardFixtureVariantTests(unittest.TestCase):
    """Contract edges the shared fixture cannot reach; each test serves its own database copy."""

    def dashboard(self, database: Path, query: dict, cache: Path | None = None) -> dict:
        cache = cache or database.parent / "missing-prices.sqlite"
        with mock.patch.object(server, "PRICE_CACHE", cache), running_server(database) as address:
            status, _, body = http_request(address, "/api/dashboard?" + urlencode(query))
        self.assertEqual(status, 200, body.decode("utf-8", errors="replace"))
        return json.loads(body)

    def test_initiations_direction_compares_new_holder_counts_with_the_prior_quarter(self) -> None:
        # Fresh Initiations: up when a security gained more first-time holders than in the prior
        # quarter, down when fewer, flat when equal; a missing prior row counts as zero.
        with tempfile.TemporaryDirectory(prefix="13f-initiations-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(database)) as con:
                rows = (("FEWER", 5, 3, 1), ("SAME", 6, 2, 2), ("MORE", 7, 1, 4))
                for ticker, security_id, previous_new, current_new in rows:
                    con.execute("INSERT INTO securities VALUES (?,?,?,'COM','',?)",
                                (security_id, f"{10000 + security_id}X104", f"{ticker} CORP", ticker))
                    con.execute("INSERT INTO security_weight_stats VALUES (2,?,1,0.01,1.0,?)",
                                (security_id, previous_new))
                    con.execute("INSERT INTO security_weight_stats VALUES (3,?,1,0.01,1.0,?)",
                                (security_id, current_new))
                con.commit()
            payload = self.dashboard(database, {"view": "initiations"})
            self.assertEqual([(row["ticker"], row["metric"], row["direction"]) for row in payload["rows"]],
                             [("MORE", 4, "up"), ("SAME", 2, "flat"), ("MSFT", 1, "up"), ("FEWER", 1, "down")])

    def test_movers_include_securities_fully_exited_since_the_comparison_period(self) -> None:
        # Section 1: movers cover the UNION of P and P-k, a missing side counting as zero.  A
        # security present only in P-k is a loser with change == -avg_weight(P-k) and no holders.
        with tempfile.TemporaryDirectory(prefix="13f-exited-") as directory:
            database = _fixture_with_security(Path(directory), "EXIT", ((1, 5.0), (2, 12.5)))
            losers = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "losers"})
            self.assertEqual(losers["count"], 3)
            self.assertEqual([(row["ticker"], row["direction"], row["holders"]) for row in losers["rows"]],
                             [("AAPL", "down", 1), ("EXIT", "down", 0), ("TSLA", "down", 1)])
            exited = losers["rows"][1]
            self.assertEqual(exited["metric"], -12.5)
            self.assertEqual((exited["cusip"], exited["issuer"], exited["name"], exited["sector"]),
                             ("12345X104", "EXIT CORP", "EXIT CORP", ""))
            two_quarters = self.dashboard(database, {"view": "movers", "horizon": 2, "side": "losers"})
            self.assertEqual([row["ticker"] for row in two_quarters["rows"]], ["AAPL", "EXIT", "MSFT"])
            self.assertEqual(two_quarters["rows"][1]["metric"], -5.0)
            gainers = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "gainers"})
            self.assertEqual([row["ticker"] for row in gainers["rows"]], ["NVDA", "MSFT"])
            holdings = self.dashboard(database, {"view": "holdings"})
            self.assertEqual(holdings["count"], 4)
            self.assertNotIn("EXIT", [row["ticker"] for row in holdings["rows"]])

    def test_movers_exclude_zero_change_from_both_sides(self) -> None:
        # Section 1: change == 0 belongs to neither side, and Top Holdings marks it flat.
        with tempfile.TemporaryDirectory(prefix="13f-zero-") as directory:
            database = _fixture_with_security(Path(directory), "FLAT", ((2, 9.0), (3, 9.0)))
            for side in ("gainers", "losers"):
                with self.subTest(side=side):
                    payload = self.dashboard(database, {"view": "movers", "horizon": 1, "side": side})
                    tickers = [row["ticker"] for row in payload["rows"]]
                    self.assertNotIn("FLAT", tickers)
                    self.assertEqual(payload["count"], len(tickers))
                    self.assertEqual(tickers, [ticker for ticker, _ in MOVERS[(1, side)]])
            holdings = self.dashboard(database, {"view": "holdings"})
            self.assertEqual(holdings["count"], 5)
            self.assertEqual([(row["ticker"], row["direction"]) for row in holdings["rows"]],
                             [("NVDA", "up"), ("TSLA", "down"), ("AAPL", "down"), ("FLAT", "flat"), ("MSFT", "up")])

    def test_holdings_direction_is_flat_when_only_one_period_exists(self) -> None:
        # Section 1: without a P-1 every Top Holdings row is flat and movers have nothing to compare.
        with tempfile.TemporaryDirectory(prefix="13f-single-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(database)) as con:
                con.execute("DELETE FROM periods WHERE id<>3")
                con.execute("DELETE FROM security_weight_stats WHERE period_id<>3")
                con.execute("DELETE FROM dashboard_period_stats WHERE period_id<>3")
                con.execute("UPDATE dashboard_period_stats SET previous_period_id=NULL")
                con.commit()
            holdings = self.dashboard(database, {"view": "holdings"})
            self.assertEqual((holdings["period"], holdings["comparison_period"],
                              holdings["comparison_period_date"], holdings["count"]),
                             ("31-DEC-2025", None, None, 4))
            self.assertEqual([(row["ticker"], row["direction"]) for row in holdings["rows"]],
                             [("NVDA", "flat"), ("TSLA", "flat"), ("AAPL", "flat"), ("MSFT", "flat")])
            for horizon in (1, 4):
                movers = self.dashboard(database, {"view": "movers", "horizon": horizon, "side": "gainers"})
                self.assertEqual((movers["rows"], movers["count"], movers["comparison_period"]), ([], 0, None))
            # /api/meta still describes the single remaining quarter.
            with running_server(database) as address:
                status, _, body = http_request(address, "/api/meta")
            self.assertEqual(status, 200)
            meta = json.loads(body)
            self.assertEqual((meta["period_count"], meta["latest_period"], len(meta["periods"])),
                             (1, "31-DEC-2025", 1))

    def test_blank_ticker_and_unmapped_ticker_fall_back_to_issuer(self) -> None:
        # Section 5: name falls back to the SEC issuer and sector to '' when the screener has no
        # row; a blank ticker never looks up sectors or prices even when such rows exist.
        with tempfile.TemporaryDirectory(prefix="13f-naming-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(database)) as con:
                con.execute("UPDATE securities SET ticker='' WHERE cusip='88160R101'")  # TSLA: unmapped CUSIP
                con.execute("DELETE FROM sectors WHERE ticker='MSFT'")  # MSFT: no screener row
                con.execute("INSERT INTO sectors VALUES ('','Bogus','Blank Ticker Trap')")
                con.commit()
            cache = _price_cache(Path(directory) / "prices.sqlite", (
                ("", "2026-01-14", 1.0), ("", "2026-01-15", 2.0),
                ("MSFT", "2025-12-31", 400.0), ("MSFT", "2026-01-14", 410.0), ("MSFT", "2026-01-15", 420.0),
            ))
            payload = self.dashboard(database, {"view": "holdings", "unmapped": "include"}, cache)
            self.assertTrue(payload["price_available"])
            self.assertEqual((payload["count"], payload["unmapped"]), (4, "include"))
            rows = {row["cusip"]: row for row in payload["rows"]}
            tesla = rows["88160R101"]
            self.assertEqual((tesla["ticker"], tesla["issuer"], tesla["name"], tesla["sector"]),
                             ("", "TESLA INC", "TESLA INC", ""))
            self.assertEqual((tesla["price"], tesla["price_date"], tesla["day_change"], tesla["ytd_change"]),
                             (None, "", None, None))
            microsoft = rows["594918104"]
            self.assertEqual((microsoft["ticker"], microsoft["name"], microsoft["sector"]),
                             ("MSFT", "MICROSOFT CORP", ""))
            self.assertEqual((microsoft["price"], microsoft["price_date"]), (420.0, "2026-01-15"))
            self.assertAlmostEqual(microsoft["day_change"], 100 * (420 / 410 - 1), places=4)
            self.assertAlmostEqual(microsoft["ytd_change"], 5.0, places=4)
            self.assertEqual((rows["037833100"]["name"], rows["037833100"]["sector"]),
                             ("Apple Inc.", "Technology"))
            # Sorting: the blank ticker, the missing sector, and the '' price bars all land last.
            order = lambda query: [row["cusip"] for row in
                                   self.dashboard(database, {**query, "unmapped": "include"}, cache)["rows"]]
            tesla, microsoft, apple, nvidia = "88160R101", "594918104", "037833100", "67066G104"
            self.assertEqual(order({"view": "holdings"}), [nvidia, tesla, apple, microsoft])
            self.assertEqual(order({"view": "holdings", "sort": "ticker", "direction": "asc"}),
                             [apple, microsoft, nvidia, tesla])
            self.assertEqual(order({"view": "holdings", "sort": "ticker", "direction": "desc"}),
                             [nvidia, microsoft, apple, tesla])
            self.assertEqual(order({"view": "holdings", "sort": "name", "direction": "asc"}),
                             [apple, microsoft, nvidia, tesla])  # Apple, MICROSOFT CORP, NVIDIA, TESLA INC
            for direction in ("asc", "desc"):
                self.assertEqual(order({"view": "holdings", "sort": "sector", "direction": direction}),
                                 [nvidia, apple, tesla, microsoft])
                self.assertEqual(order({"view": "holdings", "sort": "price", "direction": direction}),
                                 [microsoft, nvidia, tesla, apple])
                self.assertEqual(order({"view": "holdings", "sort": "day", "direction": direction}),
                                 [microsoft, nvidia, tesla, apple])

    def test_securities_without_a_ticker_are_hidden_by_default(self) -> None:
        # MSFT is a holding, the only fresh initiation, and a 1Q gainer; blanking its ticker
        # drops it from every view unless unmapped=include restores today's behaviour.
        with tempfile.TemporaryDirectory(prefix="13f-unmapped-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(database)) as con:
                con.execute("UPDATE securities SET ticker='' WHERE cusip='594918104'")
                con.commit()
            holdings = self.dashboard(database, {"view": "holdings"})
            self.assertEqual(set(holdings), DASHBOARD_KEYS)
            self.assertEqual((holdings["unmapped"], holdings["count"], [row["ticker"] for row in holdings["rows"]]),
                             ("exclude", 3, ["NVDA", "TSLA", "AAPL"]))
            self.assertTrue(all(row["ticker"] for row in holdings["rows"]))
            included = self.dashboard(database, {"view": "holdings", "unmapped": "include"})
            self.assertEqual((included["unmapped"], included["count"], [row["ticker"] for row in included["rows"]]),
                             ("include", 4, ["NVDA", "TSLA", "AAPL", ""]))
            self.assertEqual((included["rows"][3]["cusip"], included["rows"][3]["name"]),
                             ("594918104", "MICROSOFT CORP"))
            explicit = self.dashboard(database, {"view": "holdings", "unmapped": "exclude"})
            self.assertEqual((explicit["unmapped"], explicit["rows"]), ("exclude", holdings["rows"]))
            initiations = self.dashboard(database, {"view": "initiations"})
            self.assertEqual((initiations["unmapped"], initiations["count"], initiations["rows"]), ("exclude", 0, []))
            initiations = self.dashboard(database, {"view": "initiations", "unmapped": "include"})
            self.assertEqual((initiations["count"], [row["ticker"] for row in initiations["rows"]]), (1, [""]))
            gainers = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "gainers"})
            self.assertEqual((gainers["unmapped"], gainers["count"], [row["ticker"] for row in gainers["rows"]]),
                             ("exclude", 1, ["NVDA"]))
            gainers = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "gainers",
                                                "unmapped": "include"})
            self.assertEqual((gainers["count"], [row["ticker"] for row in gainers["rows"]]), (2, ["NVDA", ""]))
            losers = self.dashboard(database, {"view": "movers", "horizon": 2, "side": "losers"})
            self.assertEqual((losers["count"], [row["ticker"] for row in losers["rows"]]), (1, ["AAPL"]))
            # Sorting and paging apply to the filtered set: the blank row is not a hidden page-filler.
            paged = self.dashboard(database, {"view": "holdings", "sort": "ticker", "direction": "desc",
                                              "page": 1, "size": 10})
            self.assertEqual((paged["count"], [row["ticker"] for row in paged["rows"]]), (3, ["TSLA", "NVDA", "AAPL"]))
            paged = self.dashboard(database, {"view": "holdings", "sort": "ticker", "direction": "asc",
                                              "page": 2, "size": 10, "unmapped": "include"})
            self.assertEqual((paged["count"], paged["rows"]), (4, []))

    def test_movers_hide_an_exited_security_without_a_ticker_by_default(self) -> None:
        # A blank-ticker security present only in P-k has no securities row in P; it must still be
        # evaluated after the union and dropped only because it is unmapped.
        with tempfile.TemporaryDirectory(prefix="13f-unmapped-exit-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with closing(sqlite3.connect(database)) as con:
                con.execute("INSERT INTO securities VALUES (5,'12345X104','VERSANT MEDIA GROUP INC','COM','','')")
                con.executemany("INSERT INTO security_weight_stats VALUES (?,5,1,?,?,0)",
                                ((1, 0.05, 5.0), (2, 0.125, 12.5)))
                con.commit()
            losers = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "losers"})
            self.assertEqual((losers["unmapped"], losers["count"], [row["ticker"] for row in losers["rows"]]),
                             ("exclude", 2, ["AAPL", "TSLA"]))
            included = self.dashboard(database, {"view": "movers", "horizon": 1, "side": "losers",
                                                 "unmapped": "include"})
            self.assertEqual((included["unmapped"], included["count"]), ("include", 3))
            self.assertEqual([(row["ticker"], row["holders"]) for row in included["rows"]],
                             [("AAPL", 1), ("", 0), ("TSLA", 1)])
            self.assertEqual((included["rows"][1]["metric"], included["rows"][1]["issuer"]),
                             (-12.5, "VERSANT MEDIA GROUP INC"))
            two_quarters = self.dashboard(database, {"view": "movers", "horizon": 2, "side": "losers"})
            self.assertEqual([row["ticker"] for row in two_quarters["rows"]], ["AAPL", "MSFT"])

    def tickers(self, database: Path, query: dict, cache: Path | None = None) -> list[str]:
        payload = self.dashboard(database, query, cache)
        self.assertEqual(set(payload), DASHBOARD_KEYS)
        self.assertEqual((payload["count"], len(payload["rows"])), (4, 4))
        self.assertEqual((payload["sort"], payload["direction"]), (query["sort"], query["direction"]))
        return [row["ticker"] for row in payload["rows"]]

    def test_price_sorts_put_unpriced_rows_last_in_both_directions(self) -> None:
        # Only AAPL is priced on the mark date (TSLA is stale, NVDA and MSFT absent): every price
        # sort leads with AAPL and leaves the rest in the view's default order either way.
        with tempfile.TemporaryDirectory(prefix="13f-price-sort-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            cache = _price_cache(Path(directory) / "prices.sqlite", AAPL_ONLY_BARS)
            for sort in ("price", "day", "ytd"):
                for direction in ("asc", "desc"):
                    with self.subTest(sort=sort, direction=direction):
                        query = {"view": "holdings", "sort": sort, "direction": direction}
                        self.assertEqual(self.tickers(database, query, cache), ["AAPL", "NVDA", "TSLA", "MSFT"])
            payload = self.dashboard(database, {"view": "holdings", "sort": "price", "direction": "asc"}, cache)
            self.assertTrue(payload["price_available"])
            self.assertEqual(payload["rows"][0]["price"], 265.0)
            self.assertEqual([row["price"] for row in payload["rows"][1:]], [None, None, None])
            self.assertTrue(all(set(row) == DASHBOARD_ROW_KEYS for row in payload["rows"]))
            losers = self.dashboard(database, {"view": "movers", "side": "losers", "sort": "price",
                                               "direction": "desc"}, cache)
            self.assertEqual(([row["ticker"] for row in losers["rows"]], losers["count"]), (["AAPL", "TSLA"], 2))

    def test_price_sorts_order_priced_rows_by_close_day_and_year_to_date_change(self) -> None:
        # AAPL 265 (+1.92% day, +6.0% ytd), MSFT 420 (-2.33% day, +5.0% ytd), NVDA 180 with no
        # earlier bar (NULL changes even though priced), TSLA stale (NULL everything).
        with tempfile.TemporaryDirectory(prefix="13f-price-order-") as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            cache = _price_cache(Path(directory) / "prices.sqlite", TWO_PRICED_BARS)
            expected = {
                ("price", "desc"): ["MSFT", "AAPL", "NVDA", "TSLA"], ("price", "asc"): ["NVDA", "AAPL", "MSFT", "TSLA"],
                ("day", "desc"): ["AAPL", "MSFT", "NVDA", "TSLA"], ("day", "asc"): ["MSFT", "AAPL", "NVDA", "TSLA"],
                ("ytd", "desc"): ["AAPL", "MSFT", "NVDA", "TSLA"], ("ytd", "asc"): ["MSFT", "AAPL", "NVDA", "TSLA"],
            }
            for (sort, direction), tickers in expected.items():
                with self.subTest(sort=sort, direction=direction):
                    query = {"view": "holdings", "sort": sort, "direction": direction}
                    self.assertEqual(self.tickers(database, query, cache), tickers)
            # The response rows keep their price_fields() values; only the order changed.
            payload = self.dashboard(database, {"view": "holdings", "sort": "day", "direction": "asc"}, cache)
            microsoft, apple = payload["rows"][0], payload["rows"][1]
            self.assertEqual((microsoft["price"], apple["price"]), (420.0, 265.0))
            self.assertAlmostEqual(microsoft["day_change"], 100 * (420 / 430 - 1), places=4)
            self.assertAlmostEqual(apple["ytd_change"], 6.0, places=4)
            self.assertEqual([row["day_change"] for row in payload["rows"][2:]], [None, None])
            initiations = self.dashboard(database, {"view": "initiations", "sort": "price", "direction": "desc"}, cache)
            self.assertEqual(([row["ticker"] for row in initiations["rows"]], initiations["count"]), (["MSFT"], 1))
            # An unusable cache turns every price key NULL: the default order, still echoing the sort.
            broken = Path(directory) / "broken.sqlite"
            broken.write_bytes(b"not a database")
            payload = self.dashboard(database, {"view": "holdings", "sort": "ytd", "direction": "asc"}, broken)
            self.assertEqual(([row["ticker"] for row in payload["rows"]], payload["price_available"],
                              payload["sort"], payload["direction"]), (DEFAULT_ORDER, False, "ytd", "asc"))

    def test_pages_apply_after_sorting(self) -> None:
        extras = tuple(f"B{index:02d}" for index in range(1, 13))
        with tempfile.TemporaryDirectory(prefix="13f-sorted-pages-") as directory:
            database = _fixture_with_tickers(Path(directory), extras)
            first = self.dashboard(database, {"view": "holdings", "sort": "ticker", "direction": "asc",
                                              "page": 1, "size": 10})
            second = self.dashboard(database, {"view": "holdings", "sort": "ticker", "direction": "asc",
                                               "page": 2, "size": 10})
            self.assertEqual((first["count"], second["count"], first["page"], second["page"]), (16, 16, 1, 2))
            self.assertEqual([row["ticker"] for row in first["rows"]], ["AAPL", *extras[:9]])
            self.assertEqual([row["ticker"] for row in second["rows"]], [*extras[9:], "MSFT", "NVDA", "TSLA"])
            last = self.dashboard(database, {"view": "holdings", "sort": "ticker", "direction": "desc",
                                             "page": 2, "size": 10})
            self.assertEqual([row["ticker"] for row in last["rows"]], [*reversed(extras[:5]), "AAPL"])
            # The default order is untouched by the extra rows' sortable columns.
            by_metric = self.dashboard(database, {"view": "holdings", "page": 1, "size": 10})
            self.assertEqual((by_metric["sort"], by_metric["direction"], by_metric["count"]), ("metric", "desc", 16))
            self.assertEqual([row["ticker"] for row in by_metric["rows"][:4]], DEFAULT_ORDER)
            # Initiations: the extras are all new holders; ties on the metric keep the default order.
            initiations = self.dashboard(database, {"view": "initiations", "sort": "ticker", "direction": "desc",
                                                    "page": 2, "size": 10})
            self.assertEqual((initiations["count"], [row["ticker"] for row in initiations["rows"]]),
                             (13, ["B03", "B02", "B01"]))


class BasePathTests(unittest.TestCase):
    """The same handler behind a path prefix (BASE_PATH="/13f"), with unprefixed paths still accepted."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="13f-base-path-")
        cls.database = create_fixture_database(Path(cls._temporary.name) / "fixture.sqlite")
        cls._patches = [
            mock.patch.object(server, "PRICE_CACHE", Path(cls._temporary.name) / "missing-prices.sqlite"),
            mock.patch.object(server, "BASE_PATH", "/13f"),
        ]
        for patch in cls._patches:
            patch.start()
        cls._server_context = running_server(cls.database)
        cls.address = cls._server_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)
        for patch in reversed(cls._patches):
            patch.stop()
        cls._temporary.cleanup()

    def request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        return http_request(self.address, path, method)

    def test_base_path_is_restored_for_other_tests(self) -> None:
        self.assertEqual(server.BASE_PATH, "/13f")
        self.assertIsInstance(self._patches[1], mock._patch)

    def test_prefixed_and_unprefixed_paths_are_served(self) -> None:
        html = "text/html; charset=utf-8"
        # path -> (body prefix, content type, served document or None)
        expected = {
            "/13f": (DOCTYPE, html, "dashboard.html"),
            "/13f/": (DOCTYPE, html, "dashboard.html"),
            "/13f/?sort=ticker&direction=asc": (DOCTYPE, html, "dashboard.html"),
            "/13f/initiations": (DOCTYPE, html, "dashboard.html"),
            "/13f/movers": (DOCTYPE, html, "dashboard.html"),
            "/13f/movers?horizon=2&side=losers": (DOCTYPE, html, "dashboard.html"),
            "/13f/about": (DOCTYPE, html, "dashboard.html"),
            "/13f/about?ignored=1": (DOCTYPE, html, "dashboard.html"),
            "/13f/dashboard": (DOCTYPE, html, "dashboard.html"),
            "/13f/dashboard/initiations": (DOCTYPE, html, "dashboard.html"),
            "/13f/dashboard/movers": (DOCTYPE, html, "dashboard.html"),
            "/13f/dashboard.html": (DOCTYPE, html, "dashboard.html"),
            "/13f/dashboard.js": (b"'use strict';", "text/javascript", None),
            "/13f/dashboard.css": (b":root", "text/css", None),
            # A proxy that strips the prefix delivers these; they keep working.
            "/": (DOCTYPE, html, "dashboard.html"),
            "/about": (DOCTYPE, html, "dashboard.html"),
            "/movers": (DOCTYPE, html, "dashboard.html"),
            "/dashboard/movers": (DOCTYPE, html, "dashboard.html"),
            "/dashboard.js": (b"'use strict';", "text/javascript", None),
        }
        for path, (prefix, content_type, document) in expected.items():
            with self.subTest(method="GET", path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200, body[:120])
                self.assertIn(prefix, body[:600], body[:80])
                self.assertEqual(int(headers["content-length"]), len(body))
                self.assertTrue(headers.get("content-type", "").startswith(content_type), headers.get("content-type"))
                self.assertEqual(headers.get("cache-control"), "no-cache")
                self.assertEqual(headers.get("x-content-type-options"), "nosniff")
                self.assertEqual(headers.get("x-frame-options"), "DENY")
                self.assertIn("default-src 'self'", headers.get("content-security-policy", ""))
                if document is not None:
                    self.assertIn(DASHBOARD_MARKER, body)
                    for marker in EXPLORER_MARKERS:
                        self.assertNotIn(marker, body)
            with self.subTest(method="HEAD", path=path):
                status, headers, body = self.request(path, "HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertGreater(int(headers["content-length"]), 0)
                self.assertEqual(headers.get("cache-control"), "no-cache")
        for path in ("/13f-other", "/13fx/dashboard.js", "/13f/explorer", "/13f/explorer/", "/13f/index.html",
                     "/13f/app.js", "/13f/styles.css", "/13f/about/", "/13f/about/x", "/13f/About",
                     "/13f/initiations/", "/13f/movers/", "/13f/dashboard/", "/13f/dashboard/about",
                     "/13f/server.py", "/13f/data/13f.sqlite", "/13f/13f/dashboard.js", "/13F/dashboard.js",
                     "/13f//dashboard.js", "/13f/dashboard/movers/", "/13f/dashboard.html/", "/13f/movers/x",
                     "/explorer", "/app.js", "/about/"):
            for method in ("GET", "HEAD"):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    self.assertEqual(headers.get("x-content-type-options"), "nosniff")
                    if method == "HEAD":
                        self.assertEqual(body, b"")

    def test_documents_carry_the_prefix(self) -> None:
        for path in ("/13f", "/13f/", "/13f/initiations", "/13f/movers", "/13f/about", "/13f/dashboard",
                     "/13f/dashboard/movers", "/13f/dashboard.html", "/", "/about", "/dashboard"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 200)
                text = body.decode("utf-8")
                self.assertIn('src="/13f/dashboard.js"', text)
                self.assertIn('href="/13f/dashboard.css"', text)
                self.assertIn('id="dashLogo" class="dash-logo" href="/13f/"', text)
                self.assertIn('href="/13f/initiations"', text)
                self.assertIn('href="/13f/movers"', text)
                self.assertIn('href="/13f/about" data-view="about"', text)
                self.assertIn('href="/13f/movers?side=losers"', text)
                self.assertIn('href="/13f/?sort=ticker&amp;direction=asc"', text)
                self.assertIn(DASHBOARD_MARKER, body)
                for marker in ABOUT_MARKERS:
                    self.assertIn(marker, body)
                self.assertNotIn("//13f", text)
                self.assertNotIn('href="/about"', text)
                self.assertNotIn("/13f/dashboard", text.replace("/13f/dashboard.js", "").replace("/13f/dashboard.css", ""))
                self.assertIn('href="data:image/svg+xml,', text)
        # Scripts and styles are served verbatim (the prefix is derived client-side).
        _, _, script = self.request("/13f/dashboard.js")
        self.assertEqual(script, (server.ROOT / "dashboard.js").read_bytes())
        _, _, stylesheet = self.request("/13f/dashboard.css")
        self.assertEqual(stylesheet, (server.ROOT / "dashboard.css").read_bytes())

    def test_api_under_the_prefix(self) -> None:
        for path in ("/13f/api/meta", "/api/meta", "/13f/api/dashboard?view=movers",
                     "/13f/api/dashboard?view=holdings&unmapped=include"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200, body[:120])
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(headers.get("cache-control"), "no-store")
                payload = json.loads(body)
                self.assertIsInstance(payload, dict)
        movers = json.loads(self.request("/13f/api/dashboard?view=movers")[2])
        self.assertEqual((movers["view"], movers["unmapped"], [row["ticker"] for row in movers["rows"]]),
                         ("movers", "exclude", ["NVDA", "MSFT"]))
        meta = json.loads(self.request("/13f/api/meta")[2])
        self.assertEqual((meta["period_count"], meta["latest_period"]), (3, "31-DEC-2025"))
        for path in ("/13f/api/nope", "/13f/api/holdings?period=31-DEC-2025", "/13f/api/funds"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual((status, json.loads(body)), (404, {"error": "Not found"}))
        status, _, body = self.request("/13f/api/meta?x=1")
        self.assertEqual(status, 400)
        status, _, body = self.request("/13f/api/meta", "HEAD")
        self.assertEqual((status, body), (405, b""))
        # Look-alike prefixes never reach the API dispatcher.
        status, _, _ = self.request("/13fx/api/meta")
        self.assertEqual(status, 404)
        status, _, _ = self.request("/13f-other/api/meta")
        self.assertEqual(status, 404)


class BasePathRestoredTests(unittest.TestCase):
    def test_module_default_is_the_root(self) -> None:
        # BasePathTests patches server.BASE_PATH; outside that class it is the empty root prefix.
        self.assertEqual(server.BASE_PATH, "")


if __name__ == "__main__":
    unittest.main()
