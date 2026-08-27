from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

import server
from tests.support import create_fixture_database, http_request, running_server


class HTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="13f-http-tests-")
        cls.database = create_fixture_database(Path(cls._temporary.name) / "fixture.sqlite")
        cls._server_context = running_server(cls.database)
        cls.address = cls._server_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)
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
        allowed = {
            "/": b"<!doctype html>",
            "/index.html": b"<!doctype html>",
            "/app.js": b"const $",
            "/styles.css": b":root",
            "/app.js?v=fixture": b"const $",
        }
        for path, prefix in allowed.items():
            with self.subTest(method="GET", path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(body.startswith(prefix), body[:80])
                self.assertEqual(int(headers["content-length"]), len(body))
            with self.subTest(method="HEAD", path=path):
                status, headers, body = self.request(path, "HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertGreater(int(headers["content-length"]), 0)

        denied = (
            "/server.py",
            "/build_database.py",
            "/data/13f.sqlite",
            "/tests/support.py",
            "/favicon.ico",
            "/app.js/extra",
            "/APP.JS",
            "/%2e%2e/server.py",
            "/app%2ejs",
        )
        for method in ("GET", "HEAD"):
            for path in denied:
                with self.subTest(method=method, path=path):
                    status, _, body = self.request(path, method)
                    self.assertEqual(status, 404)
                    if method == "HEAD":
                        self.assertEqual(body, b"")

    def test_security_headers_cover_success_and_error_responses(self) -> None:
        paths = (("/", 200), ("/api/meta", 200), ("/api/not-real", 404), ("/server.py", 404))
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
        payload, headers = self.request_json("/api/meta")
        self.assertIsInstance(payload, dict)
        self.assert_keys(payload, {
            "schema_version", "periods", "latest_period", "forms", "states", "holding_count",
            "total_value", "distinct_managers", "distinct_issuers", "signal_available",
            "signal_price_source", "signal_price_date", "research_fund_cutoff",
        })
        self.assertEqual(payload["latest_period"], "31-DEC-2025")
        self.assertEqual(len(payload["periods"]), 3)
        self.assert_keys(payload["periods"][0], {"label", "period_date"})
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_holdings_response_schema_and_literal_search(self) -> None:
        payload, _ = self.request_json("/api/holdings?size=10&sort=issuer_asc")
        self.assert_keys(payload, {"rows", "count", "page", "size", "value", "managers", "issuers"})
        self.assertGreater(payload["count"], 0)
        self.assert_keys(payload["rows"][0], {
            "id", "ticker", "issuer", "class", "cusip", "value", "shares", "shares_type",
            "put_call", "manager_name", "cik", "starred", "period", "filing_date",
            "submission_type", "accession", "is_amendment", "coverage_status",
        })
        for query in ("%", "_", "' OR 1=1 --"):
            filtered, _ = self.request_json("/api/holdings?" + urlencode({"q": query, "size": 10}))
            self.assertEqual(filtered["count"], 0)

    def test_aggregate_response_schema(self) -> None:
        payload, _ = self.request_json("/api/aggregate?group=issuer&size=10")
        self.assert_keys(payload, {"group", "label", "rows", "count", "page", "size"})
        self.assertEqual(payload["group"], "issuer")
        self.assertGreater(payload["count"], 0)
        self.assert_keys(payload["rows"][0], {"name", "key", "ticker", "starred", "value", "positions", "managers", "issuers"})

    def test_funds_response_schema(self) -> None:
        payload, _ = self.request_json("/api/funds?size=10")
        self.assert_keys(payload, {
            "rows", "count", "starred_count", "page", "size", "scope",
            "signal_price_source", "signal_price_date",
        })
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["starred_count"], 1)
        self.assert_keys(payload["rows"][0], {
            "manager_name", "cik", "starred", "state_country", "filings", "positions",
            "securities", "value", "latest_filing", "coverage_status", "signal_return",
            "signal_pnl", "signal_coverage", "priced_signals", "eligible_signals",
            "signal_rankable", "signal_reason", "scope_value", "scope_scale_inferred",
        })
        self.assertTrue(all(row["filings"] == 3 for row in payload["rows"]))

    def test_research_scope_and_search_override(self) -> None:
        scoped, _ = self.request_json("/api/funds?scope=research&size=10")
        self.assertEqual(scoped["count"], 1)
        self.assertEqual(scoped["rows"][0]["manager_name"], "Alpha Capital")
        searched, _ = self.request_json("/api/funds?scope=research&fund_q=Beta&size=10")
        self.assertEqual(searched["count"], 1)
        self.assertEqual(searched["rows"][0]["manager_name"], "Beta Partners")

    def test_single_part_restatement_is_reported_and_filtered_as_an_amendment(self) -> None:
        holdings, _ = self.request_json(
            "/api/holdings?period=31-DEC-2025&manager=Alpha&amendments=only&size=10"
        )
        self.assertGreater(holdings["count"], 0)
        self.assertTrue(all(row["submission_type"] == "13F-HR/A" for row in holdings["rows"]))
        self.assertTrue(all(row["is_amendment"] == 1 for row in holdings["rows"]))

        funds, _ = self.request_json(
            "/api/funds?period=31-DEC-2025&manager=Alpha&amendments=only&size=10"
        )
        self.assertEqual(funds["count"], 1)
        self.assertEqual(funds["rows"][0]["manager_name"], "Alpha Capital")

    def test_unknown_query_parameters_are_rejected_before_dispatch(self) -> None:
        paths = (
            "/api/meta?x=1", "/api/holdings?x=1", "/api/aggregate?x=1",
            "/api/funds?x=1", "/api/suggest?x=1", "/api/stock-detail?x=1",
            "/api/fund-detail?x=1", "/api/net-adds?x=1",
        )
        for path in paths:
            with self.subTest(path=path):
                payload, _ = self.request_json(path, expected_status=400)
                self.assertIn("Unknown query parameter", payload["error"])

    def test_suggest_response_schema(self) -> None:
        managers, _ = self.request_json("/api/suggest?kind=manager&q=Al")
        self.assertIsInstance(managers, list)
        self.assertEqual(managers[0]["name"], "Alpha Capital")
        self.assert_keys(managers[0], {"name", "key"})
        issuers, _ = self.request_json("/api/suggest?kind=issuer&q=AA")
        self.assertIsInstance(issuers, list)
        self.assert_keys(issuers[0], {"name", "key"})
        short, _ = self.request_json("/api/suggest?kind=invalid&q=A")
        self.assertEqual(short, [])

    def test_stock_detail_response_schema(self) -> None:
        payload, _ = self.request_json(
            "/api/stock-detail?cusip=037833100&period=31-DEC-2025&page=1&size=10"
        )
        self.assert_keys(payload, {
            "security", "current_period", "previous_period", "history", "rows", "count",
            "page", "size", "summary",
        })
        self.assertEqual(payload["security"]["ticker"], "AAPL")
        self.assertEqual(len(payload["history"]), 3)
        self.assert_keys(payload["history"][0], {"period", "period_date", "value", "funds"})
        self.assertGreater(payload["count"], 0)
        self.assert_keys(payload["rows"][0], {
            "manager_id", "manager_name", "cik", "starred", "position_type", "shares_type",
            "current_shares", "previous_shares", "delta_shares", "delta_percent", "current_value",
            "previous_value", "delta_value", "status", "current_weight", "previous_weight",
            "weight_change", "current_coverage", "previous_coverage",
        })
        self.assert_keys(payload["summary"], {
            "increased", "reduced", "new", "exited", "added_or_new", "reduced_or_exited", "not_comparable",
        })

    def test_fund_detail_response_schema(self) -> None:
        payload, _ = self.request_json(
            "/api/fund-detail?cik=1&period=31-DEC-2025&page=1&size=10"
        )
        self.assert_keys(payload, {
            "manager", "current_period", "previous_period", "current_coverage", "previous_coverage",
            "history", "rows", "count", "page", "size", "summary",
        })
        self.assertEqual(payload["manager"]["name"], "Alpha Capital")
        self.assertEqual(len(payload["history"]), 3)
        self.assert_keys(payload["history"][0], {
            "period", "period_date", "coverage_status", "value", "positions", "securities",
        })
        self.assertGreater(payload["count"], 0)
        self.assert_keys(payload["rows"][0], {
            "ticker", "issuer", "class", "cusip", "position_type", "shares_type",
            "current_shares", "previous_shares", "delta_shares", "delta_percent", "current_value",
            "previous_value", "delta_value", "status", "current_weight", "previous_weight",
            "weight_change", "current_coverage", "previous_coverage",
        })
        self.assert_keys(payload["summary"], {"increased", "reduced", "new", "exited"})

    def test_oldest_quarter_detail_schemas_remain_complete(self) -> None:
        stock, _ = self.request_json(
            "/api/stock-detail?cusip=037833100&period=30-JUN-2025&page=7&size=10"
        )
        self.assert_keys(stock, {
            "security", "current_period", "previous_period", "history", "rows", "count",
            "page", "size", "summary",
        })
        self.assertIsNone(stock["previous_period"])
        self.assertEqual((stock["rows"], stock["count"], stock["page"], stock["size"]), ([], 0, 7, 10))
        self.assert_keys(stock["summary"], {
            "increased", "reduced", "new", "exited", "added_or_new", "reduced_or_exited",
            "not_comparable",
        })

        fund, _ = self.request_json(
            "/api/fund-detail?cik=1&period=30-JUN-2025&page=7&size=10"
        )
        self.assert_keys(fund, {
            "manager", "current_period", "previous_period", "current_coverage", "previous_coverage",
            "history", "rows", "count", "page", "size", "summary",
        })
        self.assertIsNone(fund["previous_period"])
        self.assertEqual((fund["rows"], fund["count"], fund["page"], fund["size"]), ([], 0, 7, 10))
        self.assert_keys(fund["summary"], {"increased", "reduced", "new", "exited"})

    def test_out_of_range_fund_detail_page_preserves_total_and_summary(self) -> None:
        base, _ = self.request_json(
            "/api/fund-detail?cik=1&period=31-DEC-2025&page=1&size=10"
        )
        beyond, _ = self.request_json(
            "/api/fund-detail?cik=1&period=31-DEC-2025&page=100&size=10"
        )
        self.assertGreater(base["count"], 0)
        self.assertEqual(beyond["rows"], [])
        self.assertEqual(beyond["count"], base["count"])
        self.assertEqual(beyond["summary"], base["summary"])
        self.assertEqual((beyond["page"], beyond["size"]), (100, 10))

    def test_net_adds_response_schema_for_all_metrics(self) -> None:
        for metric in ("value", "portfolio", "position"):
            with self.subTest(metric=metric):
                payload, _ = self.request_json(
                    "/api/net-adds?" + urlencode({"metric": metric, "position": "SHARES", "size": 10})
                )
                self.assert_keys(payload, {"periods", "metric", "position_type", "rows", "count", "page", "size"})
                self.assertEqual(payload["metric"], metric)
                self.assertEqual(len(payload["periods"]), 2)
                self.assert_keys(payload["periods"][0], {
                    "id", "label", "period_date", "previous_id", "previous_label", "comparable_managers",
                })
                self.assertGreater(payload["count"], 0)
                row = payload["rows"][0]
                self.assert_keys(row, {
                    "ticker", "issuer", "cusip", "market_cap", "overall", "defined_releases",
                    "adding_funds", "cutting_funds", "current_funds", "current_value", "trend",
                    "net_rank", "history",
                })
                self.assertEqual(len(row["history"]), len(payload["periods"]))
                if metric == "position":
                    self.assertEqual(len(row["history_status"]), len(payload["periods"]))

    def test_unknown_api_is_json_404(self) -> None:
        payload, _ = self.request_json("/api/does-not-exist", expected_status=404)
        self.assertEqual(payload, {"error": "Not found"})

    def test_invalid_numerics_enums_ranges_and_pages(self) -> None:
        cases = (
            "/api/holdings?page=0",
            f"/api/holdings?page={server.MAX_PAGE + 1}",
            "/api/holdings?page=abc",
            "/api/holdings?size=9",
            "/api/holdings?size=201",
            "/api/holdings?min_value=-1",
            "/api/holdings?min_value=1.5",
            "/api/holdings?min_value=nan",
            "/api/holdings?min_value=20&max_value=10",
            "/api/holdings?amendments=all",
            "/api/holdings?put_call=short",
            "/api/holdings?form=13F-HR%2FA",
            "/api/holdings?period=ALL",
            "/api/aggregate?group=unknown",
            "/api/aggregate?direction=sideways",
            "/api/aggregate?size=4",
            "/api/funds?starred=2",
            "/api/funds?scope=small",
            "/api/funds?direction=sideways",
            "/api/suggest?kind=unknown&q=AA",
            "/api/stock-detail?cusip=bad%20cusip",
            "/api/stock-detail?cusip=123456789",
            "/api/stock-detail?cusip=037833100&period=unknown",
            "/api/stock-detail?cusip=037833100&change=BOUGHT",
            "/api/stock-detail?cusip=037833100&position=3",
            "/api/stock-detail?cusip=037833100&page=-1",
            "/api/fund-detail?cik=abc",
            "/api/fund-detail?cik=999",
            "/api/fund-detail?cik=1&change=BOUGHT",
            "/api/fund-detail?cik=1&position=3",
            "/api/net-adds?metric=shares",
            "/api/net-adds?position=SHORT",
            "/api/net-adds?direction=sideways",
            "/api/net-adds?min_activity=-1",
            "/api/net-adds?min_activity=1.5",
            "/api/net-adds?min_market_cap=nan",
            "/api/net-adds?min_adding_funds=4&max_adding_funds=3",
            "/api/net-adds?min_cutting_funds=4&max_cutting_funds=3",
            "/api/net-adds?min_market_cap=4&max_market_cap=3",
            "/api/net-adds?page=0",
            "/api/holdings?q=" + "x" * (server.MAX_PARAMETER_LENGTH + 1),
            "/api/holdings?q=line%0Abreak",
        )
        for path in cases:
            with self.subTest(path=path):
                payload, _ = self.request_json(path, expected_status=400)
                self.assertIsInstance(payload.get("error"), str)
                self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
