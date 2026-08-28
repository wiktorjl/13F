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
        for params in ({"page": ["0"]}, {"page": [str(server.MAX_PAGE + 1)]}, {"size": ["9"]}, {"size": ["201"]}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                server.page_params(params)

    def test_unknown_parameters_are_rejected_with_their_names(self) -> None:
        server.reject_unknown_params({"view": ["holdings"]}, server.API_PARAMETERS["/api/dashboard"])
        server.reject_unknown_params({}, server.API_PARAMETERS["/api/meta"])
        with self.assertRaisesRegex(ValueError, "Unknown query parameter: period"):
            server.reject_unknown_params({"period": ["x"]}, server.API_PARAMETERS["/api/meta"])
        with self.assertRaisesRegex(ValueError, "Unknown query parameters: a, b"):
            server.reject_unknown_params({"b": ["1"], "a": ["1"], "view": ["holdings"]},
                                         server.API_PARAMETERS["/api/dashboard"])
        # Only the two dashboard-era endpoints exist; the explorer's are gone for good.
        self.assertEqual(set(server.API_PARAMETERS), {"/api/meta", "/api/dashboard"})
        self.assertEqual(server.API_PARAMETERS["/api/meta"], frozenset())


def _price_cache(path: Path, bars: tuple[tuple[str, str, float], ...],
                 metadata: dict[str, str]) -> Path:
    with closing(sqlite3.connect(path)) as con:
        con.execute("""CREATE TABLE bars (symbol TEXT NOT NULL COLLATE NOCASE,price_date TEXT NOT NULL,
          close REAL NOT NULL CHECK(close>0),PRIMARY KEY(symbol,price_date)) WITHOUT ROWID""")
        con.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
        con.executemany("INSERT INTO bars VALUES (?,?,?)", bars)
        con.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        con.commit()
    return path


FIXTURE_BARS = (
    ("AAPL", "2025-12-30", 248.0), ("AAPL", "2025-12-31", 250.0),
    ("AAPL", "2026-01-14", 260.0), ("AAPL", "2026-01-15", 265.0),
    ("TSLA", "2026-01-14", 400.0),
    ("XYZ", "2025-06-30", 100.0), ("XYZ", "2026-01-15", 120.0),  # base before December: ytd null, day from June
    ("NEW", "2026-01-15", 50.0),  # mark-date close only: day and ytd null
    ("", "2026-01-15", 1.0),  # a blank symbol is never looked up
)


class DashboardHelperTests(unittest.TestCase):
    def test_static_paths_are_exact_and_dashboard_routes_share_one_page(self) -> None:
        # The dashboard is the whole site: four routes plus the legacy aliases, one document.
        self.assertEqual(server.static_path_for("/"), "/dashboard.html")
        self.assertEqual(server.static_path_for("/about"), "/dashboard.html")
        self.assertEqual(server.DASHBOARD_ROUTES, {"/", "/initiations", "/movers", "/about"})
        self.assertEqual(server.DASHBOARD_ALIASES, {"/dashboard", "/dashboard/initiations", "/dashboard/movers"})
        self.assertEqual(server.STATIC_PATHS, {"/dashboard.html", "/dashboard.js", "/dashboard.css"})
        self.assertEqual(server.HTML_DOCUMENTS, {"dashboard.html"})
        for route in server.DASHBOARD_ROUTES | server.DASHBOARD_ALIASES:
            with self.subTest(route=route):
                self.assertEqual(server.static_path_for(route), "/dashboard.html")
        for member in server.STATIC_PATHS:
            self.assertEqual(server.static_path_for(member), member)
        # The explorer and its assets are gone; trailing slashes and case variants were never routes.
        for path in ("/explorer", "/explorer/", "/explorer/x", "/Explorer", "/index.html", "/app.js", "/styles.css",
                     "/about/", "/about/x", "/About", "/ABOUT", "/dashboard/about", "/about.html",
                     "/initiations/", "/initiations/x", "/movers/", "/movers/x", "/Movers", "/dashboard/",
                     "/dashboard/x", "/DASHBOARD", "/Dashboard", "/dashboard/movers/", "/dashboard.html/",
                     "/data/13f.sqlite", "/server.py", "//", "", "about", "/dashboard.js/"):
            with self.subTest(path=path):
                self.assertIsNone(server.static_path_for(path))

    def test_dashboard_params_defaults_and_rejections(self) -> None:
        self.assertEqual(server.dashboard_params({}), {
            "view": "holdings", "horizon": 1, "side": "gainers", "sort": "metric", "direction": "desc",
            "unmapped": "exclude", "page": 1, "size": 100,
        })
        self.assertEqual(server.dashboard_params({
            "view": ["movers"], "horizon": ["4"], "side": ["losers"], "page": ["3"], "size": ["10"],
        }), {"view": "movers", "horizon": 4, "side": "losers", "sort": "metric", "direction": "asc",
             "unmapped": "exclude", "page": 3, "size": 10})
        self.assertEqual(server.dashboard_params({
            "view": ["initiations"], "sort": ["ytd"], "direction": ["asc"], "page": ["2"], "size": ["50"],
            "unmapped": ["include"],
        }), {"view": "initiations", "horizon": 1, "side": "gainers", "sort": "ytd", "direction": "asc",
             "unmapped": "include", "page": 2, "size": 50})
        # Securities without a ticker are hidden unless explicitly included.
        self.assertEqual(server.dashboard_params({"unmapped": ["exclude"]})["unmapped"], "exclude")
        self.assertEqual(server.dashboard_params({"unmapped": ["include"]})["unmapped"], "include")
        # horizon/side are validated even when the view ignores them.
        self.assertEqual(server.dashboard_params({"view": ["holdings"], "horizon": ["2"]})["horizon"], 2)
        # Only movers losers default to ascending (most negative first); an explicit direction wins.
        for params, direction in (
            ({"view": ["movers"], "side": ["losers"]}, "asc"),
            ({"view": ["movers"], "side": ["losers"], "sort": ["ticker"]}, "asc"),
            ({"view": ["movers"], "side": ["losers"], "direction": ["desc"]}, "desc"),
            ({"view": ["movers"], "side": ["gainers"]}, "desc"),
            ({"view": ["holdings"], "side": ["losers"]}, "desc"),
            ({"view": ["initiations"], "side": ["losers"], "direction": ["asc"]}, "asc"),
        ):
            with self.subTest(params=params):
                self.assertEqual(server.dashboard_params(params)["direction"], direction)
        for sort in sorted(server.DASHBOARD_SORTS):
            self.assertEqual(server.dashboard_params({"sort": [sort]})["sort"], sort)
        for params in ({"view": ["funds"]}, {"view": ["about"]}, {"side": ["up"]}, {"horizon": ["0"]},
                       {"horizon": ["5"]}, {"horizon": ["1.5"]}, {"horizon": ["abc"]}, {"page": ["0"]},
                       {"size": ["9"]}, {"size": ["201"]}, {"view": ["hold\nings"]}, {"sort": ["bogus"]},
                       {"sort": ["Metric"]}, {"sort": [""]}, {"sort": ["price desc"]}, {"direction": ["up"]},
                       {"direction": ["ASC"]}, {"direction": [""]}, {"direction": ["asc;"]}, {"unmapped": ["all"]},
                       {"unmapped": ["Include"]}, {"unmapped": [""]}, {"unmapped": ["yes"]},
                       {"view": ["x" * (server.MAX_PARAMETER_LENGTH + 1)]}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                server.dashboard_params(params)

    def test_dashboard_order_is_composed_only_from_fixed_fragments(self) -> None:
        def order(priced=False, **params):
            return server.dashboard_order(server.dashboard_params(
                {key: [str(value)] for key, value in params.items()}), priced=priced)

        # The default of every view is byte-for-byte its historical order.
        self.assertEqual(order(view="holdings"), "metric DESC,holders DESC,cusip ASC")
        self.assertEqual(order(view="initiations"), "metric DESC,avg_weight DESC,cusip ASC")
        self.assertEqual(order(view="movers", side="gainers"), "metric DESC,cusip ASC")
        self.assertEqual(order(view="movers", side="losers"), "metric ASC,cusip ASC")
        self.assertEqual(order(view="holdings", sort="metric", direction="asc"), "metric ASC,holders DESC,cusip ASC")
        self.assertEqual(order(view="movers", side="losers", direction="desc"), "metric DESC,cusip ASC")
        # Other keys lead, NULLs last either way, then the view's default order breaks ties.
        self.assertEqual(order(view="holdings", sort="ticker", direction="asc"),
                         "nullif(ticker,'') COLLATE NOCASE ASC NULLS LAST,metric DESC,holders DESC,cusip ASC")
        self.assertEqual(order(view="movers", side="losers", sort="name", direction="desc"),
                         "nullif(name,'') COLLATE NOCASE DESC NULLS LAST,metric ASC,cusip ASC")
        self.assertEqual(order(view="initiations", sort="sector", direction="asc"),
                         "nullif(sector,'') COLLATE NOCASE ASC NULLS LAST,metric DESC,avg_weight DESC,cusip ASC")
        for sort in ("price", "day", "ytd"):
            with self.subTest(sort=sort):
                self.assertEqual(order(view="holdings", sort=sort, direction="desc", priced=True),
                                 f"sort_{sort} DESC NULLS LAST,metric DESC,holders DESC,cusip ASC")
                self.assertEqual(order(view="movers", side="losers", sort=sort, direction="asc", priced=True),
                                 f"sort_{sort} ASC NULLS LAST,metric ASC,cusip ASC")
                # Without a price cache every price key is NULL, so the default order stands.
                self.assertEqual(order(view="holdings", sort=sort, direction="asc"), order(view="holdings"))
        for sort in server.DASHBOARD_SORTS:
            for direction in server.SORT_DIRECTIONS:
                fragment = order(view="holdings", sort=sort, direction=direction, priced=True)
                self.assertRegex(fragment, r"^[A-Za-z_(),' ]+$")
        self.assertEqual(set(server.DASHBOARD_SORT_SQL), server.DASHBOARD_SORTS - {"metric"})
        self.assertEqual(server.DASHBOARD_PRICE_SORTS, {"price", "day", "ytd"})

    def test_percent_change_requires_both_closes_and_a_positive_base(self) -> None:
        self.assertEqual(server.percent_change(110.0, 100.0), 10.0)
        self.assertAlmostEqual(server.percent_change(265.0, 260.0), 1.9231, places=4)
        for current, base in ((None, 100.0), (100.0, None), (100.0, 0.0), (100.0, -1.0)):
            self.assertIsNone(server.percent_change(current, base))

    def _read_only_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect("file::memory:", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    def test_db_connections_are_read_only_and_carry_no_sidecar(self) -> None:
        # The fund-signals sidecar is no longer attached: only the main schema (and, for the
        # dashboard handler, the price cache) is visible to a request.
        with tempfile.TemporaryDirectory() as directory:
            database = create_fixture_database(Path(directory) / "fixture.sqlite")
            with mock.patch.object(server, "DB", database), closing(server.db()) as con:
                self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual([row["name"] for row in con.execute("PRAGMA database_list")], ["main"])
                self.assertEqual(con.execute("SELECT count(*) FROM periods").fetchone()[0], 3)
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("DELETE FROM periods")

    def test_attach_price_cache_falls_back_to_the_empty_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            garbage = root / "garbage.sqlite"
            garbage.write_bytes(b"not a database")
            no_close = root / "no-close.sqlite"
            with closing(sqlite3.connect(no_close)) as con:
                con.execute("CREATE TABLE bars(symbol TEXT,price_date TEXT)")
                con.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
                con.commit()
            no_metadata = root / "no-metadata.sqlite"
            with closing(sqlite3.connect(no_metadata)) as con:
                con.execute("CREATE TABLE bars(symbol TEXT,price_date TEXT,close REAL)")
                con.commit()
            for path in (root / "missing.sqlite", garbage, no_close, no_metadata):
                with self.subTest(path=path.name), mock.patch.object(server, "PRICE_CACHE", path):
                    self.assertFalse(server.price_cache_is_usable())
                    with closing(self._read_only_connection()) as con:
                        self.assertFalse(server.attach_price_cache(con))
                        con.execute("BEGIN")
                        self.assertEqual(server.price_mark(con), ("", ""))
                        self.assertEqual(con.execute("SELECT count(*) FROM prices.bars").fetchone()[0], 0)
                        self.assertEqual(server.price_fields(con, "AAPL", ""), {
                            "price": None, "price_date": "", "day_change": None, "ytd_change": None,
                        })
                        self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_attach_price_cache_reads_mark_day_and_year_to_date_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = _price_cache(Path(directory) / "prices.sqlite", FIXTURE_BARS,
                                 {"mark_date": "2026-01-15", "source": "Fixture prices"})
            with mock.patch.object(server, "PRICE_CACHE", cache):
                self.assertTrue(server.price_cache_is_usable())
                with closing(self._read_only_connection()) as con:
                    self.assertTrue(server.attach_price_cache(con))
                    con.execute("BEGIN")
                    self.assertEqual(server.price_mark(con), ("2026-01-15", "Fixture prices"))
                    apple = server.price_fields(con, "AAPL", "2026-01-15")
                    self.assertEqual((apple["price"], apple["price_date"]), (265.0, "2026-01-15"))
                    self.assertAlmostEqual(apple["day_change"], 100 * (265 / 260 - 1), places=4)
                    self.assertAlmostEqual(apple["ytd_change"], 6.0, places=4)
                    # The year-to-date base must fall inside December of the prior year; an
                    # older close still serves as the previous close for the day change.
                    xyz = server.price_fields(con, "XYZ", "2026-01-15")
                    self.assertEqual((xyz["price"], xyz["price_date"], xyz["ytd_change"]),
                                     (120.0, "2026-01-15", None))
                    self.assertAlmostEqual(xyz["day_change"], 20.0, places=4)
                    # A mark-date close with no earlier bar is priced but has no changes.
                    self.assertEqual(server.price_fields(con, "NEW", "2026-01-15"), {
                        "price": 50.0, "price_date": "2026-01-15", "day_change": None, "ytd_change": None,
                    })
                    # Stale symbols (no close on the mark date), unknown symbols, and blank
                    # tickers (never looked up, even with a matching bar) are unpriced.
                    for ticker in ("TSLA", "MSFT", ""):
                        with self.subTest(ticker=ticker):
                            self.assertEqual(server.price_fields(con, ticker, "2026-01-15"), {
                                "price": None, "price_date": "", "day_change": None, "ytd_change": None,
                            })
                    self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_price_mark_falls_back_to_the_latest_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = _price_cache(Path(directory) / "prices.sqlite", FIXTURE_BARS,
                                 {"mark_date": "not-a-date", "source": "Fixture prices"})
            with mock.patch.object(server, "PRICE_CACHE", cache):
                with closing(self._read_only_connection()) as con:
                    self.assertTrue(server.attach_price_cache(con))
                    self.assertEqual(server.price_mark(con), ("2026-01-15", "Fixture prices"))
            empty = _price_cache(Path(directory) / "empty.sqlite", (), {})
            with mock.patch.object(server, "PRICE_CACHE", empty):
                with closing(self._read_only_connection()) as con:
                    self.assertTrue(server.attach_price_cache(con))
                    self.assertEqual(server.price_mark(con), ("", ""))


class FreshnessTests(unittest.TestCase):
    def test_database_freshness_checks_every_input_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "snapshot.sqlite"
            archive = root / "fixture_form13f.zip"
            ticker_map = root / "tickers.json"
            market_caps = root / "caps.json"
            watchlist = root / "funds.json"
            sectors = root / "sectors.json"
            archive.write_bytes(b"archive")
            ticker_map.write_bytes(b"tickers")
            market_caps.write_bytes(b"caps")
            watchlist.write_bytes(b"funds")
            sectors.write_bytes(b"sectors")

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            metadata = {
                "schema_version": server.SCHEMA_VERSION,
                "source_archives": json.dumps([archive.name]),
                "source_archive_hashes": json.dumps([{"name": archive.name, "sha256": digest(archive)}]),
                "ticker_map_sha256": digest(ticker_map),
                "market_cap_sha256": digest(market_caps),
                "fund_watchlist_sha256": digest(watchlist),
                "sector_sha256": digest(sectors),
            }
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                con.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                con.commit()

            with (
                mock.patch.object(server, "ROOT", root),
                mock.patch.object(server, "ARCHIVE_DIR", root),
                mock.patch.object(server, "DB", database),
                mock.patch.object(server, "TICKER_MAP", ticker_map),
                mock.patch.object(server, "MARKET_CAPS", market_caps),
                mock.patch.object(server, "STARRED_FUNDS", watchlist),
                mock.patch.object(server, "SECTORS", sectors),
            ):
                self.assertTrue(server.database_is_current())
                market_caps.write_bytes(b"changed")
                self.assertFalse(server.database_is_current())
                market_caps.write_bytes(b"caps")
                self.assertTrue(server.database_is_current())
                sectors.write_bytes(b"changed")
                self.assertFalse(server.database_is_current())
                sectors.unlink()
                self.assertFalse(server.database_is_current())

    def test_archives_are_hashed_from_archive_dir_not_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives = root / "archives"
            archives.mkdir()
            database = root / "snapshot.sqlite"
            archive = archives / "fixture_form13f.zip"
            archive.write_bytes(b"archive")
            digest = hashlib.sha256(b"archive").hexdigest()
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                con.executemany("INSERT INTO metadata VALUES (?,?)", {
                    "schema_version": server.SCHEMA_VERSION,
                    "source_archives": json.dumps([archive.name]),
                    "source_archive_hashes": json.dumps([{"name": archive.name, "sha256": digest}]),
                    "ticker_map_sha256": "", "market_cap_sha256": "", "fund_watchlist_sha256": "",
                    "sector_sha256": "",
                }.items())
                con.commit()
            missing = root / "missing.json"
            with (
                mock.patch.object(server, "ROOT", root),
                mock.patch.object(server, "DB", database),
                mock.patch.object(server, "TICKER_MAP", missing),
                mock.patch.object(server, "MARKET_CAPS", missing),
                mock.patch.object(server, "STARRED_FUNDS", missing),
                mock.patch.object(server, "SECTORS", missing),
            ):
                with mock.patch.object(server, "ARCHIVE_DIR", archives):
                    self.assertTrue(server.database_is_current())
                with mock.patch.object(server, "ARCHIVE_DIR", root):  # no archives here: stale
                    self.assertFalse(server.database_is_current())
                    self.assertTrue(server.database_is_current(trust=True))

    def test_trust_mode_checks_only_existence_and_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = create_fixture_database(root / "fixture.sqlite")
            # An unrelated archive next to the fixture makes the strict check stale;
            # trust mode never hashes it.
            (root / "fixture_form13f.zip").write_bytes(b"archive")
            with (
                mock.patch.object(server, "ROOT", root),
                mock.patch.object(server, "ARCHIVE_DIR", root),
                mock.patch.object(server, "DB", database),
            ):
                self.assertFalse(server.database_is_current())
                self.assertTrue(server.database_is_current(trust=True))
                with closing(sqlite3.connect(database)) as con:
                    con.execute("UPDATE metadata SET value='0' WHERE key='schema_version'")
                    con.commit()
                self.assertFalse(server.database_is_current(trust=True))
                with closing(sqlite3.connect(database)) as con:
                    con.execute("UPDATE metadata SET value=? WHERE key='schema_version'",
                                (server.SCHEMA_VERSION,))
                    con.commit()
                self.assertTrue(server.database_is_current(trust=True))
            with mock.patch.object(server, "DB", root / "absent.sqlite"):
                self.assertFalse(server.database_is_current(trust=True))
            garbage = root / "garbage.sqlite"
            garbage.write_bytes(b"not a database")
            with mock.patch.object(server, "DB", garbage):
                self.assertFalse(server.database_is_current(trust=True))
            self.assertFalse(server.env_flag("THIRTEEN_F_UNSET_FLAG"))
            for raw, expected in (("1", True), ("true", True), ("YES", True), (" yes ", True),
                                  ("0", False), ("", False), ("no", False), ("on", False)):
                with self.subTest(raw=raw), mock.patch.dict("os.environ", {"THIRTEEN_F_FLAG": raw}):
                    self.assertEqual(server.env_flag("THIRTEEN_F_FLAG"), expected)


class BasePathHelperTests(unittest.TestCase):
    def test_normalize_base_path(self) -> None:
        for raw, expected in (("", ""), ("/", ""), ("  ", ""), ("/13f", "/13f"), ("/13f/", "/13f"),
                              (" /13f ", "/13f"), ("/a/b", "/a/b"), ("/a/b/", "/a/b"), ("/v1.2_x~y-z", "/v1.2_x~y-z")):
            with self.subTest(raw=raw):
                self.assertEqual(server.normalize_base_path(raw), expected)
        for raw in ("13f", "//x", "/x y", "/13f//", "/a//b", "/x?y", "/x#y", "/ü", "/x%20y",
                    "http://host/13f", "/x\ny", "/x\\y", "/13f/;"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                server.normalize_base_path(raw)

    def test_request_path_strips_only_the_exact_prefix(self) -> None:
        with mock.patch.object(server, "BASE_PATH", "/13f"):
            for raw, expected in (
                ("/13f", "/"), ("/13f/", "/"), ("/13f/dashboard.html", "/dashboard.html"),
                ("/13f/api/meta", "/api/meta"), ("/13f/about", "/about"), ("/13f/initiations", "/initiations"),
                ("/13f/movers", "/movers"), ("/13f/about/", "/about/"), ("/13f/movers/", "/movers/"),
                ("/13f/dashboard/movers", "/dashboard/movers"), ("/13f/dashboard/", "/dashboard/"),
                ("/13f/dashboard.js", "/dashboard.js"), ("/13f/13f/dashboard.js", "/13f/dashboard.js"),
                # Removed explorer paths are stripped like any other and left to the allowlist (404).
                ("/13f/explorer", "/explorer"), ("/13f/app.js", "/app.js"),
                # Unprefixed paths pass through unchanged (a proxy may strip the prefix itself);
                # look-alikes are left for the allowlist to reject.
                ("/", "/"), ("/api/meta", "/api/meta"), ("/about", "/about"), ("/dashboard", "/dashboard"),
                ("/13f-other", "/13f-other"), ("/13fx/dashboard.js", "/13fx/dashboard.js"),
                ("/13F/dashboard.js", "/13F/dashboard.js"), ("", ""),
            ):
                with self.subTest(path=raw):
                    self.assertEqual(server.request_path(raw), expected)
            # The query string is not part of the path the handler passes in.
            self.assertEqual(server.request_path("/13f/dashboard/movers?x=1"), "/dashboard/movers?x=1")
        with mock.patch.object(server, "BASE_PATH", "/a/b"):
            self.assertEqual(server.request_path("/a/b/dashboard.js"), "/dashboard.js")
            self.assertEqual(server.request_path("/a/dashboard.js"), "/a/dashboard.js")
        with mock.patch.object(server, "BASE_PATH", ""):
            for raw in ("/", "/13f", "/13f/dashboard.js", "/api/meta", "/13f-other"):
                self.assertEqual(server.request_path(raw), raw)

    def test_render_document_rewrites_root_absolute_references_only_under_a_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                '<!doctype html><html><head><link rel="stylesheet" href="/dashboard.css">'
                '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\'/%3E">'
                '<link rel="stylesheet" href="//cdn.example/x.css"><link href="relative.css" rel="stylesheet">'
                '</head><body><a href="/movers">D</a><a href="/?sort=ticker&amp;direction=asc">S</a>'
                '<a href="/about">A</a>'
                '<a href="?view=stocks">Q</a><a href="#top">H</a><a href="/">Root</a>'
                '<img src="/13f/already.png"><a href="/13f">Prefixed</a><a href="/13fx/other">Lookalike</a>'
                '<script src="/dashboard.js"></script></body></html>'
            )
            (root / "dashboard.html").write_text(source, encoding="utf-8")
            (root / "index.html").write_text('<script src="/app.js"></script>', encoding="utf-8")
            with mock.patch.object(server, "ROOT", root):
                with mock.patch.object(server, "BASE_PATH", ""):
                    self.assertEqual(server.render_document("dashboard.html"), source.encode("utf-8"))
                with mock.patch.object(server, "BASE_PATH", "/13f"):
                    rendered = server.render_document("dashboard.html").decode("utf-8")
                    for expected in ('href="/13f/dashboard.css"', 'src="/13f/dashboard.js"', 'href="/13f/movers"',
                                     'href="/13f/?sort=ticker&amp;direction=asc"', 'href="/13f/"',
                                     'href="/13f/about"', 'href="/13f/13fx/other"'):
                        with self.subTest(expected=expected):
                            self.assertIn(expected, rendered)
                    for untouched in ('href="data:image/svg+xml,', 'href="//cdn.example/x.css"',
                                      'href="relative.css"', 'href="?view=stocks"', 'href="#top"',
                                      'src="/13f/already.png"', 'href="/13f">Prefixed'):
                        with self.subTest(untouched=untouched):
                            self.assertIn(untouched, rendered)
                    self.assertNotIn("//13f", rendered)
                    self.assertNotIn("/13f/13f/", rendered)
                with mock.patch.object(server, "BASE_PATH", "/a/b"):
                    self.assertIn('src="/a/b/dashboard.js"', server.render_document("dashboard.html").decode("utf-8"))
                # A stray index.html on disk is not a served document any more.
                for name in ("index.html", "/index.html"):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        server.render_document(name)
        # Only the dashboard document is renderable; the real file gains the prefix too.
        for name in ("server.py", "/dashboard.html", "dashboard.js", "../dashboard.html", "index.html", "app.js", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                server.render_document(name)
        with mock.patch.object(server, "BASE_PATH", "/13f"):
            dashboard = server.render_document("dashboard.html").decode("utf-8")
            self.assertIn('src="/13f/dashboard.js"', dashboard)
            self.assertIn('href="/13f/dashboard.css"', dashboard)
            self.assertIn('id="dashLogo" class="dash-logo" href="/13f/"', dashboard)
            self.assertIn('href="/13f/initiations"', dashboard)
            self.assertIn('href="/13f/movers"', dashboard)
            self.assertIn('href="/13f/?sort=ticker&amp;direction=asc"', dashboard)
            self.assertNotIn("//13f", dashboard)
            self.assertIn('href="data:image/svg+xml,', dashboard)
        with mock.patch.object(server, "BASE_PATH", ""):
            self.assertEqual(server.render_document("dashboard.html"), (server.ROOT / "dashboard.html").read_bytes())


if __name__ == "__main__":
    unittest.main()
