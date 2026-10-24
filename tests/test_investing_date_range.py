"""Regression: the Investing.com date-range endpoint.

The public historical-data pages render only their most recent ~20 rows and
expose no URL parameter for a wider window. History older than that window is
retrieved through the endpoint the pages themselves use to load their table,
which accepts `start-date` / `end-date`.

These tests pin:
  * the instrument id is read out of the page when it is not pre-mapped,
  * the endpoint URL / query parameters are correct,
  * rows come back date-ascending in the same record shape as the HTML path,
  * the plain HTML page is still the fallback when the endpoint is unavailable.
"""
import sys
from datetime import date

import pytest

import investing_html_scraper as ihs

sys.path.insert(0, "scripts")

PAGE_HTML = (
    "<html><script>window.__NUXT__={"
    '"instrumentId":"123456","currentContentView":"historical-data"'
    "}</script></html>"
)

RANGE_PAYLOAD = {
    "data": [
        {"rowDate": "Oct 23, 2026", "rowDateTimestamp": "2026-10-23T00:00:00Z",
         "last_close": "2,733.00", "last_open": "2,689.00",
         "last_max": "2,741.00", "last_min": "2,680.00"},
        {"rowDate": "Aug 03, 2026", "rowDateTimestamp": "2026-08-03T00:00:00Z",
         "last_close": "2,874.20", "last_open": "2,895.40",
         "last_max": "2,907.80", "last_min": "2,860.00"},
        {"rowDateTimestamp": "2026-10-20T00:00:00Z", "last_close": "2,700.00",
         "last_open": None, "last_max": None, "last_min": None},
    ]
}


class _Resp:
    def __init__(self, status_code=200, payload=None, text="", content=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        # fetch_investing_html() requires a substantial body before it will
        # hand the page to the parser.
        self.content = content if content is not None else text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


_BIG = b"x" * 200_000


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_extract_instrument_id_from_page_payload():
    assert ihs.extract_instrument_id(PAGE_HTML) == "123456"
    assert ihs.extract_instrument_id("") is None
    assert ihs.extract_instrument_id("<html>no id</html>") is None


def test_parse_instrument_row_shape_and_thousands_separator():
    row = ihs.parse_instrument_row(RANGE_PAYLOAD["data"][0])
    assert row == {
        "date": "2026-10-23",
        "price": 2733.00,
        "open": 2689.00,
        "high": 2741.00,
        "low": 2680.00,
    }


def test_parse_instrument_row_tolerates_missing_ohlc():
    row = ihs.parse_instrument_row(RANGE_PAYLOAD["data"][2])
    assert row["date"] == "2026-10-20"
    assert row["price"] == 2700.00
    assert row["open"] is None and row["high"] is None and row["low"] is None


def test_parse_instrument_row_rejects_bad_rows():
    assert ihs.parse_instrument_row({}) is None
    assert ihs.parse_instrument_row({"rowDateTimestamp": "2026-10-20T00:00:00Z"}) is None
    assert ihs.parse_instrument_row(
        {"rowDateTimestamp": "nonsense", "last_close": "1.0"}) is None
    assert ihs.parse_instrument_row(None) is None


# --------------------------------------------------------------------------
# Endpoint call
# --------------------------------------------------------------------------
def test_instrument_ids_cover_every_mapped_symbol():
    missing = sorted(set(ihs.INVESTING_URLS) - set(ihs.INVESTING_INSTRUMENT_IDS))
    assert missing == [], "symbols without a verified instrument id: %s" % missing


def test_fetch_instrument_range_builds_url_and_returns_ascending(monkeypatch):
    calls = {}

    def fake_get(url, **kwargs):
        calls["url"] = url
        calls["params"] = kwargs.get("params")
        calls["headers"] = kwargs.get("headers")
        return _Resp(200, RANGE_PAYLOAD)

    monkeypatch.setattr(ihs._http, "get", fake_get)

    rows = ihs.fetch_instrument_range(
        "CIHc1", start_date=date(2026, 8, 1), end_date=date(2026, 10, 26)
    )

    assert calls["url"] == (
        "https://api.investing.com/api/financialdata/historical/1115062"
    )
    assert calls["params"]["start-date"] == "2026-08-01"
    assert calls["params"]["end-date"] == "2026-10-26"
    assert calls["params"]["time-frame"] == "Daily"
    assert calls["headers"]["domain-id"] == "www"

    assert [r["date"] for r in rows] == ["2026-08-03", "2026-10-20", "2026-10-23"]


def test_fetch_instrument_range_reads_id_from_page_when_unmapped(monkeypatch):
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if "api.investing.com" not in url:
            return _Resp(200, text=PAGE_HTML, content=_BIG)
        return _Resp(200, RANGE_PAYLOAD)

    monkeypatch.setattr(ihs._http, "get", fake_get)
    monkeypatch.delitem(ihs.INVESTING_INSTRUMENT_IDS, "CIHc1")

    rows = ihs.fetch_instrument_range("CIHc1", start_date=date(2026, 8, 1))

    assert len(seen) == 2, "expected a page fetch then the range endpoint"
    assert seen[1].endswith("/historical/123456")
    assert rows


@pytest.mark.parametrize("status", [403, 404, 500])
def test_fetch_instrument_range_returns_none_on_http_failure(monkeypatch, status):
    monkeypatch.setattr(ihs._http, "get", lambda *a, **k: _Resp(status))
    assert ihs.fetch_instrument_range("CIHc1", start_date=date(2026, 8, 1)) is None


def test_fetch_instrument_range_returns_none_on_unparsable_payload(monkeypatch):
    monkeypatch.setattr(ihs._http, "get", lambda *a, **k: _Resp(200, {"data": "junk"}))
    assert ihs.fetch_instrument_range("CIHc1", start_date=date(2026, 8, 1)) is None

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ihs._http, "get", boom)
    assert ihs.fetch_instrument_range("CIHc1", start_date=date(2026, 8, 1)) is None


def test_fetch_instrument_range_unknown_symbol(monkeypatch):
    monkeypatch.setattr(
        ihs._http, "get",
        lambda *a, **k: pytest.fail("must not fetch for an unknown symbol"),
    )
    assert ihs.fetch_instrument_range("NOPE", start_date=date(2026, 8, 1)) is None


# --------------------------------------------------------------------------
# Routing / fallback
# --------------------------------------------------------------------------
def test_fetch_historical_data_prefers_the_range_when_dates_given(monkeypatch):
    monkeypatch.setattr(
        ihs, "fetch_instrument_range",
        lambda symbol, start_date=None, end_date=None, timeout=45: [
            {"date": "2026-08-03", "price": 1.0, "open": 1.0,
             "high": 1.0, "low": 1.0},
        ],
    )
    monkeypatch.setattr(
        ihs, "fetch_investing_html",
        lambda *a, **k: pytest.fail("HTML page must not be fetched when the "
                                    "range endpoint answers"),
    )

    rows = ihs.fetch_historical_data(
        "CIHc1", start_date=date(2026, 8, 1), end_date=date(2026, 10, 26)
    )
    assert rows and rows[0]["date"] == "2026-08-03"


def test_fetch_historical_data_falls_back_to_html_when_range_empty(monkeypatch):
    monkeypatch.setattr(
        ihs, "fetch_instrument_range",
        lambda symbol, start_date=None, end_date=None, timeout=45: None,
    )

    html_page = (
        "<table><tr><td>Date</td><td>Price</td><td>Open</td>"
        "<td>High</td><td>Low</td></tr>"
        "<tr><td>Oct 23, 2026</td><td>2,733.00</td><td>2,689.00</td>"
        "<td>2,741.00</td><td>2,680.00</td></tr></table>"
    )
    monkeypatch.setattr(ihs, "fetch_investing_html", lambda *a, **k: html_page)

    rows = ihs.fetch_historical_data("CIHc1", start_date=date(2026, 8, 1))
    assert rows and rows[0]["date"] == "2026-10-23"
    assert rows[0]["price"] == 2733.00


def test_fetch_historical_data_without_dates_scrapes_the_page(monkeypatch):
    monkeypatch.setattr(
        ihs, "fetch_instrument_range",
        lambda *a, **k: pytest.fail("range endpoint must not be used without dates"),
    )

    html_page = (
        "<table><tr><td>Date</td><td>Price</td><td>Open</td>"
        "<td>High</td><td>Low</td></tr>"
        "<tr><td>Oct 23, 2026</td><td>2,733.00</td><td>2,689.00</td>"
        "<td>2,741.00</td><td>2,680.00</td></tr></table>"
    )
    monkeypatch.setattr(ihs, "fetch_investing_html", lambda *a, **k: html_page)

    rows = ihs.fetch_historical_data("CIHc1")
    assert rows and rows[0]["date"] == "2026-10-23"


def test_fetch_historical_data_empty_when_both_paths_fail(monkeypatch):
    monkeypatch.setattr(
        ihs, "fetch_instrument_range",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(ihs, "fetch_investing_html", lambda *a, **k: None)
    assert ihs.fetch_historical_data("CIHc1", start_date=date(2026, 8, 1)) == []
