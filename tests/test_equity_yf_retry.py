from equity_yf_eod_refresh import _ohlcv_issues, _valid_ohlcv


def test_ohlcv_accepts_complete_row():
    row = {"open": 10, "high": 12, "low": 9, "close": 11, "volume": 1000}
    assert _valid_ohlcv(row)
    assert _ohlcv_issues(row) == []


def test_ohlcv_records_exact_invalid_fields():
    row = {"open": 0, "high": 8, "low": 9, "close": 11, "volume": None}
    issues = _ohlcv_issues(row)
    assert "open_nonpositive" in issues
    assert "volume_missing" in issues
    assert "high_lt_low" in issues
    assert not _valid_ohlcv(row)
