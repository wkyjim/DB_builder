from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.equity_session_coverage_audit import build_session_rows


def test_build_session_rows_flags_partial_raw_and_indicator_gaps():
    sessions = [date(2026, 5, 1), date(2026, 5, 4)]
    raw = {
        sessions[0]: {"A", "B", "C", "D"},
        sessions[1]: {"A", "B", "C"},
    }
    indicators = {
        sessions[0]: {"A", "B", "C", "D"},
        sessions[1]: {"A", "B"},
    }

    rows = build_session_rows(sessions, raw, indicators, minimum_ratio=0.98)

    assert rows[1]["coverage_ratio"] == 0.75
    assert rows[1]["missing_tickers"] == ["D"]
    assert rows[1]["missing_baseline_tickers"] == ["D"]
    assert rows[1]["missing_indicator_tickers"] == ["C"]
    assert rows[1]["flags"] == "LOW_RAW_COVERAGE,INDICATOR_GAP"
